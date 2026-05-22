"""감사 5차(2026-05-22) 회귀 테스트: orchestrator/webui.py.

- #8/#9 /api/run 숫자 검증이 NaN/Inf 를 거부(이전엔 fv<0 만 봐서 통과했다).
- #17   WEB_UI_TOKEN 설정 시 토큰 인증(헤더/쿼리/쿠키), 미설정 시 하위호환.
- #20   _read_events / _read_agent_logs 가 seek-tail 로 마지막 n 줄만 반환.
- #23   INDEX_HTML 이 /api/state 오류를 사용자에게 표시(showErr)한다.

HTTP 엔드포인트는 임시 포트의 stdlib ThreadingHTTPServer 를 fake spawn 으로 띄워 검증한다
(오프라인·결정적, 실제 백엔드/네트워크 없음).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from orchestrator import webui


@pytest.fixture
def make_server(tmp_path):
    """token 을 지정해 핸들러를 띄우는 팩토리. 여러 서버를 한 테스트에서 만들 수 있다."""
    servers = []

    def _make(token=None):
        spawned = []

        def fake_spawn(cmd, log_path):
            spawned.append(cmd)

            class _P:
                pid = 44444

                def poll(self):
                    return None

                def wait(self):
                    return 0

            return _P()

        manager = webui.RunManager(tmp_path / f"runs{len(servers)}", spawn=fake_spawn)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, token))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        servers.append(httpd)
        return {"base": f"http://127.0.0.1:{port}", "manager": manager, "spawned": spawned}

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


def _post(base, path, body, headers=None):
    code, text, _ = _request(base, path, "POST", body, headers)
    return code, json.loads(text)


# ----------------- #8/#9: NaN/Inf 숫자 거부 -----------------
@pytest.mark.parametrize("fld", ["budget", "poll_interval", "timeout"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_run_rejects_non_finite(make_server, fld, bad):
    s = make_server()
    code, j = _post(s["base"], "/api/run", {"spec_text": "# s", "backend": "mock", fld: bad})
    assert code == 400, j
    assert "finite" in j["error"]


def test_run_accepts_finite_budget(make_server):
    # 회귀: 정상 유한값은 그대로 통과.
    s = make_server()
    code, j = _post(s["base"], "/api/run", {"spec_text": "# s", "backend": "mock", "budget": 5.0})
    assert code == 200, j
    assert "run_id" in j


# ----------------- #17: 토큰 인증 -----------------
def test_no_token_server_allows_api(make_server):
    # WEB_UI_TOKEN 미설정(토큰 None) → 인증 비활성(하위호환).
    s = make_server(token=None)
    code, _text, _ = _request(s["base"], "/api/runs")
    assert code == 200


def test_token_server_blocks_without_token(make_server):
    s = make_server(token="SECRET")
    code, text, _ = _request(s["base"], "/api/runs")
    assert code == 401
    assert "unauthorized" in json.loads(text)["error"]


def test_token_via_query(make_server):
    s = make_server(token="SECRET")
    code, _t, _ = _request(s["base"], "/api/runs?token=SECRET")
    assert code == 200


def test_token_via_bearer_header(make_server):
    s = make_server(token="SECRET")
    code, _t, _ = _request(s["base"], "/api/runs", headers={"Authorization": "Bearer SECRET"})
    assert code == 200


def test_token_via_x_auth_header(make_server):
    s = make_server(token="SECRET")
    code, _t, _ = _request(s["base"], "/api/runs", headers={"X-Auth-Token": "SECRET"})
    assert code == 200


def test_token_via_cookie(make_server):
    s = make_server(token="SECRET")
    code, _t, _ = _request(s["base"], "/api/runs", headers={"Cookie": "token=SECRET"})
    assert code == 200


def test_wrong_token_rejected(make_server):
    s = make_server(token="SECRET")
    code, _t, _ = _request(s["base"], "/api/runs", headers={"Authorization": "Bearer NOPE"})
    assert code == 401


def test_post_requires_token(make_server):
    s = make_server(token="SECRET")
    code, j = _post(s["base"], "/api/run", {"spec_text": "# s", "backend": "mock"})
    assert code == 401
    # 인증 실패 시 spawn 이 일어나면 안 된다.
    assert s["spawned"] == []


def test_post_with_token_runs(make_server):
    s = make_server(token="SECRET")
    code, j = _post(
        s["base"], "/api/run", {"spec_text": "# s", "backend": "mock"}, {"X-Auth-Token": "SECRET"}
    )
    assert code == 200, j
    assert "run_id" in j


def test_index_is_ungated_and_sets_cookie_on_valid_token(make_server):
    s = make_server(token="SECRET")
    # index 는 비밀이 없는 정적 셸이라 토큰 없이도 200.
    code, _t, _ = _request(s["base"], "/")
    assert code == 200
    # 유효한 ?token= 로 접속하면 쿠키를 심어 이후 fetch 가 자동 인증된다.
    code, _t, hdrs = _request(s["base"], "/?token=SECRET")
    assert code == 200
    assert "token=SECRET" in (hdrs.get("Set-Cookie") or "")


# ----------------- #20: seek-tail -----------------
def test_read_events_tail(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir(parents=True)
    p = orch / "events.log"
    p.write_text("\n".join(f"e{i}" for i in range(100_000)) + "\n", encoding="utf-8")
    out = webui._read_events(orch, n=3)
    assert out.splitlines() == ["e99997", "e99998", "e99999"]


def test_read_agent_logs_tail(tmp_path):
    orch = tmp_path / ".orchestrator"
    ad = orch / "agents"
    ad.mkdir(parents=True)
    (ad / "qa.log").write_text("\n".join(f"L{i}" for i in range(50_000)) + "\n", encoding="utf-8")
    out = webui._read_agent_logs(orch, ["qa"], n=2)
    assert out["qa"].splitlines() == ["L49998", "L49999"]


# ----------------- #3: RunManager.stop 그룹 SIGKILL 스윕 -----------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_runmanager_stop_reaps_sigterm_ignoring_child(tmp_path):
    base = tmp_path / "runs"
    manager = webui.RunManager(base)
    run_id = "myrun"
    orch = base / run_id / ".orchestrator"
    orch.mkdir(parents=True)
    pidfile = tmp_path / "child.pid"
    child_py = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    parent_sh = f"{sys.executable} -c {child_py!r} &\nsleep 60\n"
    parent = subprocess.Popen(["/bin/sh", "-c", parent_sh], start_new_session=True)
    try:
        (orch / "run.pid").write_text(str(parent.pid), encoding="utf-8")
        deadline = time.time() + 3.0
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "자식이 PID 를 기록하지 못함 (테스트 전제 실패)"
        child_pid = int(pidfile.read_text().strip())

        assert manager.stop(run_id) is True

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
        try:
            os.killpg(os.getpgid(parent.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            parent.wait(timeout=2)
        except Exception:
            pass


# ----------------- #23: 프론트 오류 표시 -----------------
def test_index_html_has_error_banner_and_handler():
    html = webui.INDEX_HTML
    assert 'id="err"' in html  # 오류 배너 엘리먼트
    assert "function showErr" in html  # 토글 헬퍼
    assert "s.error" in html  # /api/state 오류 본문 검사
