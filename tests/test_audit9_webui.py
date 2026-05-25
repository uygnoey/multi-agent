"""감사 9차 회귀 테스트: orchestrator/webui.py 생산성 강화.

대상 수정:
  #1  HIGH(safety) — run.pid 의 PID 를 *양의 정수* 일 때만 사용(_read_pid). 0/-1/비숫자
        같은 손상값이 os.kill/os.getpgid/os.killpg 로 흘러가 무관한 프로세스를 죽이는 사고
        방지. (둘째 줄 start-time 토큰은 너그럽게 허용하되 강제하지 않음.)
  #2  MEDIUM(CSRF) — 무토큰 dogfood 에서도 상태변경 POST 가 브라우저발 cross-site 면 차단
        (Sec-Fetch-Site). 비-브라우저(Origin/Sec-Fetch 없음)는 기존대로 허용.
  #3  MEDIUM — rerun 이 저장된 _run_opts.json 을 start() 에 넘기기 전에 /api/run 과 같은
        타입 보장으로 정규화(sanitize_run_opts).
  #4  MEDIUM — _json 이 직렬화 불가/surrogate 값에 raise 하지 않고 500 JSON 으로 응답.
  #9  LOW — _json Content-Type 에 charset=utf-8.
  #12 LOW — /api/state 가 board _corrupt 를 corrupt 로 노출.
  #6  LOW — is_running 의 liveness 결과를 짧은 TTL 로 캐시(폴링 부하 상한).

오프라인·결정적이며 실제 백엔드/네트워크를 쓰지 않는다. HTTP 엔드포인트는 임시 포트의
stdlib ThreadingHTTPServer 를 fake spawn 으로 띄워 검증한다.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from orchestrator import webui


# ---------------------------------------------------------------------------
# HTTP harness (다른 audit*_webui 와 동일 패턴)
# ---------------------------------------------------------------------------
@pytest.fixture
def make_server(tmp_path):
    servers = []

    def _make(token=None):
        spawned = []

        def fake_spawn(cmd, log_path):
            spawned.append(cmd)

            class _P:
                pid = 44444

                def poll(self):
                    return 0  # 즉시 종료된 척 (rerun 가능)

                def wait(self):
                    return 0

            return _P()

        manager = webui.RunManager(tmp_path / f"runs{len(servers)}", spawn=fake_spawn)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, token))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        servers.append(httpd)
        return {
            "base": f"http://127.0.0.1:{port}",
            "port": port,
            "manager": manager,
            "spawned": spawned,
        }

    yield _make
    for h in servers:
        h.shutdown()
        h.server_close()


def _request(base, path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        h.update(headers)
    req = urllib.request.Request(base + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), e.headers


# ---------------------------------------------------------------------------
# #1: _read_pid — 양의 정수만, 0/-1/비숫자 거부, 둘째 줄 토큰 허용
# ---------------------------------------------------------------------------
def test_read_pid_accepts_positive(tmp_path):
    pf = tmp_path / "run.pid"
    pf.write_text("12345\n", encoding="utf-8")
    assert webui._read_pid(pf) == 12345


def test_read_pid_rejects_zero_and_negative(tmp_path):
    pf = tmp_path / "run.pid"
    for bad in ("0", "-1", "-12345", "  -1  "):
        pf.write_text(bad, encoding="utf-8")
        assert webui._read_pid(pf) is None, bad


def test_read_pid_rejects_non_numeric(tmp_path):
    pf = tmp_path / "run.pid"
    for bad in ("", "   ", "abc", "12x", "1.5", "0x10"):
        pf.write_text(bad, encoding="utf-8")
        assert webui._read_pid(pf) is None, bad


def test_read_pid_tolerates_optional_second_line(tmp_path):
    # 다른 소유자가 둘째 줄에 start-time 토큰을 덧붙여도 첫 줄 PID 를 읽는다(강제하지 않음).
    pf = tmp_path / "run.pid"
    pf.write_text("9876\n1700000000.5\n", encoding="utf-8")
    assert webui._read_pid(pf) == 9876


def test_read_pid_missing_file_returns_none(tmp_path):
    assert webui._read_pid(tmp_path / "nope.pid") is None


def test_run_alive_rejects_corrupt_pid_no_signal(tmp_path, monkeypatch):
    # 손상된 pid(0/-1)면 _run_alive 는 os.kill 을 *호출하지 않고* False 를 돌려준다.
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "run.pid").write_text("-1\n", encoding="utf-8")

    called = []
    real_kill = webui.os.kill

    def spy_kill(pid, sig):
        called.append((pid, sig))
        return real_kill(pid, sig)

    monkeypatch.setattr(webui.os, "kill", spy_kill)
    assert webui._run_alive(orch) is False
    assert called == [], "손상된 PID 에 시그널을 보내면 안 된다(os.kill 호출 금지)"


def test_stop_with_corrupt_pid_returns_false_without_signaling(tmp_path, monkeypatch):
    m = webui.RunManager(tmp_path / "runs")
    orch = m.project_dir("c-1") / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "run.pid").write_text("0\n", encoding="utf-8")  # 손상값

    called = []
    monkeypatch.setattr(webui.os, "kill", lambda pid, sig: called.append((pid, sig)))
    monkeypatch.setattr(webui.os, "killpg", lambda pgid, sig: called.append(("pg", pgid, sig)))
    # pid 가 None 으로 거부되므로 stop 은 시그널 없이 False.
    assert m.stop("c-1") is False
    assert called == []


# ---------------------------------------------------------------------------
# #2: CSRF — 무토큰 서버에서도 브라우저발 cross-site POST 차단
# ---------------------------------------------------------------------------
def test_no_token_cross_site_fetch_post_blocked(make_server):
    # 무토큰 dogfood 서버 + Origin 없음 + Sec-Fetch-Site: cross-site(브라우저 위조 페이지)
    # → CSRF 표적이므로 403.
    s = make_server()
    code, text, _ = _request(
        s["base"],
        "/api/run",
        "POST",
        {"spec_text": "# s", "backend": "mock"},
        {"Sec-Fetch-Site": "cross-site"},
    )
    assert code == 403, text
    assert "cross-origin" in json.loads(text)["error"]
    assert s["spawned"] == []  # 차단 → run 생성 안 됨


def test_no_token_same_site_fetch_post_blocked(make_server):
    # same-site(다른 서브도메인/포트)도 CSRF 가능 → 차단.
    s = make_server()
    code, _t, _ = _request(
        s["base"],
        "/api/run",
        "POST",
        {"spec_text": "# s", "backend": "mock"},
        {"Sec-Fetch-Site": "same-site"},
    )
    assert code == 403


def test_no_token_same_origin_fetch_post_allowed(make_server):
    # Sec-Fetch-Site: same-origin → 정상 같은 출처 요청 → 허용.
    s = make_server()
    code, text, _ = _request(
        s["base"],
        "/api/run",
        "POST",
        {"spec_text": "# s", "backend": "mock"},
        {"Sec-Fetch-Site": "same-origin"},
    )
    assert code == 200, text


def test_no_token_sec_fetch_none_allowed(make_server):
    # Sec-Fetch-Site: none(주소창 직접 입력/북마크) → 허용.
    s = make_server()
    code, text, _ = _request(
        s["base"],
        "/api/run",
        "POST",
        {"spec_text": "# s", "backend": "mock"},
        {"Sec-Fetch-Site": "none"},
    )
    assert code == 200, text


def test_no_token_no_origin_no_secfetch_allowed(make_server):
    # 비-브라우저(curl/urllib): Origin/Sec-Fetch-Site 둘 다 없음 → 기존대로 허용(하위호환).
    s = make_server()
    code, text, _ = _request(s["base"], "/api/run", "POST", {"spec_text": "# s", "backend": "mock"})
    assert code == 200, text


# ---------------------------------------------------------------------------
# #3: rerun opts 정규화 (sanitize_run_opts)
# ---------------------------------------------------------------------------
def test_sanitize_run_opts_normalizes_types():
    out = webui.sanitize_run_opts(
        {
            "backend": ["not", "a", "string"],  # → "mock"
            "backends": "claude-cli, codex",  # 문자열 → list
            "role_backends": ["bad", "list"],  # dict 아님 → None
            "completion_level": "BOGUS",  # 화이트리스트 밖 → mvp
            "name": 12345,  # 비문자열 → None
            "concurrency": "abc",  # #M08: 손상값 → 기본 1 로 클램프
        }
    )
    assert out["backend"] == "mock"
    assert out["backends"] == ["claude-cli", "codex"]
    assert out["role_backends"] is None
    assert out["completion_level"] == "mvp"
    assert out["name"] is None
    assert out["concurrency"] == 1  # #M08


def test_sanitize_run_opts_clamps_numbers_and_model():
    # #M08: 음수 숫자가 build_command → CLI argparse 검증 에러로 재실행을 깨지 않게 클램프.
    out = webui.sanitize_run_opts(
        {
            "concurrency": -5,  # → 1 (>=1)
            "max_attempts": -3,  # → 0 (>=0)
            "retries": -1,  # → 0
            "max_units": -2,  # → 0 (build_command 가 0 은 생략)
            "model": "-rm -rf",  # #M03: '-' 시작 → None (argv 오염 방지)
        }
    )
    assert out["concurrency"] == 1
    assert out["max_attempts"] == 0
    assert out["retries"] == 0
    assert out["max_units"] == 0
    assert out["model"] is None


def test_sanitize_run_opts_role_backends_cleaned():
    out = webui.sanitize_run_opts(
        {
            "role_backends": {
                "project-manager": "claude-cli",  # 유효
                "backend-developer": ["codex", 123, ""],  # 비문자열/빈값 제거 → ["codex"]
                "nonexistent-role": "x",  # 알 수 없는 역할 → 제거
                "project-leader": {"nested": 1},  # dict → 제거
            }
        }
    )
    rb = out["role_backends"]
    assert rb.get("project-manager") == "claude-cli"
    assert rb.get("backend-developer") == ["codex"]
    assert "nonexistent-role" not in rb
    assert "project-leader" not in rb


def test_rerun_sanitizes_bad_backends_before_start(make_server):
    # 저장 opts 의 backends 가 dict(손상)여도 rerun 은 깨지지 않고 동작한다.
    s = make_server()
    m = s["manager"]
    rid = m.start("# spec", {"name": "demo", "backend": "mock", "mock": True})
    op = m.project_dir(rid) / "_run_opts.json"
    op.write_text(
        json.dumps({"backend": "mock", "backends": {"weird": "dict"}, "completion_level": "Zzz"}),
        encoding="utf-8",
    )
    code, text, _ = _request(s["base"], "/api/rerun", "POST", {"run": rid})
    assert code == 200, text
    cmd = s["spawned"][-1]
    # 손상 backends 가 제거되어 --backends 가 명령에 들어가지 않는다.
    assert "--backends" not in cmd
    # 손상 completion_level 은 mvp 로 정규화.
    assert cmd[cmd.index("--completion-level") + 1] == "mvp"


# ---------------------------------------------------------------------------
# #4 / #9: _json 직렬화 실패 → 500, charset=utf-8
# ---------------------------------------------------------------------------
def test_json_serialization_failure_returns_500(make_server, monkeypatch):
    # /api/check 핸들러가 직렬화 불가 객체(set)를 반환하도록 강제 → _json 이 500 으로 방어.
    s = make_server()

    def bad_backend_status():
        return [{"name": "x", "ok": True, "reason": {"unserializable", "set"}}]

    monkeypatch.setattr(webui, "backend_status", bad_backend_status)
    code, text, headers = _request(s["base"], "/api/check")
    assert code == 500, text
    assert json.loads(text)["error"] == "internal serialization error"
    # 핸들러가 죽지 않고 응답이 왔다(빈 연결 아님).
    assert headers is not None


def test_json_content_type_has_charset(make_server):
    s = make_server()
    _code, _text, headers = _request(s["base"], "/api/runs")
    ctype = headers.get("Content-Type", "")
    assert "application/json" in ctype
    assert "charset=utf-8" in ctype


# ---------------------------------------------------------------------------
# #12: /api/state 가 board _corrupt 를 corrupt 로 노출
# ---------------------------------------------------------------------------
def test_state_exposes_board_corrupt(make_server):
    s = make_server()
    m = s["manager"]
    orch = m.project_dir("corrupt-1") / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "board.json").write_text("{not valid json", encoding="utf-8")  # 손상
    code, text, _ = _request(s["base"], "/api/state?run=corrupt-1")
    assert code == 200, text
    j = json.loads(text)
    assert j["corrupt"] is True


def test_state_not_corrupt_for_valid_board(make_server):
    s = make_server()
    m = s["manager"]
    orch = m.project_dir("ok-1") / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "board.json").write_text(
        json.dumps({"phase": "design", "agents": {}}), encoding="utf-8"
    )
    code, text, _ = _request(s["base"], "/api/state?run=ok-1")
    assert code == 200, text
    assert json.loads(text)["corrupt"] is False


# ---------------------------------------------------------------------------
# #6: is_running liveness TTL 캐시 — 같은 pidfile 에 대해 _run_alive 를 매번 부르지 않음
# ---------------------------------------------------------------------------
def test_is_running_caches_liveness(tmp_path, monkeypatch):
    m = webui.RunManager(tmp_path / "runs")
    orch = m.project_dir("cache-1") / ".orchestrator"
    orch.mkdir(parents=True)
    (orch / "run.pid").write_text("1\n", encoding="utf-8")  # 양의 정수(없는 PID 일 수 있음)

    calls = {"n": 0}
    real = webui._run_alive

    def counting(orch_dir):
        calls["n"] += 1
        return real(orch_dir)

    monkeypatch.setattr(webui, "_run_alive", counting)
    # 짧은 시간 내 여러 번 호출해도 _run_alive 는 한 번만 평가된다(TTL 캐시).
    for _ in range(5):
        m.is_running("cache-1")
    assert calls["n"] == 1, f"_run_alive 가 {calls['n']}회 호출됨(캐시 미적용)"


def test_alive_cache_invalidated_when_pidfile_changes(tmp_path, monkeypatch):
    m = webui.RunManager(tmp_path / "runs")
    orch = m.project_dir("cache-2") / ".orchestrator"
    orch.mkdir(parents=True)
    pf = orch / "run.pid"
    pf.write_text("1\n", encoding="utf-8")

    calls = {"n": 0}
    real = webui._run_alive

    def counting(orch_dir):
        calls["n"] += 1
        return real(orch_dir)

    monkeypatch.setattr(webui, "_run_alive", counting)
    m.is_running("cache-2")  # 1회
    # pidfile 내용/메타데이터를 바꾸면 캐시 키가 바뀌어 재평가된다.
    pf.write_text("2\n22\n", encoding="utf-8")
    import os as _os

    st = pf.stat()
    _os.utime(pf, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    m.is_running("cache-2")  # 키 변경 → 재평가
    assert calls["n"] == 2
