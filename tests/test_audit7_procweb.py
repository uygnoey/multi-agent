"""감사 7차(2026-05-22) 보안 회귀 테스트: monitor _stop_run + webui Origin/slow-loris/힌트.

다루는 수정:
- monitor._stop_run / webui.RunManager.stop: PID/PGID 재사용 방어. 그룹 시그널 직전 원래
  리더가 아직 그 그룹을 이끄는지 재검증하고, 리더가 사라지면 그룹 시그널을 중단한다.
- webui._origin_ok: host 정규화 비교(대소문자/기본 포트) + 쿠키-only 인증 시 Origin 필수.
- webui Handler.timeout: slow-loris 방어용 소켓 read 타임아웃.
- INDEX_HTML: 401 힌트가 더 이상 "모든 /api/* 에 ?token= 을 붙이라" 고 안내하지 않음.

실제 프로세스 그룹을 띄우므로 결정적이도록 넉넉한 타임아웃·정리(cleanup)를 둔다.
"""

from __future__ import annotations

import http.client
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
from orchestrator.monitor import _stop_run


# ---------------------------------------------------------------------------
# monitor._stop_run: 살아있는 run 의 그룹을 SIGKILL 로 일소 (리더 생존 동안)
# ---------------------------------------------------------------------------
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_stop_run_reaps_sigterm_ignoring_child_while_leader_alive(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir(parents=True)
    pidfile = tmp_path / "child.pid"
    # 자식: SIGTERM 무시 + 오래 잠 → 그룹 SIGTERM 으로는 안 죽고 그룹 SIGKILL 만 통한다.
    child_py = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    # 부모(그룹 리더): 자식을 백그라운드로 띄우고 자신도 SIGTERM 을 무시한 채 살아있다.
    # → audit7 안전 로직이 "리더 생존" 을 확인하고 그룹 SIGKILL 을 보내 자식까지 일소한다.
    parent_py = (
        "import os, signal, subprocess, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"subprocess.Popen([sys.executable, '-c', {child_py!r}]); "
        "time.sleep(60)"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_py], start_new_session=True)
    try:
        (orch / "run.pid").write_text(str(parent.pid), encoding="utf-8")
        # 자식이 PID 를 기록할 때까지 잠깐 대기(테스트 전제).
        deadline = time.time() + 3.0
        while not pidfile.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert pidfile.exists(), "자식이 PID 를 기록하지 못함 (테스트 전제 실패)"
        child_pid = int(pidfile.read_text().strip())

        # stop: SIGTERM(무시됨) → supervisor 가 리더 생존 확인 후 그룹 SIGKILL 로 부모·자식 일소.
        assert _stop_run(orch) is True

        deadline = time.time() + 6.0
        while _pid_alive(child_pid) and time.time() < deadline:
            time.sleep(0.1)
        alive = _pid_alive(child_pid)
        if alive:  # 정리(좀비 방지)
            try:
                os.kill(child_pid, signal.SIGKILL)
            except Exception:
                pass
        assert not alive, (
            "SIGTERM 무시 자식이 stop 후에도 살아남음 (리더 생존 중 그룹 SIGKILL 누락)"
        )
    finally:
        # 부모/자식 정리(좀비 방지).
        try:
            os.killpg(os.getpgid(parent.pid), signal.SIGKILL)
        except Exception:
            pass
        try:
            parent.wait(timeout=3)
        except Exception:
            pass


def test_stop_run_no_pidfile_returns_false(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir(parents=True)
    # run.pid 가 없으면 stop 대상 없음 → False.
    assert _stop_run(orch) is False


# ---------------------------------------------------------------------------
# webui HTTP harness (test_audit6_webui.py 패턴 차용)
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
        return {"base": f"http://127.0.0.1:{port}", "port": port, "spawned": spawned}

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
# webui._origin_ok: 정규화 비교 + cross-origin 차단
# ---------------------------------------------------------------------------
def test_post_matching_origin_case_variant_not_blocked(make_server):
    # 대소문자만 다른 same-origin → 정규화 비교로 통과(403 아님).
    s = make_server()
    origin = f"http://127.0.0.1:{s['port']}".upper()  # scheme/host 대문자 변형
    code, text, _ = _request(
        s["base"], "/api/run", "POST", {"spec_text": "# s", "backend": "mock"}, {"Origin": origin}
    )
    assert code != 403, text
    assert code == 200, text


def test_post_cross_origin_blocked(make_server):
    s = make_server()
    code, text, _ = _request(
        s["base"],
        "/api/run",
        "POST",
        {"spec_text": "# s", "backend": "mock"},
        {"Origin": "http://evil.example"},
    )
    assert code == 403
    assert "cross-origin" in json.loads(text)["error"]
    # 차단된 요청은 run 을 만들지 않는다.
    assert s["spawned"] == []


def test_cookie_only_auth_without_origin_blocked(make_server):
    # 토큰 서버 + 쿠키-only 인증(헤더 토큰 없음) + Origin 부재 → CSRF 표적이므로 403.
    s = make_server(token="SECRET")
    code, _t, _ = _request(
        s["base"],
        "/api/stop",
        "POST",
        {"run": "x"},
        {"Cookie": "token=SECRET"},  # 쿠키만으로 인증, Origin 없음
    )
    assert code == 403


def test_header_auth_without_origin_allowed(make_server):
    # 헤더 토큰(X-Auth-Token) 인증 + Origin 부재 → 비-브라우저, CSRF 위조 불가 → 통과.
    s = make_server(token="SECRET")
    code, _t, _ = _request(
        s["base"],
        "/api/stop",
        "POST",
        {"run": "x"},
        {"X-Auth-Token": "SECRET"},  # 쿠키 없음
    )
    # 403(cross-origin) 도 아니고 401(unauthorized) 도 아니어야 한다 → stop 처리(200).
    assert code == 200, code


# ---------------------------------------------------------------------------
# slow-loris: Handler 클래스에 숫자 timeout 속성이 있는지
# ---------------------------------------------------------------------------
def test_handler_has_numeric_timeout(tmp_path):
    manager = webui.RunManager(tmp_path / "runs", spawn=lambda c, p: None)
    Handler = webui._make_handler(manager, None)
    assert isinstance(Handler.timeout, (int, float))
    assert Handler.timeout > 0


# ---------------------------------------------------------------------------
# INDEX_HTML: 401 힌트가 더 이상 모든 /api/* 에 ?token= 을 붙이라고 하지 않음
# ---------------------------------------------------------------------------
def test_index_html_token_hint_describes_cookie_flow():
    html = webui.INDEX_HTML
    # 예전의 잘못된 힌트("URL 에 ?token=… 를 추가하세요")가 사라졌는지.
    assert "URL 에 ?token=… 를 추가하세요" not in html
    # 새 힌트는 /?token=<TOKEN> 로 한 번 접속해 쿠키를 설정하는 흐름을 설명한다.
    assert "/?token=<TOKEN>" in html
    assert "쿠키" in html


# ---------------------------------------------------------------------------
# (부수) Set-Cookie 흐름이 여전히 동작하는지 — 토큰 서버에서 ?token= 으로 접속
# ---------------------------------------------------------------------------
def test_token_query_sets_cookie(make_server):
    s = make_server(token="SECRET")
    conn = http.client.HTTPConnection("127.0.0.1", s["port"])
    conn.request("GET", "/?token=SECRET")
    resp = conn.getresponse()
    assert resp.status == 303
    cookie = resp.getheader("Set-Cookie") or ""
    assert "token=SECRET" in cookie
