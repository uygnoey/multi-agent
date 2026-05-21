"""Backend 프로토콜 + 역할 실행 요청/결과 자료형.

모든 백엔드는 동일 계약을 따른다: 역할 세션이 타깃 cwd 안의 파일을 편집하고,
필요하면 결과 JSON 을 result_path 에 남긴다. 오케스트레이터가 그것을 읽는다.
"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


async def run_subprocess(cmd, cwd, timeout, log_path=None, line_render=None):
    """서브프로세스를 타임아웃과 함께 실행. (returncode, stdout, stderr, timed_out) 반환.

    log_path 가 주어지면 stdout/stderr 를 라인 단위로 그 파일에 실시간 append(tee)한다 →
    긴 CLI 호출 중에도 로그가 실시간으로 쌓인다. line_render(line_bytes)->str|None 가 주어지면
    stdout 각 라인을 그 함수로 가독 변환해 기록(예: stream-json → 텍스트). 타임아웃 시 kill.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2**20,  # 큰 JSON 라인 대응 (기본 64KB 초과 방지)
        start_new_session=True,  # 새 프로세스 그룹 → 타임아웃 시 자식까지 그룹째 종료
    )
    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    f = None
    if log_path is not None:
        try:
            f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 (수동 close)
        except Exception:
            f = None

    async def _pump(stream, chunks, render):
        while True:
            try:
                line = await stream.readline()
            except Exception:
                break
            if not line:
                break
            chunks.append(line)
            if f is not None:
                try:
                    if render is not None:
                        rendered = render(line)
                        if rendered:
                            f.write(rendered if rendered.endswith("\n") else rendered + "\n")
                            f.flush()
                    else:
                        f.write(line.decode(errors="replace"))
                        f.flush()
                except Exception:
                    pass

    try:
        await asyncio.wait_for(
            asyncio.gather(
                _pump(proc.stdout, out_chunks, line_render),
                _pump(proc.stderr, err_chunks, None),
            ),
            timeout=timeout,
        )
        await proc.wait()
        return proc.returncode, b"".join(out_chunks), b"".join(err_chunks), False
    except asyncio.TimeoutError:
        # 프로세스 그룹째 종료 (CLI 가 spawn 한 자식 — node/codex 등 — 이 살아남지 않도록)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await proc.wait()
        except Exception:
            pass
        return None, b"".join(out_chunks), b"".join(err_chunks), True
    finally:
        if f is not None:
            try:
                f.close()
            except Exception:
                pass


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
    live_log_path: Path | None = None  # 실시간 스트리밍 로그 파일 (per-agent)
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
    model: str | None = None  # 실제 사용된 모델 (캡처 가능 시)
    tokens: int | None = None  # 토큰 사용량 (codex 등 USD 미보고 백엔드용)
    cost_estimated: bool = False  # 구독 사용 → cost 는 토큰 환산 추정치(실청구 아님)


class Backend:
    name = "base"

    def available(self) -> tuple[bool, str]:
        """(사용 가능 여부, 사람이 읽을 사유)."""
        return False, "not implemented"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        raise NotImplementedError
