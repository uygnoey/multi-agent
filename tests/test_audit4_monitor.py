"""4차 감사 수정 검증.

- #6 monitor._stop_run 의 TUI stop 상태 불일치 수정.
  예전 구현은 SIGTERM 직후 곧바로 run.pid 를 지워, 프로세스가 아직 살아 run 상태를
  쓰는 중인데도 TUI 가 "stopped" 로 보였다. 이제 웹 UI(webui.RunManager.stop)처럼
  실제 종료를 확인한 뒤에만(또는 SIGKILL 폴백 후) pidfile 을 제거한다.

  실제 단명 서브프로세스를 띄워 pid 를 run.pid 에 쓰고 _stop_run 을 호출한 뒤,
  프로세스가 죽고 pidfile 이 결국 제거되는지 폴링으로 확인한다.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from orchestrator.monitor import _run_alive, _stop_run


def _spawn_sleeper(extra_args: list[str] | None = None) -> subprocess.Popen:
    """자체 프로세스 그룹(start_new_session=True)으로 단명 sleep 프로세스를 띄운다.

    실제 SIGTERM/SIGKILL·killpg 경로를 그대로 태우기 위해 mock 이 아니라 진짜
    서브프로세스를 사용한다. 테스트가 새 나가지 않도록 짧은 sleep 으로 둔다.
    """
    args = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = subprocess.Popen(
        args + (extra_args or []),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc


def _wait_until(pred, timeout: float = 6.0, interval: float = 0.05) -> bool:
    """pred() 가 True 가 될 때까지(또는 timeout) 폴링. 도달하면 True."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def _proc_dead(proc: subprocess.Popen) -> bool:
    # 우리가 띄운 자식이므로 poll() 로 좀비를 reap 한다. os.kill(pid,0) 만으로는 좀비도
    # 살아있다고 보이므로(부모가 reap 하기 전), 자식엔 poll() 이 정확하다 (#6 _stop_run 의
    # _alive 가 _is_zombie 로 보정하는 것과 같은 이유).
    return proc.poll() is not None


# ---------------- #6 stop: 종료 확인 후에만 pidfile 제거 ----------------


def test_stop_run_no_pidfile_returns_false(tmp_path: Path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    assert _stop_run(orch) is False  # run.pid 없음 → False (예외 없이)


def test_stop_run_bad_pidfile_returns_false(tmp_path: Path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "run.pid").write_text("not-a-pid", encoding="utf-8")
    assert _stop_run(orch) is False  # 파싱 실패 → False (예외 없이)


def test_stop_run_terminates_and_removes_pidfile(tmp_path: Path):
    # 실제 서브프로세스를 SIGTERM 으로 종료하고, 죽은 뒤 run.pid 가 제거되는지 확인 (#6).
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    pf = orch / "run.pid"
    proc = _spawn_sleeper()
    try:
        pf.write_text(str(proc.pid), encoding="utf-8")
        assert _stop_run(orch) is True  # stop 시작됨

        # 프로세스가 실제로 죽는다 (SIGTERM 으로 sleep 은 즉시 종료).
        assert _wait_until(lambda: _proc_dead(proc)), "process should die after SIGTERM"
        # 그리고 pidfile 은 (종료 확인 후) 결국 제거된다.
        assert _wait_until(lambda: not pf.exists()), "pidfile should be removed after death"
        # 종료+제거가 끝나면 _run_alive 도 False.
        assert _run_alive(orch) is False
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_stop_run_does_not_unlink_pidfile_synchronously(tmp_path: Path):
    # 핵심 회귀(#6): SIGTERM 을 트랩해 graceful 종료가 늦는 프로세스는, _stop_run 이
    #   반환한 직후 시점에 아직 살아있을 수 있고 그동안 pidfile 도 남아 있어야 한다.
    #   (예전 버그: SIGTERM 직후 동기 unlink → 프로세스 생존 중에도 stopped 로 보임)
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    pf = orch / "run.pid"
    ready = tmp_path / "ready"
    # SIGTERM 을 무시하고 잠시 더 사는 프로세스 (graceful 트랩 시뮬레이션). 핸들러를 설치한
    # 뒤 ready 파일을 써서, 테스트가 신호 무시 준비가 끝난 시점을 결정적으로 알 수 있게 한다
    # (핸들러 설치 전 SIGTERM 이 도착해 그냥 죽는 startup race 제거).
    code = (
        "import signal, time, pathlib\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ok')\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert _wait_until(ready.exists), "child should install SIGTERM handler"
        pf.write_text(str(proc.pid), encoding="utf-8")
        assert _stop_run(orch) is True

        # _stop_run 반환 직후: 프로세스는 SIGTERM 을 무시하므로 아직 살아 있고,
        # pidfile 도 동기적으로 지워지지 않았어야 한다 (TUI 가 곧장 stopped 로 안 보이게).
        assert not _proc_dead(proc)
        assert pf.exists()

        # 백그라운드 supervisor 가 SIGTERM 유예 후 SIGKILL 로 에스컬레이션 → 결국 종료되고
        # pidfile 도 제거된다 (어떤 경우에도 잔상이 남지 않게).
        assert _wait_until(lambda: _proc_dead(proc), timeout=10.0), (
            "process should be SIGKILLed eventually"
        )
        assert _wait_until(lambda: not pf.exists(), timeout=10.0), (
            "pidfile should be removed after SIGKILL fallback"
        )
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_stop_run_kills_whole_process_group(tmp_path: Path):
    # start_new_session 으로 띄운 자체 그룹의 리더를 killpg 로 종료할 수 있어야 한다 (#6).
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    pf = orch / "run.pid"
    proc = _spawn_sleeper()
    try:
        # 자체 세션이면 pgid == pid (그룹 리더).
        assert os.getpgid(proc.pid) == proc.pid
        pf.write_text(str(proc.pid), encoding="utf-8")
        assert _stop_run(orch) is True
        # 그룹 전체로 신호가 가서 리더가 종료된다 (poll() 로 좀비 reap 까지).
        assert _wait_until(lambda: _proc_dead(proc))
        # reap 후 그룹이 비었으면 killpg(pgid, 0) 은 ESRCH(OSError) 를 낸다.
        assert _wait_until(lambda: _group_empty(proc.pid)), (
            "process group should be empty after stop"
        )
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def _group_empty(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return True
    return False
