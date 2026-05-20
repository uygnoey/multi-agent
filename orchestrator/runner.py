"""결과파일 계약 실행기: 역할 프롬프트 합성 → 백엔드 호출 → 결과 파싱.

보드 전이 자체는 스케줄러가 담당한다. 여기서는 한 역할 세션을 돌리고
표준화된 outcome dict 를 돌려준다.
"""
from __future__ import annotations

import json

from .agents import load_agent
from .backends import get_backend
from .backends.base import RoleRequest
from .board import Board
from .config import MAX_TURNS, ROLES, RunConfig
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

        req = RoleRequest(
            role=role,
            phase=spec.phase,
            unit=unit,
            system_prompt=agent.system_prompt,
            prompt=prompt,
            cwd=self.board.project_dir,
            allowed_tools=agent.tools or list(spec.tools),
            model=self.cfg.model_for(backend_name),
            max_turns=MAX_TURNS.get(spec.phase, 20),
            budget=self.cfg.budget,
            result_path=result_path,
            result_rel=result_rel,
            spec_text=self.board.spec_text,
        )

        await self.board.log_event(
            role, f"start [{backend_name}]" + (f" unit={key}" if unit else "")
        )
        res = await backend.run_role(req)
        outcome = self._read_result(result_path, res)
        cost = f" ${res.cost_usd:.4f}" if res.cost_usd else ""
        await self.board.log_event(
            role,
            f"done ok={res.ok}{cost}" + ("" if res.ok else f" err={res.error}"),
        )
        return outcome

    @staticmethod
    def _read_result(result_path, res) -> dict:
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
                data["_ok"] = res.ok
                return data
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
