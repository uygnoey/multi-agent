"""#M6: PID 재사용 방어 (run.pid 의 시작시각 토큰)."""

from __future__ import annotations

import os
import threading

from orchestrator import procutil


def test_token_cache_thread_safe(monkeypatch):
    # #H06: 여러 스레드가 동시에 process_start_token 을 호출해도(eviction 경합 포함)
    #       "dictionary changed size during iteration" 등 예외가 나면 안 된다.
    monkeypatch.setattr(procutil, "_compute_start_token", lambda pid: f"t{pid}")
    procutil._TOKEN_CACHE.clear()
    errors: list[str] = []

    def worker(base: int) -> None:
        try:
            for i in range(1500):
                procutil.process_start_token(base + (i % 400))  # 400 distinct → eviction 반복
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(b * 1000 + 1,)) for b in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_format_pidfile_roundtrip(tmp_path):
    pf = tmp_path / "run.pid"
    pf.write_text(procutil.format_pidfile(os.getpid()), encoding="utf-8")
    # 첫 줄은 항상 우리 pid.
    assert pf.read_text().splitlines()[0].strip() == str(os.getpid())
    token = procutil.process_start_token(os.getpid())
    if token:  # 이 플랫폼이 시작시각을 구할 수 있으면 둘째 줄에 토큰이 기록된다.
        assert procutil.read_pid_token(pf) == token
    else:  # 못 구하면 pid 한 줄만 — read_pid_token 은 None.
        assert procutil.read_pid_token(pf) is None


def test_pid_is_ours_fallback_without_token():
    # 저장 토큰이 없으면(구형 pidfile) 항상 True 로 폴백한다(하위 호환).
    assert procutil.pid_is_ours(os.getpid(), None) is True
    assert procutil.pid_is_ours(os.getpid(), "") is True


def test_pid_is_ours_detects_reuse():
    token = procutil.process_start_token(os.getpid())
    if not token:
        # 토큰을 못 구하는 플랫폼: 검증을 건너뛰고 폴백(True)만 확인.
        assert procutil.pid_is_ours(os.getpid(), "anything") is True
        return
    # 같은 토큰이면 우리 것, 다른 토큰이면(=pid 재사용) 우리 것이 아니다.
    assert procutil.pid_is_ours(os.getpid(), token) is True
    assert procutil.pid_is_ours(os.getpid(), token + "_STALE") is False


def test_monitor_run_alive_rejects_reused_pid(tmp_path):
    from orchestrator import monitor

    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    pf = orch / "run.pid"
    token = procutil.process_start_token(os.getpid())
    if not token:
        return  # 플랫폼이 토큰 미지원 — 이 검증은 의미 없음
    # 살아있는 우리 pid + '틀린' 시작시각 토큰 = pid 가 재사용된 무관 프로세스 시뮬레이션.
    pf.write_text(f"{os.getpid()}\n{token}_WRONG\n", encoding="utf-8")
    assert monitor._run_alive(orch) is False
    # 올바른 토큰이면 살아있는 것으로 본다.
    pf.write_text(procutil.format_pidfile(os.getpid()), encoding="utf-8")
    assert monitor._run_alive(orch) is True


def test_monitor_stop_run_skips_reused_pid(tmp_path):
    from orchestrator import monitor

    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    pf = orch / "run.pid"
    token = procutil.process_start_token(os.getpid())
    if not token:
        return
    pf.write_text(f"{os.getpid()}\n{token}_WRONG\n", encoding="utf-8")
    # 재사용 의심 pid 에는 시그널을 보내지 않는다 → stop 은 '중지할 우리 run 없음'(False).
    assert monitor._stop_run(orch) is False


def test_webui_run_alive_rejects_reused_pid(tmp_path):
    from orchestrator import webui

    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    pf = orch / "run.pid"
    token = procutil.process_start_token(os.getpid())
    if not token:
        return
    pf.write_text(f"{os.getpid()}\n{token}_WRONG\n", encoding="utf-8")
    assert webui._run_alive(orch) is False
    pf.write_text(procutil.format_pidfile(os.getpid()), encoding="utf-8")
    assert webui._run_alive(orch) is True
