"""2차 감사(audit) 회귀 테스트: orchestrator/webui.py.

대상 이슈:
  #135 — web stop pidfile 정리가 프로세스 종료와 경합. SIGKILL 타이머 경로도 pidfile 을
         반드시 제거하고, is_running/_run_alive 와 stop cleanup 이 일치하도록 한다.
  #38  — 손상된 _run_opts.json 의 숫자 옵션으로 build_command 가 int()/float() raise 하지
         않게 하고, /api/rerun 이 원시 예외 텍스트(str(e))를 노출하지 않게 한다.

모두 오프라인·결정적이다. HTTP 엔드포인트는 임시 포트의 stdlib ThreadingHTTPServer 를
fake spawn 으로 띄워 검증한다.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import webui

# ----------------- #38: build_command 의 손상값 관용 -----------------


def test_build_command_tolerates_corrupt_numeric_opts():
    """#38: _run_opts.json 의 숫자 옵션이 비정상 문자열이어도 int()/float() raise 없이 동작."""
    cmd = webui.build_command(
        "py",
        Path("/s.md"),
        Path("/p"),
        {
            "backend": "mock",
            "concurrency": "abc",
            "poll_interval": "xx",
            "max_units": "nope",
            "max_attempts": "??",
            "timeout": "bad",
            "retries": "huh",
            "budget": "$$$",
        },
    )
    # 손상된 concurrency/poll_interval 은 기본값으로 폴백
    assert cmd[cmd.index("--concurrency") + 1] == "3"
    assert cmd[cmd.index("--poll-interval") + 1] == "600"
    # 변환 불가한 timeout/budget 은 명령에서 생략 (raise 하지 않음)
    assert "--timeout" not in cmd
    assert "--budget" not in cmd
    # 손상된 max_units 는 "전체"로 간주 → 플래그 생략
    assert "--max-units" not in cmd


def test_build_command_coerces_float_string_concurrency():
    """#38: "4.0" 같은 실수 문자열도 정수로 관대하게 변환."""
    cmd = webui.build_command(
        "py", Path("/s.md"), Path("/p"), {"backend": "mock", "concurrency": "4.0"}
    )
    assert cmd[cmd.index("--concurrency") + 1] == "4"


def test_build_command_max_units_nonpositive_omitted():
    """#38: max_units 가 0/음수면 '전체' 의미라 플래그를 넣지 않는다."""
    for v in (0, -3, "0", "-1"):
        cmd = webui.build_command(
            "py", Path("/s.md"), Path("/p"), {"backend": "mock", "max_units": v}
        )
        assert "--max-units" not in cmd, v


def test_build_command_still_passes_valid_numbers():
    """#38: 정상 숫자는 그대로 전달 (회귀 없음)."""
    cmd = webui.build_command(
        "py",
        Path("/s.md"),
        Path("/p"),
        {
            "backend": "mock",
            "concurrency": 5,
            "max_units": 2,
            "max_attempts": 4,
            "timeout": 30.0,
            "budget": 1.5,
            "retries": 2,
        },
    )
    assert cmd[cmd.index("--concurrency") + 1] == "5"
    assert cmd[cmd.index("--max-units") + 1] == "2"
    assert cmd[cmd.index("--max-attempts") + 1] == "4"
    assert cmd[cmd.index("--timeout") + 1] == "30.0"
    assert cmd[cmd.index("--budget") + 1] == "1.5"
    assert cmd[cmd.index("--retries") + 1] == "2"


def test_coerce_helpers():
    """#38: _coerce_int/_coerce_float 단위 검증."""
    assert webui._coerce_int(None, 7) == 7
    assert webui._coerce_int("", 7) == 7
    assert webui._coerce_int("abc", 7) == 7
    assert webui._coerce_int("3", 7) == 3
    assert webui._coerce_int("3.9", 7) == 3
    assert webui._coerce_int(4, 7) == 4
    assert webui._coerce_float(None, 1.0) == 1.0
    assert webui._coerce_float("bad", None) is None
    assert webui._coerce_float("2.5", None) == 2.5


