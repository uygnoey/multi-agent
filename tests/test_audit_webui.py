"""감사(audit) 회귀 테스트: orchestrator/webui.py 의 수정 사항을 검증한다.

대상 이슈: 13, 15, 34, 35, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 72, 78,
79, 80, 81, 88, 89, 99, 100, 101.

모두 오프라인·결정적이며 실제 백엔드/네트워크를 쓰지 않는다. HTTP 엔드포인트는
임시 포트의 stdlib ThreadingHTTPServer 를 띄워 fake spawn 으로 검증한다.
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

# ----------------- 순수 헬퍼 (서버 없이) -----------------


def test_build_command_poll_interval_configurable():
    """#61: poll-interval 이 opts 에서 설정 가능하고, 미지정 시 600 기본."""
    cmd = webui.build_command(
        "py", Path("/s.md"), Path("/p"), {"backend": "mock", "poll_interval": 30}
    )
    assert cmd[cmd.index("--poll-interval") + 1] == "30"
    cmd2 = webui.build_command("py", Path("/s.md"), Path("/p"), {"backend": "mock"})
    assert cmd2[cmd2.index("--poll-interval") + 1] == "600"  # 웹 기본


def test_build_command_forwards_extra_options():
    """#62: timeout/retries/budget/model 도 CLI 로 전달."""
    cmd = webui.build_command(
        "py",
        Path("/s.md"),
        Path("/p"),
        {
            "backend": "mock",
            "timeout": 45.0,
            "retries": 3,
            "budget": 2.5,
            "model": "sonnet",
        },
    )
    assert cmd[cmd.index("--timeout") + 1] == "45.0"
    assert cmd[cmd.index("--retries") + 1] == "3"
    assert cmd[cmd.index("--budget") + 1] == "2.5"
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_build_command_omits_unset_extra_options():
    """#62: 미지정 옵션은 명령에 안 들어간다."""
    cmd = webui.build_command("py", Path("/s.md"), Path("/p"), {"backend": "mock"})
    for flag in ("--timeout", "--retries", "--budget", "--model"):
        assert flag not in cmd


def test_build_command_role_backend_list_join():
    """#100/#101: role_backends 값이 리스트면 콤마로 합쳐 ROLE=B1,B2 형식이 된다."""
    cmd = webui.build_command(
        "py",
        Path("/s.md"),
        Path("/p"),
        {"backend": "mock", "role_backends": {"qa": ["claude-cli", "codex"]}},
    )
    pairs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--role-backend"]
    assert "qa=claude-cli,codex" in pairs  # 리스트 repr 이 아니라 콤마 결합


def test_build_command_role_backend_scalar_still_works():
    """#100/#101: 단일 문자열 값도 그대로 동작."""
    cmd = webui.build_command(
        "py",
        Path("/s.md"),
        Path("/p"),
        {"backend": "mock", "role_backends": {"qa": "codex"}},
    )
    pairs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--role-backend"]
    assert "qa=codex" in pairs


def test_project_dir_rejects_empty_and_whitespace(tmp_path):
    """#67: 빈/공백 run_id 는 base 자체로 resolve 되므로 거부."""
    m = webui.RunManager(tmp_path / "runs")
    for bad in ("", "   ", ".", None):
        with pytest.raises(ValueError):
            m.project_dir(bad)
    # 정상 run_id 는 통과
    assert m.project_dir("ok-1").name == "ok-1"


def test_list_runs_filters_symlink_outside_base(tmp_path):
    """#81: base 밖을 가리키는 심볼릭 run 디렉터리는 목록에서 제외."""
    base = tmp_path / "runs"
    base.mkdir(parents=True)
    # 정상 run
    (base / "good" / ".orchestrator").mkdir(parents=True)
    (base / "good" / ".orchestrator" / "board.json").write_text("{}", encoding="utf-8")
    # base 밖의 외부 run 을 심볼릭으로 연결
    outside = tmp_path / "outside"
    (outside / ".orchestrator").mkdir(parents=True)
    (outside / ".orchestrator" / "board.json").write_text("{}", encoding="utf-8")
    try:
        (base / "evil").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unsupported on this platform")
    ids = {r["id"] for r in webui.list_runs(base)}
    assert "good" in ids
    assert "evil" not in ids  # project_dir() 이 거부할 항목은 노출하지 않음


