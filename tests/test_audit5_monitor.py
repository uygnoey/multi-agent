"""감사 5차(2026-05-22) 회귀 테스트: orchestrator/monitor.py.

- #20 _read_agent_log 가 전체 파일을 읽지 않고 seek-tail 로 마지막 n 줄만 반환.
- #3  _stop_run 이 부모(run.pid)가 graceful 종료해도 마지막 그룹 SIGKILL 스윕으로
      SIGTERM 무시 자식(child-only 잔존)을 일소한다.

#3 테스트는 실제 프로세스 그룹을 띄우므로 결정적이도록 넉넉한 타임아웃·정리(cleanup)를 둔다.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from orchestrator.monitor import _read_agent_log, _stop_run


# ---------------------------------------------------------------------------
# #20: _read_agent_log seek-tail
# ---------------------------------------------------------------------------
def test_read_agent_log_tail_large(tmp_path):
    ad = tmp_path / "agents"
    ad.mkdir(parents=True)
    p = ad / "backend-developer.log"
    lines = [f"line-{i}" for i in range(200_000)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = _read_agent_log(tmp_path, "backend-developer", n=5)
    assert out.splitlines() == [f"line-{i}" for i in range(199_995, 200_000)]


def test_read_agent_log_small(tmp_path):
    ad = tmp_path / "agents"
    ad.mkdir(parents=True)
    (ad / "dba.log").write_text("a\nb\nc\n", encoding="utf-8")
    assert _read_agent_log(tmp_path, "dba", n=2).splitlines() == ["b", "c"]


def test_read_agent_log_missing(tmp_path):
    assert _read_agent_log(tmp_path, "nonexistent-role") == ""


# ---------------------------------------------------------------------------
# #3: _stop_run 그룹 SIGKILL 스윕
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_stop_run_reaps_sigterm_ignoring_child(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir(parents=True)
    pidfile = tmp_path / "child.pid"
    # 자식: SIGTERM 을 무시(trap)하고 오래 잔다 → 그룹 SIGTERM 으로는 안 죽고 SIGKILL 만 통한다.
    child_py = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    # 부모 셸: 자식을 백그라운드로 띄우고 자신은 살아 있다(sleep). SIGTERM 에 곧 죽는다(graceful).
    parent_sh = f"{sys.executable} -c {child_py!r} &\nsleep 60\n"
    parent = subprocess.Popen(["/bin/sh", "-c", parent_sh], start_new_session=True)
    try:
        (orch / "run.pid").write_text(str(parent.pid), encoding="utf-8")
        # 자식이 PID 를 기록할 때까지 잠깐 대기(테스트 전제).
        deadline = time.time() + 3.0
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "자식이 PID 를 기록하지 못함 (테스트 전제 실패)"
        child_pid = int(pidfile.read_text().strip())

        # stop: SIGTERM → (부모 graceful 종료) → supervisor 의 그룹 SIGKILL 스윕으로 자식 일소.
        assert _stop_run(orch) is True

        deadline = time.time() + 6.0
        while _pid_alive(child_pid) and time.time() < deadline:
            time.sleep(0.1)
        alive = _pid_alive(child_pid)
        if alive:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except Exception:
                pass
        assert not alive, "SIGTERM 무시 자식이 stop 후에도 살아남음 (그룹 SIGKILL 스윕 누락)"
    finally:
        # 부모/자식 정리(좀비 방지).
        for p in (parent.pid,):
            try:
                os.killpg(os.getpgid(p), signal.SIGKILL)
            except Exception:
                pass
        try:
            parent.wait(timeout=2)
        except Exception:
            pass


def test_stop_run_no_pidfile_returns_false(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir(parents=True)
    # run.pid 가 없으면 stop 대상 없음 → False.
    assert _stop_run(orch) is False