# ----------------- #38: /api/rerun 이 원시 예외를 노출하지 않음 -----------------


@pytest.fixture
def server(tmp_path):
    """fake spawn 으로 실제 서브프로세스 없이 HTTP 핸들러를 띄운다."""
    spawned = []

    def fake_spawn(cmd, log_path):
        spawned.append(cmd)

        class _P:
            pid = 22222

            def poll(self):
                return 0  # 즉시 종료된 척 (rerun 가능 상태)

            def wait(self):
                return 0

        return _P()

    manager = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"base": f"http://127.0.0.1:{port}", "manager": manager, "spawned": spawned}
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, body_obj):
    data = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_rerun_corrupt_opts_returns_clean_error_not_raw_exception(server):
    """#38: 손상된 _run_opts.json 으로도 rerun 은 동작해야 하며(관대 변환), 만약 실패해도
    원시 int() 예외 텍스트가 응답에 새어나오지 않는다.
    """
    m = server["manager"]
    # 정상 start 로 run 생성 후, _run_opts.json 을 손상값으로 덮어쓴다.
    rid = m.start("# spec", {"name": "demo", "backend": "mock", "mock": True})
    op = m.project_dir(rid) / "_run_opts.json"
    op.write_text(json.dumps({"backend": "mock", "concurrency": "abc"}), encoding="utf-8")

    code, j = _post(server["base"], "/api/rerun", {"run": rid})
    # 관대 변환 덕분에 rerun 은 성공한다.
    assert code == 200
    assert "run_id" in j
    # 새 run 의 명령에 손상값 대신 기본 concurrency 가 들어갔는지
    cmd = server["spawned"][-1]
    assert cmd[cmd.index("--concurrency") + 1] == "3"


def test_rerun_error_message_has_no_int_exception_text(server, monkeypatch):
    """#38: build_command 가 (가정상) 어떤 이유로 raise 해도 응답엔 'invalid literal' 같은
    내부 예외 텍스트가 들어가지 않고 깔끔한 메시지로 대체된다.
    """
    m = server["manager"]
    rid = m.start("# spec", {"name": "demo", "backend": "mock", "mock": True})

    def boom(*a, **k):
        raise ValueError("invalid literal for int() with base 10: 'abc'")

    monkeypatch.setattr(webui, "build_command", boom)
    code, j = _post(server["base"], "/api/rerun", {"run": rid})
    assert code == 400
    assert "invalid literal" not in j["error"]
    assert "int()" not in j["error"]


# ----------------- #135: stop pidfile 정리 결정성 -----------------


def test_stop_kills_running_process_repeated(tmp_path):
    """#135: stop() 이 프로세스를 죽이고 run.pid 를 결정적으로 제거한다 (여러 번 반복).

    test_webui.py 의 stop 테스트가 타이밍에 민감하지 않은지(flaky 아님) 확인하기 위해
    같은 시나리오를 반복 실행한다.
    """
    for i in range(5):
        m = webui.RunManager(tmp_path / f"runs{i}")
        orch = m.project_dir(f"r-{i}") / ".orchestrator"
        orch.mkdir(parents=True)
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        (orch / "run.pid").write_text(str(proc.pid), encoding="utf-8")
        try:
            assert m.stop(f"r-{i}") is True
            for _ in range(40):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            assert proc.poll() is not None, f"iteration {i}: 프로세스가 종료되지 않음"
            # pidfile 은 동기 0.5초 확인 또는 supervisor 에서 결정적으로 제거된다.
            for _ in range(60):
                if not (orch / "run.pid").exists():
                    break
                time.sleep(0.1)
            assert not (orch / "run.pid").exists(), f"iteration {i}: run.pid 잔존"
        finally:
            try:
                proc.kill()
            except Exception:
                pass


