"""Round-7 audit fixes: stale docs aside, verify the runtime hardening paths."""

from __future__ import annotations

import asyncio
import http.client
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import webui
from orchestrator.backends import base as base_mod
from orchestrator.backends.claude_cli import claude_stream_line
from orchestrator.config import RunConfig


@pytest.fixture
def server(tmp_path):
    spawned = []

    def fake_spawn(cmd, log_path):
        spawned.append(cmd)

        class _P:
            pid = 55555

            def poll(self):
                return None

            def wait(self):
                return 0

        return _P()

    manager = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, "SECRET"))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, spawned
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(httpd, path, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json", "X-Auth-Token": "SECRET"}
    if headers:
        h.update(headers)
    url = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8"), e.headers


def test_token_query_sets_cookie_then_redirects_without_token(server):
    httpd, _spawned = server
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
    conn.request("GET", "/?token=SECRET")
    resp = conn.getresponse()
    assert resp.status == 303
    assert resp.getheader("Location") == "/"
    cookie = resp.getheader("Set-Cookie") or ""
    assert "token=SECRET" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie


def test_token_cookie_secure_when_forwarded_proto_https(server):
    httpd, _spawned = server
    conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1])
    conn.request("GET", "/?token=SECRET", headers={"X-Forwarded-Proto": "https"})
    resp = conn.getresponse()
    assert resp.status == 303
    assert "Secure" in (resp.getheader("Set-Cookie") or "")


def test_run_rejects_json_float_for_integer_option(server):
    httpd, spawned = server
    code, text, _headers = _request(
        httpd, "/api/run", {"spec_text": "# spec", "backend": "mock", "concurrency": 1.5}
    )
    assert code == 400
    assert "invalid concurrency" in json.loads(text)["error"]
    assert spawned == []


def test_dashboard_units_uses_array_guard():
    html = webui.INDEX_HTML
    assert "Array.isArray(b.units)" in html
    assert "(b.units||[]).forEach" not in html


def test_runconfig_malformed_max_units_limits_to_one(tmp_path):
    cfg = RunConfig(
        spec_path=Path("s.md"),
        project_dir=tmp_path / "p",
        max_units="not-an-int",  # type: ignore[arg-type]
    )
    assert cfg.max_units == 1


def test_bounded_buffer_caps_single_huge_chunk():
    buf = base_mod._BoundedBuffer(max_bytes=10)
    buf.append(b"A" * 100)
    assert buf.getvalue() == b"A" * 10
    assert buf.dropped is True


def test_run_subprocess_handles_huge_line_without_readline_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(base_mod, "_MAX_STREAM_BYTES", 4096)
    prog = "import sys; sys.stdout.write('A' * (2 * 1024 * 1024)); sys.stdout.flush()"
    rc, out, err, timed_out = asyncio.run(
        base_mod.run_subprocess([sys.executable, "-c", prog], tmp_path, 30)
    )
    assert rc == 0
    assert timed_out is False
    assert err == b""
    assert len(out) <= 4096


def test_claude_stream_redacts_thinking_and_truncates_tool_input():
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "tool_use", "name": "Write", "input": {"content": "x" * 5000}},
            ]
        },
    }
    rendered = claude_stream_line(json.dumps(line).encode())
    assert rendered is not None
    assert "secret reasoning" not in rendered
    assert "[thinking redacted]" in rendered
    assert "truncated" in rendered
