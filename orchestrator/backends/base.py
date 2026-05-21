"""Backend 프로토콜 + 역할 실행 요청/결과 자료형.

모든 백엔드는 동일 계약을 따른다: 역할 세션이 타깃 cwd 안의 파일을 편집하고,
필요하면 결과 JSON 을 result_path 에 남긴다. 오케스트레이터가 그것을 읽는다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


async def run_subprocess(cmd: list[str], cwd: str, timeout: float | None):
    """서브프로세스를 타임아웃과 함께 실행. (returncode, stdout, stderr, timed_out) 반환.

    타임아웃 시 자식을 kill 하고 timed_out=True. 멈춘 CLI 백엔드가 런을 무한 정지시키지 않게 한다.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return None, b"", b"", True
    return proc.returncode, out, err, False


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
    timeout: float | None = None  # 백엔드 호출(서브프로세스/세션) 최대 시간(초)
    delegate: bool = False
    # 위임 가능 팀원 정의: [{name, description, prompt, tools, model}]
    teammates: list[dict] = field(default_factory=list)


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