def test_stop_sigkill_path_removes_pidfile(tmp_path):
    """#135: SIGTERM 을 무시하는(트랩) 프로세스라도 SIGKILL 폴백 경로가 pidfile 을 제거한다."""
    m = webui.RunManager(tmp_path / "runs")
    orch = m.project_dir("trap-1") / ".orchestrator"
    orch.mkdir(parents=True)
    # SIGTERM 을 무시 → SIGKILL 폴백 경로를 강제로 탄다.
    code = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
    proc = subprocess.Popen(["python3", "-c", code], start_new_session=True)
    time.sleep(0.5)  # SIG_IGN 핸들러 설치 대기
    (orch / "run.pid").write_text(str(proc.pid), encoding="utf-8")
    try:
        assert m.stop("trap-1") is True
        # SIGTERM 무시로 0.5초 동기 확인 동안엔 살아있어 pidfile 이 남아있어야 한다(#13).
        assert (orch / "run.pid").exists()
        # supervisor 가 SIGKILL 폴백 후 pidfile 을 제거할 때까지 대기.
        for _ in range(80):
            if not (orch / "run.pid").exists():
                break
            time.sleep(0.1)
        assert not (orch / "run.pid").exists()
        assert proc.poll() is not None  # SIGKILL 로 실제 종료
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_stop_owned_process_removes_pidfile_promptly(tmp_path):
    """#135: 우리가 띄운 자식이 SIGTERM 으로 즉시 죽으면 동기 경로에서 바로 pidfile 제거."""

    real_procs = []

    def real_spawn(cmd, log_path):
        # 실제 sleep 프로세스를 자식으로 띄워 _procs 경로(poll 기반 _alive)를 검증.
        p = subprocess.Popen(["sleep", "30"], start_new_session=True)
        real_procs.append(p)
        return p

    m = webui.RunManager(tmp_path / "runs", spawn=real_spawn)
    rid = m.start("# spec", {"name": "demo", "backend": "mock", "mock": True})
    orch = m.project_dir(rid) / ".orchestrator"
    orch.mkdir(parents=True, exist_ok=True)
    proc = real_procs[-1]
    (orch / "run.pid").write_text(str(proc.pid), encoding="utf-8")
    try:
        assert m.stop(rid) is True
        # 자식이므로 poll() 로 즉시 죽음을 감지 → 동기 0.5초 안에 제거되어야 한다.
        for _ in range(30):
            if not (orch / "run.pid").exists():
                break
            time.sleep(0.05)
        assert not (orch / "run.pid").exists()
        assert proc.poll() is not None
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_run_alive_treats_zombie_as_dead(tmp_path, monkeypatch):
    """#135: _run_alive 가 좀비를 종료로 취급해 stop 의 _alive() 판정과 일치한다.

    좀비가 살아있다고 보이면(os.kill(pid,0) 성공) UI 가 끝난 run 을 계속 'running' 으로
    표시하는 잔상이 생긴다. _is_zombie 를 강제로 True 로 만들어 _run_alive 가 False 를
    반환하는지 확인한다.
    """
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    # 살아있는(혹은 PID 가 존재하는) 프로세스를 가리키되 _is_zombie 를 True 로 패치.
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (orch / "run.pid").write_text(str(proc.pid), encoding="utf-8")
    try:
        # 좀비가 아니면 살아있다고 본다.
        assert webui._run_alive(orch) is True
        # _is_zombie 가 True 면 _run_alive 도 종료(False)로 판정해야 한다.
        monkeypatch.setattr(webui, "_is_zombie", lambda pid: True)
        assert webui._run_alive(orch) is False
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def test_is_running_false_after_stop_removes_pidfile(tmp_path):
    """#135: stop 으로 pidfile 이 제거되면 is_running 도 False (외부/고아 run 포함)."""
    m = webui.RunManager(tmp_path / "runs")
    orch = m.project_dir("r-9") / ".orchestrator"
    orch.mkdir(parents=True)
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (orch / "run.pid").write_text(str(proc.pid), encoding="utf-8")
    try:
        # pidfile 이 있는 동안엔 running 으로 보인다.
        assert m.is_running("r-9") is True
        assert m.stop("r-9") is True
        for _ in range(60):
            if not (orch / "run.pid").exists():
                break
            time.sleep(0.1)
        # pidfile 제거 후 is_running 은 False — UI 잔상 없음.
        assert m.is_running("r-9") is False
    finally:
        try:
            proc.kill()
        except Exception:
            pass