def test_read_agent_logs_smaller_default_tail(tmp_path):
    """#34/#35: 기본 tail 이 600 이 아니라 작게(<=120) 줄었는지."""
    orch = tmp_path / ".orchestrator"
    (orch / "agents").mkdir(parents=True)
    lines = [f"line{i}" for i in range(500)]
    (orch / "agents" / "qa.log").write_text("\n".join(lines), encoding="utf-8")
    out = webui._read_agent_logs(orch, ["qa"])
    assert "qa" in out
    assert len(out["qa"].splitlines()) <= 120
    # 명시적 n 도 동작
    out2 = webui._read_agent_logs(orch, ["qa"], n=10)
    assert len(out2["qa"].splitlines()) == 10


def test_is_running_removes_reaped_proc(tmp_path):
    """#15: 종료된 프로세스는 reap 시 _procs 에서 제거된다."""

    def fake_spawn(cmd, log_path):
        class _P:
            pid = 9999

            def poll(self):
                return 0  # 이미 종료됨

            def wait(self):
                return 0

        return _P()

    m = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    rid = m.start("# spec", {"name": "demo", "backend": "mock", "mock": True})
    assert rid in m._procs
    # is_running → 종료 감지 → reap → _procs 에서 제거 (pidfile 없으므로 False)
    assert m.is_running(rid) is False
    assert rid not in m._procs  # 누적되지 않음


def test_stop_keeps_pidfile_until_process_dead(tmp_path):
    """#13: SIGTERM 후 프로세스가 죽기 전에는 pidfile 을 지우지 않는다.

    SIGTERM 을 트랩하고 잠깐 살아있는 프로세스를 띄워, stop() 직후 시점에는
    pidfile 이 아직 남아있는지(즉시 삭제 안 함) 확인하고, 종료 후 제거되는지 본다.
    """
    m = webui.RunManager(tmp_path / "runs")
    orch = m.project_dir("trap-1") / ".orchestrator"
    orch.mkdir(parents=True)
    # SIGTERM 을 무시하고 잠시 살아있는 파이썬 프로세스
    code = "import signal,time;signal.signal(signal.SIGTERM, signal.SIG_IGN);time.sleep(2)"
    proc = subprocess.Popen(["python3", "-c", code], start_new_session=True)
    time.sleep(0.5)  # 인터프리터 기동 + SIG_IGN 핸들러 설치 대기(시작 시 SIGTERM 경합 방지)
    (orch / "run.pid").write_text(str(proc.pid), encoding="utf-8")
    try:
        assert m.stop("trap-1") is True
        # stop() 은 0.5초 동기 확인 후 백그라운드로 넘기므로, 아직 살아있는 동안
        # pidfile 이 남아있어야 한다 (즉시 삭제 금지 = #13 의 핵심).
        assert (orch / "run.pid").exists()
        # 프로세스가 죽으면(SIGKILL 폴백 포함) supervisor 가 pidfile 을 제거한다.
        for _ in range(80):
            if not (orch / "run.pid").exists():
                break
            time.sleep(0.1)
        assert not (orch / "run.pid").exists()
    finally:
        try:
            proc.kill()
        except Exception:
            pass


# ----------------- HTTP 엔드포인트 (임시 서버) -----------------


@pytest.fixture
def server(tmp_path):
    """fake spawn 으로 실제 서브프로세스 없이 HTTP 핸들러를 띄운다."""
    spawned = []

    def fake_spawn(cmd, log_path):
        spawned.append(cmd)

        class _P:
            pid = 12345

            def poll(self):
                return None  # 계속 실행 중인 척

        return _P()

    manager = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"base": f"http://127.0.0.1:{port}", "manager": manager, "spawned": spawned}
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, body_obj_or_bytes):
    if isinstance(body_obj_or_bytes, (bytes, bytearray)):
        data = bytes(body_obj_or_bytes)
    else:
        data = json.dumps(body_obj_or_bytes).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(base, path):
    try:
        with urllib.request.urlopen(base + path) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_post_non_object_body_returns_400(server):
    """#78: JSON list/str/number/null 바디는 400 (AttributeError 로 죽지 않음)."""
    for bad in (b"[1,2,3]", b'"hello"', b"42", b"null"):
        code, j = _post(server["base"], "/api/run", bad)
        assert code == 400, bad
        assert "object" in j["error"]


