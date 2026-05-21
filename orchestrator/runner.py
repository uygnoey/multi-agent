"""결과파일 계약 실행기: 역할 프롬프트 합성 → 백엔드 호출 → 결과 파싱.

보드 전이 자체는 스케줄러가 담당한다. 여기서는 한 역할 세션을 돌리고
표준화된 outcome dict 를 돌려준다.
"""

from __future__ import annotations

import asyncio
import json

from .agents import load_agent
from .backends import get_backend
from .backends.base import RoleRequest, RoleResult
from .board import Board
from .config import (
    DELEGATES,
    DELEGATION_CAPABLE,
    DELEGATION_TOOL,
    MAX_TURNS,
    ROLES,
    RunConfig,
)
from .prompts import compose_prompt


class Runner:
    def __init__(self, cfg: RunConfig, board: Board):
        self.cfg = cfg
        self.board = board

    def _candidates(self, role: str) -> list[str]:
        """우선순위 순서의 백엔드 후보. 가용한 것만 남기되, 없으면 첫 후보로 명확히 실패시킨다."""
        cands = self.cfg.backends_for(role) or [self.cfg.default_backend or "mock"]

        def _ok(name: str) -> bool:
            try:
                return bool(get_backend(name).available()[0])
            except Exception:
                return False

        avail = [c for c in cands if _ok(c)]
        return avail or cands[:1]

    def _build_teammates(self, role: str) -> list[dict]:
        out = []
        for mate in DELEGATES.get(role, ()):
            td = load_agent(mate)
            out.append(
                {
                    "name": mate,
                    "description": td.description,
                    "prompt": td.system_prompt,
                    "tools": td.tools or list(ROLES[mate].tools),
                    "model": td.model,
                }
            )
        return out

    def _build_req(self, role, spec, unit, agent, prompt, result_path, result_rel, backend_name):
        allowed_tools = agent.tools or list(spec.tools)
        delegate = self.cfg.delegate and backend_name in DELEGATION_CAPABLE
        teammates = self._build_teammates(role) if delegate else []
        if delegate and DELEGATION_TOOL not in allowed_tools:
            allowed_tools = [*allowed_tools, DELEGATION_TOOL]
        return RoleRequest(
            role=role,
            phase=spec.phase,
            unit=unit,
            system_prompt=agent.system_prompt,
            prompt=prompt,
            cwd=self.board.project_dir,
            allowed_tools=allowed_tools,
            model=self.cfg.model_for(backend_name),
            max_turns=MAX_TURNS.get(spec.phase, 20),
            budget=self.cfg.budget,
            result_path=result_path,
            result_rel=result_rel,
            spec_text=self.board.spec_text,
            timeout=self.cfg.session_timeout,
            live_log_path=self.board.agents_dir / f"{role}.log",
            delegate=delegate,
            teammates=teammates,
        )

    async def run_role(self, role: str, unit: dict | None = None) -> dict:
        spec = ROLES[role]
        agent = load_agent(role)
        key = unit["id"] if unit else "global"
        result_rel = f".orchestrator/results/{role}__{key}.json"
        result_path = self.board.project_dir / result_rel

        prompt = compose_prompt(
            role=role,
            phase=spec.phase,
            unit=unit,
            directives=self.board.directives(),
            result_rel=result_rel,
            spec_excerpt=self.board.snapshot().get("spec_excerpt", ""),
            recent_events=self.board.recent_events(12) if unit is None else "",
        )

        candidates = self._candidates(role)
        res: RoleResult | None = None
        chosen = candidates[0]
        role_cost = 0.0
        try:
            for i, name in enumerate(candidates):
                chosen = name
                req = self._build_req(
                    role, spec, unit, agent, prompt, result_path, result_rel, name
                )
                team = f" +team={[m['name'] for m in req.teammates]}" if req.teammates else ""
                fo = f" (failover {i + 1}/{len(candidates)})" if i else ""
                await self.board.log_event(
                    role, f"start [{name}]" + (f" unit={key}" if unit else "") + team + fo
                )
                await self.board.agent_update(
                    role,
                    status="running",
                    unit=key,
                    backend=name,
                    call=True,
                    activity="▶ " + (f"unit={key} " if unit else "") + f"[{name}]{team}{fo}",
                )
                if result_path.exists():
                    result_path.unlink()  # 후보마다 직전 결과 제거 → 신선도 보장

                # 상세 로그: 보낸 프롬프트 (시스템 + 작업)
                self.board.write_agent_block(
                    role,
                    f"PROMPT → [{name}]" + (f" unit={key}" if unit else ""),
                    "[SYSTEM]\n" + agent.system_prompt + "\n\n[TASK]\n" + prompt,
                )
                # 후보가 예외를 던져도 다음 후보로 폴오버 (전체 role 을 죽이지 않는다)
                try:
                    res = await self._run_with_retries(get_backend(name), req, role, key)
                except Exception as e:
                    res = RoleResult(ok=False, error=f"backend {name} raised: {e}")
                # 상세 로그: 받은 결과 전문 (절단 없음). CLI 는 위에 원시 스트리밍도 기록됨
                self.board.write_agent_block(
                    role,
                    f"RESULT ← [{name}] ok={res.ok}",
                    res.final_message or res.error or "(no output)",
                )
                if res.cost_usd:
                    role_cost += res.cost_usd
                    await self.board.add_cost(res.cost_usd)
                    await self.board.agent_update(
                        role, cost_add=res.cost_usd, cost_est=res.cost_estimated
                    )
                if res.tokens:
                    await self.board.agent_update(role, tokens_add=res.tokens)
                if res.ok:
                    break
                if i < len(candidates) - 1:
                    nxt = candidates[i + 1]
                    await self.board.log_event(role, f"failover [{name}]→[{nxt}]: {res.error}")
                    await self.board.agent_update(role, activity=f"↪ failover [{name}]→[{nxt}]")
        except Exception as e:  # 예기치 못한 오류라도 절대 전파 금지 (gather 형제 취소 방지)
            res = RoleResult(ok=False, error=f"runner error: {e}")
            await self.board.log_event(role, f"error [{chosen}]: {e}")

        if res is None:
            res = RoleResult(ok=False, error="no backend candidate")

        outcome = self._read_result(result_path, res)
        cost = f" ${role_cost:.4f}" if role_cost else ""
        await self.board.log_event(
            role,
            f"done [{chosen}] ok={res.ok}{cost}" + ("" if res.ok else f" err={res.error}"),
        )
        summary = (outcome.get("status") or "?") + (
            f" · {len(outcome.get('artifacts', []))} files" if outcome.get("artifacts") else ""
        )
        await self.board.agent_update(
            role,
            status="idle",
            backend=chosen,
            model=res.model or self.cfg.model_for(chosen),
            message=res.final_message or (res.error or ""),
            activity=f"✓ done [{chosen}]{cost} → {summary}"
            if res.ok
            else f"✗ failed [{chosen}]{cost}: {res.error}",
        )
        return outcome

    async def _run_with_retries(self, backend, req, role, key):
        attempts = max(1, self.cfg.retries + 1)
        last = None
        for i in range(attempts):
            res = await backend.run_role(req)
            if res.ok:
                return res
            last = res
            if i < attempts - 1:
                delay = self.cfg.retry_backoff * (2**i)
                await self.board.log_event(
                    role, f"retry {i + 1}/{attempts - 1} after err: {res.error} (in {delay:.0f}s)"
                )
                await asyncio.sleep(delay)
        return last

    @staticmethod
    def _read_result(result_path, res) -> dict:
        # 백엔드 호출이 성공한 경우에만 결과파일을 신뢰한다. 실패 시 남아있는
        # 부분/이전 결과파일을 성공으로 오탐하지 않도록 합성 실패 결과를 쓴다.
        if res.ok and result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                return _coerce_result(data, res)
            except Exception:
                pass
        # 결과파일을 안 남긴 경우: 백엔드 결과로 합성
        return {
            "status": "done" if res.ok else "failed",
            "artifacts": [],
            "notes": [res.final_message[:300]] if res.final_message else [],
            "blockers": [] if res.ok else [res.error or "unknown error"],
            "units": [],
            "_ok": res.ok,
        }


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _coerce_result(data: dict, res) -> dict:
    """Normalize an agent-written result file to the expected schema."""
    if not isinstance(data, dict):
        data = {}
    return {
        "status": str(data.get("status") or ("done" if res.ok else "failed")),
        "artifacts": [str(a) for a in _as_list(data.get("artifacts"))],
        "notes": [str(n) for n in _as_list(data.get("notes"))],
        "blockers": [str(b) for b in _as_list(data.get("blockers"))],
        "units": [u for u in _as_list(data.get("units")) if isinstance(u, dict)],
        "_ok": res.ok,
    }
