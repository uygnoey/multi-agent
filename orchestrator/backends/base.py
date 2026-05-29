"""Backend 프로토콜 + 역할 실행 요청/결과 자료형.

모든 백엔드는 동일 계약을 따른다: 역할 세션이 타깃 cwd 안의 파일을 편집하고,
필요하면 결과 JSON 을 result_path 에 남긴다. 오케스트레이터가 그것을 읽는다.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import time
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


def _open_live_log(log_path) -> Any:
    f = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 (closed by caller)
    f.write(f"\n===== backend run @ {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    f.flush()
    return f


def _write_live_log(f, text: str) -> None:
    f.write(text)
    f.flush()


def _close_live_log(f) -> None:
    f.close()


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
        if len(line) > self._max:
            # #RA-buf: 단일 라인이 상한을 넘으면 그 라인'만' 절단한다. 예전엔 clear() 로 이전에
            # 쌓인 라인(앞선 result/usage/init JSONL 이벤트)까지 통째로 버려, 거대한 라인 하나가
            # 들어오면 정작 파싱에 필요한 직전 이벤트들이 사라졌다. 이제 오버사이즈 라인 자체만
            # 절단(JSON 형태면 head, 아니면 tail)해 append 하고, 총 크기 상한은 아래 trim 루프에
            # 맡긴다(가장 오래된 라인부터 drop). prior 누적 라인은 보존한다.
            line = line[: self._max] if line.lstrip().startswith(b"{") else line[-self._max :]
            self.dropped = True
        self._lines.append(line)
        self._size += len(line)
        while self._size > self._max and len(self._lines) > 1:
            self._size -= len(self._lines.popleft())
            self.dropped = True

    def getvalue(self) -> bytes:
        return b"".join(self._lines)


async def run_subprocess(cmd, cwd, timeout, log_path=None, line_render=None, stdin_data=None):
    """서브프로세스를 타임아웃과 함께 실행. (returncode, stdout, stderr, timed_out) 반환.

    log_path 가 주어지면 stdout/stderr 를 라인 단위로 그 파일에 실시간 append(tee)한다 →
    긴 CLI 호출 중에도 로그가 실시간으로 쌓인다. line_render(line_bytes)->str|None 가 주어지면
    stdout 각 라인을 그 함수로 가독 변환해 기록(예: stream-json → 텍스트). 타임아웃 시 kill.

    #RA-e2big: stdin_data(bytes)가 주어지면 그것을 자식의 stdin 으로 흘려준다 → 거대 프롬프트를
    argv 가 아니라 stdin 으로 넘겨 OS ARG_MAX(E2BIG) 를 회피할 수 있다(기본 None=stdin 미사용).

    #34: 반환되는 stdout/stderr 는 스트림당 ~2MB tail 로 제한된다 (메모리 폭주 방지).
    전체 출력은 log_path tee 에 남고, 결과 이벤트는 스트림 끝부분에 있어 tail 로 충분하다.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=2**20,  # 큰 JSON 라인 대응 (기본 64KB 초과 방지)
        start_new_session=True,  # 새 프로세스 그룹 → 타임아웃 시 자식까지 그룹째 종료
    )
    # pgid 는 spawn 직후(부모 확실히 생존) 캡처한다. start_new_session 으로 부모 pid 가
    # 곧 그룹 리더 pgid 다. 부모가 먼저 죽고 자식만 살아남은 경우 타임아웃 처리 시점에는
    # os.getpgid(proc.pid) 가 ProcessLookupError 로 실패해 그룹 SIGKILL 일소를 못 했다(#1).
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    # 호출 시점의 모듈 상한을 읽어 전달 (설정/테스트에서 _MAX_STREAM_BYTES 조정 가능).
    out_chunks = _BoundedBuffer(_MAX_STREAM_BYTES)
    err_chunks = _BoundedBuffer(_MAX_STREAM_BYTES)
    f = None
    log_lock = asyncio.Lock()
    if log_path is not None:
        try:
            # #H08: "w"(truncate)는 같은 파일(agents_dir/{role}.log)에 runner 가 백엔드 호출 직전
            # 기록한 PROMPT 블록과 직전 retry/failover 로그까지 지워 장애 분석성을 떨어뜨린다.
            # "a"(append)로 보존하되, 호출마다 구분자 헤더를 1줄 남겨 시도 경계를 표시한다.
            # (재사용 project-dir 의 과거 run 로그는 board.init 이 run 시작 시 1회 비운다.)
            f = await asyncio.to_thread(_open_live_log, log_path)
        except Exception:
            f = None

    async def _pump(stream, chunks, render):
        pending = b""

        decoder = codecs.getincrementaldecoder("utf-8")("replace") if render is None else None

        async def _write_text(text: str) -> None:
            if f is None:
                return
            if not text:
                return
            try:
                async with log_lock:
                    await asyncio.to_thread(_write_live_log, f, text)
            except Exception:
                pass

        async def _write_rendered(line: bytes) -> None:
            if f is None:
                return
            try:
                if render is not None:
                    rendered = render(line)
                    if rendered:
                        await _write_text(rendered if rendered.endswith("\n") else rendered + "\n")
                else:
                    await _write_text(line.decode(errors="replace"))
            except Exception:
                pass

        while True:
            try:
                data = await stream.read(65536)
            except Exception:
                break
            if not data:
                break
            chunks.append(data)
            if render is None:
                await _write_text(decoder.decode(data, final=False) if decoder is not None else "")
                continue
            pending += data
            while True:
                pos = pending.find(b"\n")
                if pos < 0:
                    break
                line = pending[: pos + 1]
                pending = pending[pos + 1 :]
                await _write_rendered(line)
            if len(pending) > _MAX_STREAM_BYTES:
                pending = (
                    pending[:_MAX_STREAM_BYTES]
                    if pending.lstrip().startswith(b"{")
                    else pending[-_MAX_STREAM_BYTES:]
                )
                await _write_text("[stream line truncated]\n")
        if render is not None and pending:
            await _write_rendered(pending)
        if render is None and decoder is not None:
            tail = decoder.decode(b"", final=True)
            if tail:
                await _write_text(tail)

    async def _feed_stdin():
        # #RA-e2big: 거대 프롬프트를 stdin 으로 흘린다. stdout pump 와 동시에 돌려야(아래 gather)
        # 자식이 출력하며 stdin 을 읽는 경우에도 파이프 버퍼가 막히지 않는다. 쓰고 나면 EOF 를
        # 알리기 위해 stdin 을 닫는다(자식이 stdin 종료를 보고 프롬프트 수신을 끝낼 수 있도록).
        if stdin_data is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
        except Exception:
            # asyncio 의 pipe transport 는 BrokenPipe 를 drain() 밖의 write-ready Future 에도
            # 보관할 수 있다. 예외를 삼킨 뒤 그 Future 의 exception 을 회수하지 않으면
            # "Future exception was never retrieved" 경고가 stderr 를 오염시킨다.
            try:
                protocol = getattr(proc.stdin, "_protocol", None)
                waiter = getattr(protocol, "_drain_waiter", None)
                if waiter is not None and waiter.done():
                    waiter.exception()
            except Exception:
                pass
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            await proc.stdin.wait_closed()
        except Exception:
            pass

    async def _drain_and_wait():
        # #21: stdout/stderr pump 와 proc.wait() 를 한 타임아웃 창 안에 함께 넣는다.
        # 그래야 stdout/stderr 를 닫고도 계속 실행되는 프로세스가 타임아웃을 우회하지 못한다.
        await asyncio.gather(
            _feed_stdin(),
            _pump(proc.stdout, out_chunks, line_render),
            _pump(proc.stderr, err_chunks, None),
        )
        await proc.wait()

    # #audit21: 정상 종료 외 모든 경로(TimeoutError/CancelledError/예외)에서 동일한 그룹
    # 정리를 받도록 cleanup 을 finally 로 통합한다. 이전엔 TimeoutError 만 SIGTERM→SIGKILL
    # 그룹 종료를 수행했고 외부 task.cancel() 로 인한 CancelledError 경로는 transport close
    # 만 해서 provider CLI 가 spawn 한 자식(node/codex 등)이 살아남았다
    # (재현 검증: alive_after_cancel={'shell': False, 'child': True}).
    exited_normally = False
    try:
        await asyncio.wait_for(_drain_and_wait(), timeout=timeout)
        exited_normally = True
        return proc.returncode, out_chunks.getvalue(), err_chunks.getvalue(), False
    except asyncio.TimeoutError:
        return None, out_chunks.getvalue(), err_chunks.getvalue(), True
    finally:
        # 1~3) 정상 종료가 아니고 부모가 아직 살아있으면 SIGTERM 유예 후 SIGKILL (#17 정책).
        #      pgid 는 spawn 직후 캡처한 값을 재사용(여기서 다시 조회하면 부모가 이미 죽어 실패).
        if not exited_normally and proc.returncode is None:
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
            # 2) 최대 ~3초까지 자발적 종료 대기. cancel 재전달로 cleanup 이 끊기지 않게
            #    예외를 모두 흡수하고 다음 단계로 진행한다.
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except BaseException:
                pass
            # 3) 아직 살아있으면 SIGKILL 로 강제 종료
            if proc.returncode is None:
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
                except BaseException:
                    pass
        # 4) 부모가 유예 안에 죽었더라도(grace-success), SIGTERM 을 무시한 채 살아남은
        #    그룹 내 자식(부모 셸은 종료) 을 마지막으로 한 번 더 SIGKILL 로 일소한다.
        #    정상 종료 경로에서도 그룹 내 자식이 남았을 수 있으므로 동일하게 수행.
        #    그룹이 이미 비었으면 무해(try/except).
        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
        if f is not None:
            try:
                await asyncio.to_thread(_close_live_log, f)
            except Exception:
                pass
        # #2: asyncio 서브프로세스 transport 를 루프가 살아있는 지금 명시적으로 닫는다.
        #     닫지 않으면 Process 가 나중에(이벤트 루프가 이미 닫힌 뒤) GC 될 때
        #     BaseSubprocessTransport.__del__ → pipe.close() → loop.call_soon 이
        #     "Event loop is closed" unraisable warning 을 낸다. 지금 닫으면 transport 가
        #     이미 closed 라 __del__ 이 재차 닫지 않아 경고가 사라진다.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            try:
                transport.close()
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
    # True면 백엔드가 제공하는 machine-wide 권한 모드 사용. 기본은 project workspace 권한.
    full_access: bool = False
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
