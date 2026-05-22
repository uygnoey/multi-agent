"""Backend 프로토콜 + 역할 실행 요청/결과 자료형.

모든 백엔드는 동일 계약을 따른다: 역할 세션이 타깃 cwd 안의 파일을 편집하고,
필요하면 결과 JSON 을 result_path 에 남긴다. 오케스트레이터가 그것을 읽는다.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# #34: verbose CLI 가 끝없이 출력해도 컨트롤러 메모리가 무한 증가하지 않도록 스트림별
# 메모리 보관량을 상한선으로 묶는다. 전체 스트림은 log_path 가 있으면 이미 실시간으로
# 로그 파일에 tee 되므로, 메모리에는 마지막 ~2MB(tail)만 유지하면 결과 파싱에 충분하다.
# (claude/codex 의 stream-json 'result'/'turn.completed' 이벤트는 출력 끝부분에 위치 →
#  tail 유지로 parse_stream_result / codex usage 파싱이 그대로 동작한다.)
_MAX_STREAM_BYTES = 2 * 1024 * 1024  # 스트림당 메모리 보관 상한 (~2MB tail)


class _BoundedBuffer:
    """라인 bytes 를 누적하되, 총 보관 크기를 상한으로 제한하는 tail 버퍼.

    상한을 넘으면 가장 오래된 라인부터 버린다(deque 좌측 pop) → 항상 최신 tail 만 남는다.
    full 스트림은 log_path tee 가 책임지므로, 결과 파싱에 필요한 끝부분만 메모리에 둔다.
    """

    def __init__(self, max_bytes: int = _MAX_STREAM_BYTES):
        self._max = max_bytes
        self._lines: deque[bytes] = deque()
        self._size = 0
        self.dropped = False  # tail 유지를 위해 앞부분을 버렸는지 (디버깅/검증용)

    def append(self, line: bytes) -> None:
        self._lines.append(line)
        self._size += len(line)
        while self._size > self._max and len(self._lines) > 1:
            self._size -= len(self._lines.popleft())
            self.dropped = True

    def getvalue(self) -> bytes:
        return b"".join(self._lines)


async def run_subprocess(cmd, cwd, timeout, log_path=None, line_render=None):
    """서브프로세스를 타임아웃과 함께 실행. (returncode, stdout, stderr, timed_out) 반환.

    log_path 가 주어지면 stdout/stderr 를 라인 단위로 그 파일에 실시간 append(tee)한다 →
    긴 CLI 호출 중에도 로그가 실시간으로 쌓인다. line_render(line_bytes)->str|None 가 주어지면
    stdout 각 라인을 그 함수로 가독 변환해 기록(예: stream-json → 텍스트). 타임아웃 시 kill.

    #34: 반환되는 stdout/stderr 는 스트림당 ~2MB tail 로 제한된다 (메모리 폭주 방지).
    전체 출력은 log_path tee 에 남고, 결과 이벤트는 스트림 끝부분에 있어 tail 로 충분하다.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2**20,  # 큰 JSON 라인 대응 (기본 64KB 초과 방지)
        start_new_session=True,  # 새 프로세스 그룹 → 타임아웃 시 자식까지 그룹째 종료
    )
    # 호출 시점의 모듈 상한을 읽어 전달 (설정/테스트에서 _MAX_STREAM_BYTES 조정 가능).
    out_chunks = _BoundedBuffer(_MAX_STREAM_BYTES)
    err_chunks = _BoundedBuffer(_MAX_STREAM_BYTES)
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

    async def _drain_and_wait():
        # #21: stdout/stderr pump 와 proc.wait() 를 한 타임아웃 창 안에 함께 넣는다.
        # 그래야 stdout/stderr 를 닫고도 계속 실행되는 프로세스가 타임아웃을 우회하지 못한다.
        await asyncio.gather(
            _pump(proc.stdout, out_chunks, line_render),
            _pump(proc.stderr, err_chunks, None),
        )
        await proc.wait()

    try:
        await asyncio.wait_for(_drain_and_wait(), timeout=timeout)
        return proc.returncode, out_chunks.getvalue(), err_chunks.getvalue(), False
    except asyncio.TimeoutError:
        # #17: 즉시 SIGKILL 대신 SIGTERM 으로 정리(락/임시파일 등) 유예를 준 뒤 SIGKILL.
        # 프로세스 그룹째 종료한다 (CLI 가 spawn 한 자식 — node/codex 등 — 이 살아남지 않도록).
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
        # 1) SIGTERM (graceful) — 그룹 우선, 실패 시 단일 프로세스
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        # 2) 최대 ~3초까지 자발적 종료 대기
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            # 3) 아직 살아있으면 SIGKILL 로 강제 종료
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                await proc.wait()
            except Exception:
                pass
        except Exception:
            pass
        return None, out_chunks.getvalue(), err_chunks.getvalue(), True
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
    # #21: 백엔드가 요청을 그대로 못 지켰을 때(예: SDK 가 max_budget_usd 거부) 조용히 넘기지
    # 않고 구조화된 경고로 표면화한다. None 이 아니면 호출자/로그에서 눈에 띄게 노출해야 한다.
    warning: str | None = None


class Backend:
    name = "base"

    def available(self) -> tuple[bool, str]:
        """(사용 가능 여부, 사람이 읽을 사유)."""
        return False, "not implemented"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        raise NotImplementedError
