"""감사5 회귀 테스트: 타임아웃 시 그룹 SIGKILL 일소로 자식이 살아남지 않는다.

버그(#1): run_subprocess 타임아웃 처리에서 그룹에 SIGTERM 을 보낸 뒤 부모가 유예
(~3초) 안에 종료하면 SIGKILL 없이 반환했다. 그 경우 SIGTERM 을 무시하고 부모 셸만
먼저 끝난 자식이 그룹에 살아남았다. 수정: 두 경로 모두 마지막에 그룹 SIGKILL 일소로 끝낸다.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time

from orchestrator.backends.base import run_subprocess


def _pid_alive(pid: int) -> bool:
    """signal 0 으로 프로세스 생존 여부 확인 (kill 하지 않음)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 살아있지만 권한 없음 → 생존으로 간주
        return True
    return True


def test_timeout_group_kill_reaps_sigterm_ignoring_child(tmp_path):
    # 부모 셸: SIGTERM 을 무시(trap)하고 오래 sleep 하는 자식을 띄운 뒤, 자식 PID 를
    # 파일에 적고 부모 자신은 곧장 종료한다. 부모는 유예 안에 죽으므로(grace-success)
    # 이전 버그라면 SIGKILL 일소가 없어 자식이 살아남았을 것이다.
    pidfile = tmp_path / "child.pid"
    # 자식: SIGTERM(15) trap 으로 무시 → 그룹 SIGTERM 으로는 안 죽고, SIGKILL 만 통한다.
    child_py = (
        "import os, signal, time, sys; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    # 부모: 자식을 백그라운드로 띄우고 즉시 끝난다(부모 셸 자체는 SIGTERM 으로 잘 죽음).
    parent_sh = f"{sys.executable} -c {child_py!r} &\nexit 0\n"
    # run_subprocess 는 start_new_session=True → 부모/자식이 한 프로세스 그룹에 묶인다.
    rc, _out, _err, timed_out = asyncio.run(
        run_subprocess(["/bin/sh", "-c", parent_sh], str(tmp_path), 0.5)
    )
    assert timed_out is True
    assert rc is None

    # 자식이 PID 를 기록할 때까지 잠깐 대기.
    deadline = time.time() + 3.0
    while not pidfile.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert pidfile.exists(), "자식이 PID 를 기록하지 못함 (테스트 전제 실패)"
    child_pid = int(pidfile.read_text().strip())

    # 그룹 SIGKILL 일소가 동작했다면 자식은 몇 초 안에 사라져야 한다.
    deadline = time.time() + 5.0
    while _pid_alive(child_pid) and time.time() < deadline:
        time.sleep(0.1)
    alive = _pid_alive(child_pid)
    if alive:
        # 테스트가 좀비/잔존 프로세스를 남기지 않도록 정리한 뒤 실패시킨다.
        try:
            os.kill(child_pid, signal.SIGKILL)
        except Exception:
            pass
    assert not alive, "SIGTERM 무시 자식이 타임아웃 후에도 살아남음 (그룹 SIGKILL 일소 누락)"


def test_timeout_still_flags_and_returns_none_rc():
    # 일소 추가 후에도 기존 타임아웃 계약(timed_out=True, rc=None) 이 유지된다.
    rc, _out, _err, timed_out = asyncio.run(
        run_subprocess([sys.executable, "-c", "import time;time.sleep(5)"], ".", 0.3)
    )
    assert timed_out is True
    assert rc is None


def test_normal_completion_unchanged():
    # 정상 종료 경로는 그대로: 타임아웃 아님, 정상 종료코드/출력 반환.
    rc, out, _err, timed_out = asyncio.run(
        run_subprocess([sys.executable, "-c", "print('ok')"], ".", 5.0)
    )
    assert timed_out is False
    assert rc == 0
    assert b"ok" in out