def test_post_invalid_json_returns_400(server):
    code, j = _post(server["base"], "/api/run", b"{not json")
    assert code == 400
    assert j["error"] == "invalid json"


def test_run_backends_must_be_list(server):
    """#79: backends 가 문자열이면 400 (문자 단위 순회 방지)."""
    code, j = _post(
        server["base"], "/api/run", {"spec_text": "x", "backend": "mock", "backends": "mock"}
    )
    assert code == 400
    assert "backends" in j["error"]


def test_run_role_backends_must_be_dict(server):
    """#80: role_backends 가 dict 아니면 .items() 전에 400."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "x", "backend": "mock", "role_backends": ["qa"]},
    )
    assert code == 400
    assert "role_backends" in j["error"]


def test_run_role_backends_accepts_priority_list(server):
    """#100/#101: role_backends 값으로 우선순위 리스트를 받아도 통과(검증)된다."""
    code, j = _post(
        server["base"],
        "/api/run",
        {
            "spec_text": "x",
            "backend": "mock",
            "mock": True,
            "role_backends": {"qa": ["claude-cli", "codex"]},
        },
    )
    assert code == 200
    assert "run_id" in j
    # 빌드된 명령에 콤마 결합 형식이 들어갔는지
    cmd = server["spawned"][-1]
    pairs = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--role-backend"]
    assert "qa=claude-cli,codex" in pairs


def test_run_role_backends_invalid_in_list(server):
    """#100/#101: 리스트 내 잘못된 백엔드는 400."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "x", "backend": "mock", "role_backends": {"qa": ["nope"]}},
    )
    assert code == 400


def test_run_spec_text_must_be_string(server):
    """#89: spec_text 가 list/object 면 400 (write_text 에서 깨지지 않음)."""
    for bad in ([], {"a": 1}, 5):
        code, j = _post(server["base"], "/api/run", {"spec_text": bad, "backend": "mock"})
        assert code == 400, bad
        assert "spec_text" in j["error"]


def test_run_spec_byte_length_limit(server, monkeypatch):
    """#60: 글자 수가 아니라 인코딩 바이트 길이로 검사."""
    monkeypatch.setattr(webui, "MAX_SPEC_BYTES", 10)
    # 멀티바이트 문자 6개 = 18바이트 > 10 (글자 수로는 6 < 10 이지만 바이트로 거부)
    code, j = _post(server["base"], "/api/run", {"spec_text": "가나다라마바", "backend": "mock"})
    assert code == 413
    assert "bytes" in j["error"]


def test_run_raw_numeric_zero_rejected(server):
    """#99: concurrency=0 같은 값이 클라이언트 기본값으로 가려지지 않고 서버에서 400."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "x", "backend": "mock", "concurrency": 0},
    )
    assert code == 400
    assert "concurrency" in j["error"]


def test_run_raw_numeric_nonnumeric_rejected(server):
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "x", "backend": "mock", "max_attempts": "abc"},
    )
    assert code == 400
    assert "max_attempts" in j["error"]


def test_run_negative_budget_rejected(server):
    """#62: budget 음수는 400."""
    code, j = _post(server["base"], "/api/run", {"spec_text": "x", "backend": "mock", "budget": -1})
    assert code == 400
    assert "budget" in j["error"]


def test_run_poll_interval_zero_allowed(server):
    """poll_interval 은 0 허용(>=0)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "x", "backend": "mock", "mock": True, "poll_interval": 0},
    )
    assert code == 200


def test_agent_endpoint_requires_run_and_role(server):
    """#68: run/role 비어있으면 400."""
    code, j = _get(server["base"], "/api/agent")
    assert code == 400
    code, j = _get(server["base"], "/api/agent?run=foo")
    assert code == 400
    code, j = _get(server["base"], "/api/agent?role=qa")
    assert code == 400


def test_agent_endpoint_invalid_role(server):
    code, j = _get(server["base"], "/api/agent?run=foo&role=__bad__")
    assert code == 400
    assert "role" in j["error"]


def test_state_exists_flag(server):
    """#69: board.json 없는 run 은 200 + exists:false (미초기화 vs 미존재 구분)."""
    code, j = _get(server["base"], "/api/state?run=never-started")
    assert code == 200
    assert j["exists"] is False
    assert j["board"] == {}


