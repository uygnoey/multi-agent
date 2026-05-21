"""결과파일 계약 실행기: 역할 프롬프트 합성 → 백엔드 호출 → 결과 파싱.

보드 전이 자체는 스케줄러가 담당한다. 여기서는 한 역할 세션을 돌리고
표준화된 outcome dict 를 돌려준다.
"""

from __future__ import annotations

import asyncio
import json
import re

from .agents import load_agent
from .backends import get_backend
from .backends.base import RoleRequest, RoleResult
from .board import Board
from .config import (
    DELEGATES,
    DELEGATION_CAPABLE,
    DELEGATION_TOOL,
    MAX_TURNS,
    PHASE_SUPERVISOR,
    ROLES,
    RunConfig,
)
from .prompts import compose_prompt


class Runner:
    def __init__(self, cfg: RunConfig, board: Board):
        self.cfg = cfg
        self.board = board

    def _candidates(self, role: str) -> tuple[list[str], list[str]]:
        """(가용 후보, skip된 후보) 반환. 가용이 없으면 첫 후보로 명확히 실패시킨다."""
        cands = self.cfg.backends_for(role) or [self.cfg.default_backend or "mock"]

        def _ok(name: str) -> bool:
            try:
                return bool(get_backend(name).available()[0])
            except Exception:
                return False

        avail = [c for c in cands if _ok(c)]
        skipped = [c for c in cands if c not in avail]
        return (avail or cands[:1]), skipped

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
        # cfg 모델 미지정 시 frontmatter 의 per-agent model 을 fallback 으로 사용 (#93/#94).
        # agent.model 은 load_agent 에서 이미 'inherit'→None 정규화됨.
        model = self.cfg.model_for(backend_name) or agent.model
        return RoleRequest(
            role=role,
            phase=spec.phase,
            unit=unit,
            system_prompt=agent.system_prompt,
            prompt=prompt,
            cwd=self.board.project_dir,
            allowed_tools=allowed_tools,
            model=model,
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

        # 누적 예산 enforcement: 백엔드 무관하게 runner 에서 강제 (#112). 보드 누적 비용이 이미
        # 예산 이상이면 백엔드를 호출하지 않고 blocked 반환 → --budget 가 모든 백엔드에 의미.
        if self.cfg.budget is not None:
            total = self.board.snapshot().get("total_cost_usd", 0.0) or 0.0
            if total >= self.cfg.budget:
                blocker = f"budget exceeded: spent ${total:.4f} >= budget ${self.cfg.budget:.4f}"
                await self.board.log_event(role, f"skip [budget] {blocker}")
                await self.board.agent_update(
                    role, status="idle", activity=f"⏸ skipped (budget) {key if unit else ''}"
                )
                return {
                    "status": "blocked",
                    "artifacts": [],
                    "notes": [],
                    "blockers": [blocker],
                    "units": [],
                    "_ok": False,
                }

        prompt = compose_prompt(
            role=role,
            phase=spec.phase,
            unit=unit,
            directives=self.board.directives(),
            result_rel=result_rel,
            spec_excerpt=self.board.snapshot().get("spec_excerpt", ""),
            recent_events=self.board.recent_events(12) if unit is None else "",
        )

        candidates, skipped = self._candidates(role)
        if skipped:  # 왜 특정 provider 가 빠졌는지 run artifact 에서 추적 가능하게
            await self.board.log_event(role, f"backend skipped (unavailable): {skipped}")
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
                if result_path.exists() and _under_results_dir(result_path, self.board):
                    result_path.unlink()  # 후보마다 직전 결과 제거 → 신선도 보장 (#25)

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
                # 상세 로그: 받은 결과. board.write_agent_block 가 본문을 ~20000자로 절단하므로
                # spec/directives/대용량 출력이 로그를 무한히 키우지 않음 (#31/#32). 단, spec 내
                # 민감정보는 여전히 .orchestrator/agents 로그에 (절단된 형태로) 남을 수 있음.
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

        # 감독(PM/PL)은 결과파일을 안 남겨도 자연스럽다. 그 외 역할은 결과 JSON 이 계약.
        result_required = spec.phase != PHASE_SUPERVISOR
        # 페이즈/역할을 넘겨 _ok 판정을 페이즈별 계약에 맞춘다 (#97).
        outcome = self._read_result(result_path, res, result_required, phase=spec.phase, role=role)
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
    def _read_result(
        result_path,
        res,
        result_required: bool = True,
        phase: str | None = None,
        role: str | None = None,
    ) -> dict:
        # 백엔드 호출이 성공한 경우에만 결과파일을 신뢰한다. 실패 시 남아있는
        # 부분/이전 결과파일을 성공으로 오탐하지 않도록 합성 실패 결과를 쓴다.
        # phase/role 을 _coerce_result 로 전달해 페이즈별 계약(아키텍트=units 필수 등)을 반영 (#97).
        if res.ok and result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                return _coerce_result(data, res, phase=phase, role=role)
            except Exception:
                # 결과파일이 있는데 깨졌으면 계약 위반 → 성공으로 오탐하지 않는다
                return {
                    "status": "failed",
                    "artifacts": [],
                    "notes": [],
                    "blockers": ["result file unparseable (contract violation)"],
                    "units": [],
                    "_ok": False,
                }
        if res.ok and result_required:
            # 결과파일이 필수인 역할(dev/test/cicd/docs/architect 등)이 안 썼으면 계약 위반 → 실패
            return {
                "status": "failed",
                "artifacts": [],
                "notes": [res.final_message[:300]] if res.final_message else [],
                "blockers": ["no result file written (contract violation)"],
                "units": [],
                "_ok": False,
            }
        # 결과파일이 불필요한 역할(감독) 또는 백엔드 실패 → 백엔드 결과로 합성
        return {
            "status": "done" if res.ok else "failed",
            "artifacts": [],
            "notes": [res.final_message[:300]] if res.final_message else [],
            "blockers": [] if res.ok else [res.error or "unknown error"],
            "units": [],
            "_ok": res.ok,
        }


# 성공으로 인정하는 status 의 기준선(baseline). 페이즈 무관하게 우선 이 화이트리스트로 거른다
# — 여기 없는 값(fail/failure/error/incomplete 등)이거나 blocker 가 있으면 _ok=False (#97).
_SUCCESS_STATUSES = frozenset(
    {
        "done",
        "designed",
        "dev_done",
        "tested",
        "pass",
        "passed",
        "ok",
        "success",
        "complete",
        "completed",
    }
)

# 페이즈별 성공 status (#97). baseline 통과 후 추가로 페이즈 계약에 맞는지 확인한다.
# 보수적으로 — mock 백엔드(designed/dev_done/tested/done) 와 기존 테스트를 깨지 않도록
# 각 페이즈의 '정상' 값 + 범용 완료 표현(done/complete/completed/ok/success)을 함께 허용한다.
# 명백히 잘못된 조합(예: 설계 역할이 dev_done, 개발 역할이 designed)만 추가로 _ok=False 로 잡는다.
_GENERIC_DONE = frozenset({"done", "ok", "success", "complete", "completed"})
_PHASE_SUCCESS_STATUSES: dict[str, frozenset[str]] = {
    "design": _GENERIC_DONE | {"designed", "pass", "passed", "tested"},
    "dev": _GENERIC_DONE | {"dev_done", "pass", "passed"},
    "test": _GENERIC_DONE | {"tested", "pass", "passed"},
    "cicd": _GENERIC_DONE | {"pass", "passed"},
    "docs": _GENERIC_DONE | {"pass", "passed"},
}


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _under_results_dir(path, board) -> bool:
    """result_path 가 board.results_dir 하위에 있는지 검증 (디렉터리 밖 삭제 방지; #25).

    unit id 는 보드에서 슬러그되어 안전하지만, 방어적으로 resolve 후 results_dir 경계를 확인한다.
    """
    try:
        results_dir = board.results_dir.resolve()
        return board.results_dir.exists() and results_dir in path.resolve().parents
    except Exception:
        return False


def _safe_rel_artifact(raw) -> str | None:
    """아티팩트 경로 경량 검증 (보드 add_artifacts 와 동일 정책; #11).

    str 만 허용하고 strip 후 절대경로(POSIX '/' · Windows 드라이브/역슬래시) 와 '..' traversal
    토큰을 drop. 타깃 프로젝트 기준 상대경로만 유지. 비-str/빈값/안전하지 않은 값은 None.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("/") or s.startswith("\\"):
        return None
    if len(s) >= 2 and s[1] == ":":  # 예: C:\... 드라이브 절대경로
        return None
    parts = re.split(r"[\\/]+", s)
    if ".." in parts:
        return None
    return s


def _coerce_result(data: dict, res, phase: str | None = None, role: str | None = None) -> dict:
    """Normalize an agent-written result file to the expected schema.

    status 를 소문자 정규화하고, 에이전트가 status=failed/blocked/error 또는 blockers 를 보고하면
    (백엔드 호출이 성공했더라도) _ok=False 로 내린다. 그래야 스케줄러 판정이 제대로 잡힌다.

    phase/role 이 주어지면(#97) baseline 화이트리스트 통과 후 추가로 페이즈별 계약을 검사한다:
    - status 가 해당 페이즈의 성공 집합에 들어야 한다(명백히 어긋난 조합만 추가로 거른다).
    - 아키텍트(설계 페이즈, 핵심 역할)는 units 배열이 비어있지 않아야 한다(#98 과 동일한 설계 계약).
      같은 design 페이즈라도 testsheet-creator 는 units 를 만들지 않으므로 units 검사 대상이 아니다.
    phase 미지정 시 기존 동작(baseline 화이트리스트만) 그대로 — 기존 테스트 호환.
    """
    if not isinstance(data, dict):
        # [] / "done" / 숫자 같은 비-객체 JSON 은 계약 위반 → 성공으로 보지 않는다
        return {
            "status": "failed",
            "artifacts": [],
            "notes": [],
            "blockers": ["result JSON is not an object (contract violation)"],
            "units": [],
            "_ok": False,
        }
    status = str(data.get("status") or ("done" if res.ok else "failed")).strip().lower()
    # 빈/공백 blocker 슬롯은 무시 (LLM 이 빈 칸을 남겨도 불필요한 실패가 나지 않게)
    blockers = [s for b in _as_list(data.get("blockers")) if (s := str(b).strip())]
    units = [u for u in _as_list(data.get("units")) if isinstance(u, dict)]
    # 1) baseline: 실패 변형을 다 나열하기보다 성공 status 만 허용(whitelist) — 기존 동작 유지
    ok = res.ok and status in _SUCCESS_STATUSES and not blockers
    # 2) 페이즈별 계약 추가 검사(#97). baseline 을 통과한 경우에만 좁힌다(over-tighten 방지).
    if ok and phase is not None:
        allowed = _PHASE_SUCCESS_STATUSES.get(phase)
        if allowed is not None and status not in allowed:
            # 예: 개발 역할이 'designed', 설계 역할이 'dev_done' 같은 어긋난 조합
            ok = False
            blockers = [*blockers, f"status '{status}' not valid for phase '{phase}'"]
        elif role == "architecture-engineer" and not units:
            # 아키텍트 계약: spec 을 units 로 분해해야 성공 (#97/#98).
            # testsheet-creator 는 같은 design 페이즈라도 units 가 없어 정상이므로 제외.
            ok = False
            blockers = [*blockers, "architect result has no units (contract violation)"]
    # 아티팩트: str 만, 절대경로/'..' 등 안전하지 않은 값 drop (보드 정책과 일치; #11)
    artifacts = [s for a in _as_list(data.get("artifacts")) if (s := _safe_rel_artifact(a))]
    return {
        "status": status,
        "artifacts": artifacts,
        "notes": [str(n) for n in _as_list(data.get("notes"))],
        "blockers": blockers,
        "units": units,
        "_ok": ok,
    }
