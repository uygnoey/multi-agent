"""결과파일 계약 실행기: 역할 프롬프트 합성 → 백엔드 호출 → 결과 파싱.

보드 전이 자체는 스케줄러가 담당한다. 여기서는 한 역할 세션을 돌리고
표준화된 outcome dict 를 돌려준다.
"""

from __future__ import annotations

import asyncio
import json

from .agents import load_agent
from .backends import get_backend
from .backends.base import RoleRequest
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

    async def run_role(self, role: str, unit: dict | None = None) -> dict:
        spec = ROLES[role]
        agent = load_agent(role)
        backend_name = self.cfg.backend_for(role)
        backend = get_backend(backend_name)

        key = unit["id"] if unit else "global"
        result_rel = f".orchestrator/results/{role}__{key}.json"
        result_path = self.board.project_dir / result_rel
        if result_path.exists():
            result_path.unlink()  # 직전 결과 제거 → 신선도 보장

        prompt = compose_prompt(
            role=role,
            phase=spec.phase,
            unit=unit,
            directives=self.board.directives(),
            result_rel=result_rel,
            spec_excerpt=self.board.snapshot().get("spec_excerpt", ""),
            recent_events=self.board.recent_events(12) if unit is None else "",
        )

        allowed_tools = agent.tools or list(spec.tools)
        delegate = self.cfg.delegate and backend_name in DELEGATION_CAPABLE
        teammates: list[dict] = []
        if delegate:
            if DELEGATION_TOOL not in allowed_tools:
                allowed_tools = [*allowed_tools, DELEGATION_TOOL]
            for mate in DELEGATES.get(role, ()):
                td = load_agent(mate)
                teammates.append(
                    {
                        "name": mate,
                        "description": td.description,
                        "prompt": td.system_prompt,
                        "tools": td.tools or list(ROLES[mate].tools),
                        "model": td.model,
                    }
                )

        req = RoleRequest(
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
            delegate=delegate,
            teammates=teammates,
        )

        team = f" +team={[m['name'] for m in teammates]}" if teammates else ""
        start_line = f"start [{backend_name}]" + (f" unit={key}" if unit else "") + team
        await self.board.log_event(role, start_line)
        await self.board.agent_update(
            role,
            status="running",
            unit=key,
            backend=backend_name,
            call=True,
            activity=("▶ " + (f"unit={key} " if unit else "") + f"[{backend_name}]{team}"),
        )

        res = await self._run_with_retries(backend, req, role, key)

        outcome = self._read_result(result_path, res)
        if res.cost_usd:
            await self.board.add_cost(res.cost_usd)
        cost = f" ${res.cost_usd:.4f}" if res.cost_usd else ""
        await self.board.log_event(
            role,
            f"done ok={res.ok}{cost}" + ("" if res.ok else f" err={res.error}"),
        )
        summary = (outcome.get("status") or "?") + (
            f" · {len(outcome.get('artifacts', []))} files" if outcome.get("artifacts") else ""
        )
        await self.board.agent_update(
            role,
            status="idle",
            cost_add=res.cost_usd,
            message=res.final_message or (res.error or ""),
            activity=f"✓ done ok={res.ok}{cost} → {summary}"
            if res.ok
            else f"✗ failed{cost}: {res.error}",
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
        if result_path.exists():
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
