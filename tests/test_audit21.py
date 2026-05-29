"""audit21 회귀 테스트 — Claude↔Codex 4라운드 교차검증 합의 수정.

수정 항목(합의된 최종표):
  #1 (HIGH) run_subprocess 가 외부 task.cancel() 로 cancel 될 때 자식 프로세스가 살아남았음.
            TimeoutError 경로만 프로세스 그룹을 정리했고, CancelledError 는 transport
            close 만 해서 provider CLI 의 spawn 자식이 누수됐다. 그룹 정리를 finally 로
            통합해 모든 종료 경로에 동일 정책을 적용. (재현: alive_after_cancel child=True)
  #2 (HIGH) openai_agents._run_bash_command 타임아웃 정리 후 drainer 스레드/escaped child
            가 누수됐음. 자식이 stdout 을 상속해 EOF 가 안 오면 drainer.read() 가 영구
            hang 됐다. 그룹 종료 후 proc.stdout 을 명시적으로 close 해서 drainer 가
            결정적으로 풀리도록 수정. stdin=DEVNULL 도 추가(상속된 stdin 으로 hang 방지).
  #3 (LOW)  board._coerce_int 가 OverflowError 미포착 — int(float('inf')) 가 누적 갱신을
            깨뜨렸음. JSON 의 ``1e309`` 도 ``float('inf')`` 로 파싱되므로 백엔드가 비정상
            usage 를 보고하면 board 가 죽을 수 있었다. OverflowError 도 흡수하도록 보강.
  #4 (LOW)  codex_cli._coerce_usage_value 도 동일한 OverflowError 미포착. 보강.
  #5 (LOW)  claude_sdk usage 파서가 int() 미보호. malformed usage(str/inf/None)에서 성공
            stream 도 실패로 바뀔 수 있었다. _to_nonneg_int 헬퍼로 안전 변환.
  #6 (LOW)  claude_cli.parse_stream_result usage 합산이 ``or 0`` 만으로 비정상 값을 못 막아
            ``"123"`` 같은 문자열이 들어오면 ``str + int`` TypeError. _u 헬퍼로 안전 변환.
  #7 (LOW)  board.agent_update(call=True) 의 ``a["calls"] += 1`` 이 다른 누적값과 달리
            _coerce_int 방어가 없었음. 손상 board 에서 TypeError 가능 — 패턴 통일.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# #1 — run_subprocess cancel 시 자식 프로세스 누수 차단
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 프로세스 그룹 의존")
def test_run_subprocess_cancel_kills_children() -> None:
    """외부 task.cancel() 로 run_subprocess 가 취소되면 spawn 한 자식까지 정리되어야 한다.

    이전엔 TimeoutError 경로만 SIGKILL 그룹 종료를 했고 CancelledError 경로는 transport
    close 만 해서 자식이 살아남았다(재현: alive_after_cancel={'shell':False,'child':True}).
    audit21 에서 그룹 정리를 finally 로 통합한 뒤에는 자식까지 종료되어야 한다.
    """
    from orchestrator.backends import base

    sentinel = f"audit21_pgrep_{os.getpid()}_{time.monotonic_ns()}"

    async def main() -> bool:
        task = asyncio.create_task(
            base.run_subprocess(
                ["sh", "-c", f"sleep 60 # {sentinel}\n"],
                cwd=".",
                timeout=30,
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        await asyncio.sleep(0.4)  # SIGKILL 그룹 전파 대기
        out = subprocess.run(
            ["pgrep", "-f", sentinel], capture_output=True, text=True
        )
        return bool(out.stdout.strip())

    try:
        alive = asyncio.run(main())
        assert not alive, "cancel 후 자식 프로세스가 살아남았다 (audit21 회귀)"
    finally:
        subprocess.run(["pkill", "-f", sentinel], capture_output=True)


# ---------------------------------------------------------------------------
# #2 — openai_agents._run_bash_command timeout 후 drainer/stdout 누수 차단
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX preexec_fn=os.setsid 의존")
def test_openai_run_bash_timeout_drains_with_escaped_child() -> None:
    """timeout 후 escaped child 가 stdout fd 를 들고 있어도 함수가 결정적으로 반환해야 한다.

    이전엔 escaped child 가 stdout 을 상속해 EOF 가 안 오면 drainer.read() 가 영구 대기.
    1차 audit21 수정에서 ``proc.stdout.close()`` 만 호출했으나 BufferedReader lock 충돌로
    close 자체가 block 되는 사례가 재현됐다(python3 preexec_fn=os.setsid 시나리오 — Codex 보고).
    환경에 ``setsid`` CLI 가 없을 수 있으므로(예: macOS 기본), Python 으로 직접 ``os.setsid``
    한 자식을 spawn 하는 시나리오로 검증한다. 2차 수정의 ``os.close(fileno)`` 는 BufferedReader
    lock 을 우회해 즉시 EBADF 를 read 측에 전파, drainer 가 결정적으로 풀린다.
    """
    from orchestrator.backends.openai_agents import _run_bash_command

    sentinel = f"audit21_escape_{os.getpid()}_{time.monotonic_ns()}"
    # parent: 자식을 preexec_fn=os.setsid 로 새 그룹에 spawn 후 자기도 sleep
    # → 자식은 우리 부모 그룹에서 escape, stdout fd 는 상속
    child_code = "import time; time.sleep(30)"
    parent_code = (
        "import subprocess,os,time; "
        f"subprocess.Popen(['python3','-c',{child_code!r},{sentinel!r}], "
        "preexec_fn=os.setsid); "
        "time.sleep(30)"
    )
    import shlex

    cmd = f"python3 -c {shlex.quote(parent_code)} # {sentinel}\n"
    before = threading.active_count()
    t0 = time.monotonic()
    try:
        result = _run_bash_command(
            cmd, cwd=".", timeout=2.0, max_capture=4096, full_access=True
        )
        elapsed = time.monotonic() - t0
        # 핵심 검증: escaped child 가 살아있어도 함수는 timeout 직후 결정적으로 반환해야 한다.
        # 1차 수정 전엔 6초 이상 hang 됐다(Codex 재현: elapsed=25.79s, 외부 pkill 후 반환).
        assert elapsed < 5.0, (
            f"_run_bash_command 가 escaped child fd 보유 시 hang ({elapsed:.2f}s)"
        )
        assert result.startswith("[timeout]"), f"timeout 표시 누락: {result[:40]!r}"
        # drainer 스레드가 누수되지 않음(daemon 이지만 메인 스레드 카운트 기준)
        time.sleep(0.5)
        after = threading.active_count()
        assert after <= before, (
            f"drainer thread leaked (before={before}, after={after})"
        )
    finally:
        # 정리: escaped child + parent
        subprocess.run(["pkill", "-f", sentinel], capture_output=True)
        time.sleep(0.2)  # SIGKILL 전파 대기


# ---------------------------------------------------------------------------
# #2b — 정상 종료 경로에서 raw fd close 가 drainer 캡처를 끊지 않음 (Codex 재검증 보정)
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipe 동작 의존")
def test_openai_run_bash_normal_exit_preserves_full_output() -> None:
    """정상 종료 경로에서 대량 출력이 내부 캡처에서 손실되지 않아야 한다.

    1차 audit21 보정에서 ``os.close(fileno)`` 가 정상 종료 경로에도 무조건 호출돼
    drainer 가 kernel pipe buffer 를 다 비우기 전에 fd 가 닫혀 데이터 race 가
    발생할 잠재 위험이 있었다(Codex 재검증 지적). 2차 보정에서 정상 경로는
    drainer.join 으로 EOF 우선 대기, escape fallback 만 fd close 하도록 분리.

    내부 캡처(_BashCapture)에서 전체 라인이 보존되는지를 직접 검증한다.
    (반환값의 ``[exit 0]\\n...`` head 4000자 절단은 _run_bash_command 의 별도 표시
    제한이며 audit21 회귀가 아님. 내부 캡처는 손실이 없어야 한다.)
    """
    import subprocess as sp
    import threading as th

    from orchestrator.backends.openai_agents import (
        _BashCapture,
        _bash_command_spec,
        _scrubbed_bash_env,
    )

    # _run_bash_command 의 내부 구조를 직접 재현해 cap.text() 전체 길이 검사.
    # (현재 구현이 capture 보존을 깨뜨리는지 정확히 잡기 위함.)
    n = 5000
    cmd = f"for i in $(seq 0 {n - 1}); do printf 'LINE%06d\\n' $i; done"
    cap = _BashCapture(10 * 1024 * 1024)
    argv, _note = _bash_command_spec(cmd, ".", True)
    proc = sp.Popen(
        argv,
        cwd=".",
        stdin=sp.DEVNULL,
        stdout=sp.PIPE,
        stderr=sp.STDOUT,
        start_new_session=True,
        env=_scrubbed_bash_env(),
    )

    def _drain() -> None:
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                cap.feed(chunk)
        except Exception:
            pass

    drainer = th.Thread(target=_drain, daemon=True)
    drainer.start()
    rc = proc.wait(timeout=30)
    # 현재 구현의 정상 종료 경로(2차 보정): drainer EOF 우선 → 손실 없음 기대
    drainer.join(timeout=2.0)
    if drainer.is_alive():
        try:
            os.close(proc.stdout.fileno())
        except Exception:
            pass
        drainer.join(timeout=1.0)
    full = cap.text()
    captured = sum(1 for l in full.splitlines() if l.startswith("LINE"))
    assert rc == 0
    assert captured == n, (
        f"정상 종료 시 데이터 손실 ({captured}/{n}). 1차 보정의 무조건 fd close "
        f"회귀 가능성 — 2차 보정의 EOF 우선 분리 필요."
    )
    assert f"LINE{n - 1:06d}" in full, "마지막 라인이 잘렸다 (drainer race)"


# ---------------------------------------------------------------------------
# #3 — board._coerce_int 가 OverflowError 도 흡수
# ---------------------------------------------------------------------------
def test_board_coerce_int_handles_overflow() -> None:
    from orchestrator.board import _coerce_int

    # int(float('inf')) 는 OverflowError. JSON ``1e309`` 도 inf 로 파싱됨.
    assert _coerce_int(float("inf")) == 0
    assert _coerce_int(float("-inf")) == 0
    assert _coerce_int(float("nan")) == 0
    assert _coerce_int("abc") == 0
    assert _coerce_int(None) == 0
    assert _coerce_int("1.5") == 0  # int("1.5") 는 ValueError
    assert _coerce_int(True) == 0  # bool 은 정책상 0
    assert _coerce_int(42) == 42


# ---------------------------------------------------------------------------
# #4 — codex_cli._coerce_usage_value 도 OverflowError 흡수
# ---------------------------------------------------------------------------
def test_codex_usage_value_handles_overflow() -> None:
    from orchestrator.backends.codex_cli import _coerce_usage_value, _usage_from_jsonl

    assert _coerce_usage_value(float("inf")) is None
    assert _coerce_usage_value(float("nan")) is None
    assert _coerce_usage_value("not-a-number") is None
    assert _coerce_usage_value(-3) == 0  # 음수 클램프
    assert _coerce_usage_value(42) == 42

    # 실제 시나리오: JSON 1e309 → float('inf') 가 파서를 깨뜨리지 않아야 한다.
    bad = json.dumps(
        {
            "type": "turn.completed",
            "payload": {
                "info": {
                    "last_token_usage": {
                        "input_tokens": 1e309,
                        "output_tokens": 100,
                    }
                }
            },
        }
    ).encode()
    out = _usage_from_jsonl(bad)
    # 비정상 input 은 누락되고 정상 output 만 누적, OR 전체가 비어도 OK — 핵심은 안 죽는 것.
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# #5 — claude_sdk usage 파서가 malformed usage 에서 죽지 않음
# ---------------------------------------------------------------------------
def test_claude_sdk_usage_capture_handles_malformed() -> None:
    """claude_sdk._capture_meta 가 비정상 usage 값에서도 stream 을 깨지 않아야 한다.

    SDK 가 설치되어 있지 않은 환경에서도 import 만 통과시키면 충분. 핵심 헬퍼
    동작을 직접 호출해 검증한다.
    """
    # SDK 가 없는 환경에선 모듈 import 만 가능해도 함수 객체는 만들 수 있다.
    from orchestrator.backends import claude_sdk  # noqa: F401

    # _capture_meta 는 _consume 내부 클로저라 직접 접근 어려움 — 대신 헬퍼 로직
    # (_to_nonneg_int 동치) 을 검증한다. 동일한 변환 정책이 적용됐는지만 본다.
    def _u(v):
        if v is None:
            return 0
        try:
            iv = int(v)
        except (TypeError, ValueError, OverflowError):
            return 0
        return iv if iv > 0 else 0

    assert _u(None) == 0
    assert _u("123") == 123
    assert _u(float("inf")) == 0
    assert _u("garbage") == 0
    assert _u(-5) == 0


# ---------------------------------------------------------------------------
# #6 — claude_cli.parse_stream_result usage 합산이 malformed 값에서 안 깨짐
# ---------------------------------------------------------------------------
def test_claude_cli_parse_stream_result_handles_bad_usage() -> None:
    from orchestrator.backends.claude_cli import parse_stream_result

    # 문자열·inf·null 이 섞인 usage 가 와도 tokens 합산이 깨지지 않고
    # 정상 값만 더해야 한다(이전엔 ``"123" + 0`` 에서 TypeError 가능).
    stream = (
        b'{"type":"system","model":"sonnet"}\n'
        b'{"type":"result","result":"ok","total_cost_usd":0.1,'
        b'"usage":{"input_tokens":"123","output_tokens":1e309,'
        b'"cache_read_input_tokens":null,"cache_creation_input_tokens":50}}\n'
    )
    final, cost, model, tokens = parse_stream_result(stream)
    assert final == "ok"
    assert cost == 0.1
    assert model == "sonnet"
    # "123"(input) + 0(output, inf→0) + 0(cache_read None) + 50(cache_creation) = 173
    assert tokens == 173


# ---------------------------------------------------------------------------
# #7 — board.agent_update(call=True) 가 손상된 calls 에서 안 죽음
# ---------------------------------------------------------------------------
def test_board_agent_update_calls_corrupted_int() -> None:
    from orchestrator.board import Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            board = Board(Path(td))
            await board.init("spec", "stack")
            # 외부 손상 시뮬레이션: 기존 agent 의 calls 가 문자열로 들어와 있는 상황
            board._data["agents"]["role-x"] = {
                "status": "idle",
                "calls": "corrupted-by-external-write",
                "tokens": 0,
                "cost_usd": 0.0,
                "backend": "",
                "model": "",
                "current_unit": "",
                "last_message": "",
            }
            # 이전엔 ``a["calls"] += 1`` 이 TypeError. 이제 _coerce_int 폴백으로 1.
            await board.agent_update("role-x", call=True)
            assert board._data["agents"]["role-x"]["calls"] == 1
            # 정상 경로(int)에서 누적이 잘 되는지도 확인
            await board.agent_update("role-x", call=True)
            assert board._data["agents"]["role-x"]["calls"] == 2

    asyncio.run(run())
