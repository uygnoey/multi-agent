"""round-6 회귀: 웹 UI 보안 강화.

- #8  serve(): 인증 없이 비-루프백(0.0.0.0 등) 바인딩 시 fail-closed 로 기동 거부.
- #9  상태변경 POST 에 Origin 검사(cross-origin 차단 = CSRF 방어). 비-브라우저(Origin 없음) 허용.
- #10 토큰 쿠키에 HttpOnly 부여(JS/XSS 탈취 방지).

HTTP 엔드포인트는 임시 포트의 stdlib ThreadingHTTPServer 를 fake spawn 으로 띄워 검증한다.
"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from orchestrator import webui


# ---------------- #8: serve() fail-closed ----------------
def test_serve_refuses_non_loopback_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_UI_TOKEN", raising=False)
    # 0.0.0.0 + 무토큰 → fail-closed: 기동 전에 SystemExit (serve_forever 진입 전).
    with pytest.raises(SystemExit):
        webui.serve(port=0, base_dir=tmp_path / "runs", host="0.0.0.0")


def test_serve_refuses_external_ip_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv("WEB_UI_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        webui.serve(port=0, base_dir=tmp_path / "runs", host="0.0.0.0")


# ---------------- harness ----------------
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


# ---------------- #9: Origin 검사 ----------------
def test_post_without_origin_allowed(make_server):
    # 비-브라우저(Origin 헤더 없음) → 허용(CSRF 대상 아님).
    s = make_server()
    code, text, _ = _request(s["base"], "/api/run", "POST", {"spec_text": "# s", "backend": "mock"})
    assert code == 200, text


def test_post_same_origin_allowed(make_server):
    s = make_server()
    origin = f"http://127.0.0.1:{s['port']}"
    code, text, _ = _request(
        s["base"], "/api/run", "POST", {"spec_text": "# s", "backend": "mock"}, {"Origin": origin}
    )
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


def test_cross_origin_blocked_before_auth(make_server):
    # cross-origin 은 토큰 유무와 무관하게 먼저 차단된다(403, 401 아님).
    s = make_server(token="SECRET")
    code, _t, _ = _request(
        s["base"],
        "/api/stop",
        "POST",
        {"run": "x"},
        {"Origin": "http://evil.example", "X-Auth-Token": "SECRET"},
    )
    assert code == 403


# ---------------- #10: HttpOnly 쿠키 ----------------
def test_token_cookie_is_httponly(make_server):
    s = make_server(token="SECRET")
    conn = http.client.HTTPConnection("127.0.0.1", s["port"])
    conn.request("GET", "/?token=SECRET")
    resp = conn.getresponse()
    assert resp.status == 303
    cookie = resp.getheader("Set-Cookie") or ""
    assert "token=SECRET" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