def test_state_exists_true_after_board(server):
    proj = server["manager"].project_dir("has-board")
    orch = proj / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "board.json").write_text(json.dumps({"phase": "build"}), encoding="utf-8")
    code, j = _get(server["base"], "/api/state?run=has-board")
    assert code == 200
    assert j["exists"] is True
    assert j["board"]["phase"] == "build"


def test_state_only_logs_active_roles(server):
    """#34/#35: board 의 agents 에 있는 역할의 로그만 보낸다."""
    proj = server["manager"].project_dir("with-logs")
    orch = proj / ".orchestrator"
    (orch / "agents").mkdir(parents=True)
    board = {"agents": {"qa": {"status": "running"}}}
    (orch / "board.json").write_text(json.dumps(board), encoding="utf-8")
    (orch / "agents" / "qa.log").write_text("qa working\n", encoding="utf-8")
    # board 에 없는 역할 로그 파일도 존재하지만 응답에 포함되면 안 됨
    (orch / "agents" / "dba.log").write_text("dba idle\n", encoding="utf-8")
    code, j = _get(server["base"], "/api/state?run=with-logs")
    assert code == 200
    assert "qa" in j["agent_logs"]
    assert "dba" not in j["agent_logs"]  # board agents 에 없으면 미포함


def test_state_invalid_run_id_400(server):
    code, j = _get(server["base"], "/api/state?run=..%2F..%2Fetc")
    assert code == 400


def test_run_happy_path_starts(server):
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# spec", "name": "demo", "backend": "mock", "mock": True},
    )
    assert code == 200
    assert j["run_id"].startswith("demo-")


def test_send_handles_broken_pipe(server):
    """#88: 클라이언트가 응답 본문을 받기 전에 끊어도 서버가 트레이스백 없이 살아있다."""
    import socket
    from urllib.parse import urlparse

    u = urlparse(server["base"])
    s = socket.create_connection((u.hostname, u.port))
    s.sendall(b"GET /api/check HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    s.close()  # 응답을 읽지 않고 즉시 닫음 → 서버 _send 에서 BrokenPipe 가능
    time.sleep(0.1)
    # 서버가 여전히 정상 응답하면 OK (핸들러가 죽지 않았다는 증거)
    code, j = _get(server["base"], "/api/check")
    assert code == 200
    assert "backends" in j


# ----------------- 임베디드 HTML/JS 정적 검증 (#65/#66/#72) -----------------


def test_html_mock_unchecked_by_default():
    """#72: mock 체크박스 기본값이 unchecked."""
    assert 'id="mock" checked' not in webui.INDEX_HTML
    assert 'id="mock"' in webui.INDEX_HTML


def test_html_picker_no_inline_onclick_with_run_id():
    """#66: renderPicker 가 onclick 문자열에 r.id 를 직접 끼우지 않는다."""
    assert "selectRun(\\'" not in webui.INDEX_HTML  # onclick="selectRun('...')" 패턴 제거
    assert "data-run=" in webui.INDEX_HTML  # data-attribute 사용
    assert "addEventListener" in webui.INDEX_HTML


def test_html_loadchecks_escapes_backend_fields():
    """#65: 백엔드 이름/사유가 esc() 로 감싸진다."""
    assert "esc(r.name)" in webui.INDEX_HTML
    assert "esc(r.reason)" in webui.INDEX_HTML


def test_html_esc_escapes_quotes():
    """#66: esc() 가 따옴표까지 이스케이프(속성값 안전)."""
    assert "&quot;" in webui.INDEX_HTML
    assert "&#39;" in webui.INDEX_HTML


def test_html_startrun_try_finally_and_raw_values():
    """#63/#99: startRun 이 try/finally 로 버튼 재활성화, +field||default 미사용."""
    assert "finally{" in webui.INDEX_HTML
    assert '+$("concurrency").value||3' not in webui.INDEX_HTML
    assert '+$("maxAttempts").value||2' not in webui.INDEX_HTML


def test_html_loop_awaits_tick():
    """#64: 폴링 loop 가 tick() 을 await 하고 중첩 가드를 둔다."""
    assert "await tick()" in webui.INDEX_HTML
    assert "_looping" in webui.INDEX_HTML


def test_html_has_extra_option_inputs():
    """#62: 폼에 timeout/retries/budget/model/poll-interval 입력이 추가됨."""
    for el in ("pollInterval", "timeout", "retries", "budget", "model"):
        assert f'id="{el}"' in webui.INDEX_HTML
