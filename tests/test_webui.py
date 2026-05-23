"""Offline tests for the web UI helpers + RunManager (no real server/subprocess)."""

from __future__ import annotations

from pathlib import Path

from orchestrator import webui


def test_slugify_and_run_id():
    assert webui.slugify("My App!") == "my-app"
    assert webui.slugify("  ") == "run"
    assert webui.new_run_id("My App").startswith("my-app-")


def test_build_command_flags():
    cmd = webui.build_command(
        "py",
        Path("/s.md"),
        Path("/p"),
        {
            "backend": "claude-cli",
            "concurrency": 4,
            "mock": True,
            "delegate": True,
            "full_access": True,
            "auto_commit": False,
            "max_units": 2,
            "max_attempts": 3,
        },
    )
    assert "--mock" in cmd and "--delegate" in cmd and "--full-access" in cmd
    assert "--no-auto-commit" in cmd
    assert cmd[cmd.index("--backend") + 1] == "claude-cli"
    assert cmd[cmd.index("--max-units") + 1] == "2"
    assert cmd[cmd.index("--concurrency") + 1] == "4"
    assert "--spec" in cmd and "--project-dir" in cmd


def test_run_manager_start_with_fake_spawn(tmp_path):
    captured = {}

    def fake_spawn(cmd, log_path):
        captured["cmd"] = cmd

        class _P:
            def poll(self):
                return None  # still running

        return _P()

    m = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    run_id = m.start("# spec\n- feature one", {"name": "demo", "backend": "mock", "mock": True})

    assert run_id.startswith("demo-")
    spec = m.project_dir(run_id) / "_spec.md"
    assert spec.read_text(encoding="utf-8").startswith("# spec")
    assert m.is_running(run_id) is True
    assert "orchestrator" in captured["cmd"]


def test_rerun_creates_new_run_from_saved_opts(tmp_path):
    def fake_spawn(cmd, log_path):
        class _P:
            pid = 4321

            def poll(self):
                return 0

        return _P()

    m = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    rid = m.start("# spec\n- a", {"name": "demo", "backend": "mock", "mock": True})
    assert (m.project_dir(rid) / "_run_opts.json").exists()  # opts 저장됨
    rid2 = m.rerun(rid)
    assert rid2 != rid and rid2.startswith("demo-")  # 새 run


def test_stop_without_pid_returns_false(tmp_path):
    m = webui.RunManager(tmp_path / "runs")
    (m.project_dir("x-1") / ".orchestrator").mkdir(parents=True)
    assert m.stop("x-1") is False  # pid 없음 → False (예외 없이)


def test_stop_kills_running_process(tmp_path):
    import subprocess
    import time

    m = webui.RunManager(tmp_path / "runs")
    orch = m.project_dir("r-1") / ".orchestrator"
    orch.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (orch / "run.pid").write_text(str(proc.pid), encoding="utf-8")

    try:
        assert m.stop("r-1") is True
        # #42/#135: 결정적이게 — 부하/스케줄 지터에서도 견디도록 충분히 넉넉한
        #   상한(여기선 ~6초)까지 폴링한다. SIGTERM 이 곧 죽이지만, 백그라운드
        #   supervisor 의 SIGKILL 폴백/커널 teardown 까지 여유 있게 기다린다.
        for _ in range(120):  # ≈6초: 프로세스 종료 대기 (sleep 차단 금지 위해 짧은 간격)
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None  # 실제로 종료됨
        # pidfile 제거는 종료 "확인" 후 동기(0.5초) 또는 supervisor 스레드에서 일어나므로
        # 프로세스 종료 직후가 아니라 별도로 폴링해 확인한다(즉시 단정 시 flaky).
        for _ in range(120):  # ≈6초: pidfile 제거 대기
            if not (orch / "run.pid").exists():
                break
            time.sleep(0.05)
        assert not (orch / "run.pid").exists()  # 종료 확인 후 stopped (pidfile 제거)
    finally:
        try:
            proc.kill()  # 테스트 누수 방지 (이미 죽었으면 무해)
        except Exception:
            pass


def test_new_run_id_is_unique():
    assert webui.new_run_id("x") != webui.new_run_id("x")  # 같은 초에도 유일


def test_read_events_tail(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "events.log").write_text("a\nb\nc\nd\n", encoding="utf-8")
    assert webui._read_events(orch, n=2) == "c\nd"
    assert webui._read_events(tmp_path / "none") == ""  # 없는 디렉터리 → 빈 문자열


def test_list_runs_finds_board(tmp_path):
    base = tmp_path / "runs"
    orch = base / "r1" / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "board.json").write_text("{}", encoding="utf-8")
    assert any(r["id"] == "r1" for r in webui.list_runs(base))
