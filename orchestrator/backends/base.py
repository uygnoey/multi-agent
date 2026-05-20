"""Backend 프로토콜 + 역할 실행 요청/결과 자료형.

모든 백엔드는 동일 계약을 따른다: 역할 세션이 타깃 cwd 안의 파일을 편집하고,
필요하면 결과 JSON 을 result_path 에 남긴다. 오케스트레이터가 그것을 읽는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RoleRequest:
    role: str
    phase: str
    unit: dict | None
    system_prompt: str
    prompt: str
    cwd: Path  # 타깃 project-dir (절대경로)
    allowed_tools: list[str]
    model: str | None
    max_turns: int
    budget: float | None
    result_path: Path  # 절대경로
    result_rel: str  # cwd 기준 상대경로
    spec_text: str


@dataclass
class RoleResult:
    ok: bool
    final_message: str = ""
    cost_usd: float | None = None
    raw: Any = None
    error: str | None = None


class Backend:
    name = "base"

    def available(self) -> tuple[bool, str]:
        """(사용 가능 여부, 사람이 읽을 사유)."""
        return False, "not implemented"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        raise NotImplementedError
