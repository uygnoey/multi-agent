"""감사 13차 회귀 테스트: Claude↔Codex 교차검증 합의 5건 수정.

모두 결정적·오프라인이며 tmp_path 아래에만 파일을 쓴다.

- #9  result 파일이 symlink 면 _read_result 가 따라가지 않고 계약 위반(실패)으로 처리.
- #17 webui JSON 응답이 NaN/Infinity 를 비표준 JSON 으로 내보내지 않는다(allow_nan=False → 500).
- #20 board 가 런어웨이 길이 title/description/artifact 를 캡한다.
- #21 board.set_status 가 상태 머신에 없는 값을 거부(미적용)한다.
- #5  빈/공백 spec 으로 scaffold 시 .orchestrator/spec.md 포인터가 dangling 되지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import webui
from orchestrator.backends.base import RoleResult
from orchestrator.board import (
    _MAX_ARTIFACT_CHARS,
    _MAX_DESCRIPTION_CHARS,
    _MAX_TITLE_CHARS,
    DESIGNED,
    DONE,
    Board,
)
from orchestrator.runner import Runner
from orchestrator.workspace import scaffold

STACK = {"frontend": "react", "backend": "fastapi", "db": "postgres"}


def _run(coro):
    return asyncio.run(coro)


# ----------------- #9: result 파일 symlink 차단 -----------------
def test_read_result_rejects_symlinked_result_file(tmp_path: Path):
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"status": "done", "_ok": True, "artifacts": ["leaked"]}))
    link = tmp_path / "role__key.json"
    link.symlink_to(real)
    res = RoleResult(ok=True)
    out = Runner._read_result(
        link, res, result_required=True, phase="dev", role="frontend-developer"
    )
    assert out["_ok"] is False
    assert any("symlink" in b for b in out["blockers"])
    # 외부 파일의 artifacts 가 결과로 새지 않아야 한다
    assert out["artifacts"] == []


def test_read_result_accepts_regular_result_file(tmp_path: Path):
    p = tmp_path / "role__key.json"
    p.write_text(json.dumps({"status": "dev_done", "artifacts": ["src/app.py"]}))
    res = RoleResult(ok=True)
    out = Runner._read_result(p, res, result_required=True, phase="dev", role="frontend-developer")
    assert out["_ok"] is True
    assert out["artifacts"] == ["src/app.py"]


# ----------------- #20: board 길이 캡 -----------------
def test_add_units_caps_runaway_title_and_description(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units(
            [{"id": "U1", "title": "X" * 100000, "description": "Y" * 100000, "roles": ["dba"]}]
        )
        return b.units()[0]

    u = _run(scenario())
    assert len(u["title"]) <= _MAX_TITLE_CHARS + 16
    assert len(u["description"]) <= _MAX_DESCRIPTION_CHARS + 16
    assert u["title"].startswith("X" * 100)
    assert "truncated" in u["title"]


def test_add_artifacts_caps_runaway_path(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t", "roles": ["dba"]}])
        await b.add_artifacts("U1", ["A" * 50000])
        return b.units()[0]

    u = _run(scenario())
    assert u["artifacts"], "artifact should be stored (capped), not dropped"
    assert len(u["artifacts"][0]) <= _MAX_ARTIFACT_CHARS


# ----------------- #21: set_status 화이트리스트 -----------------
def test_set_status_rejects_unknown_status(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t", "roles": ["dba"]}])
        await b.set_status("U1", "totally-bogus-status-xyz")
        bogus = b.units()[0]["status"]
        await b.set_status("U1", DONE)
        good = b.units()[0]["status"]
        return bogus, good

    bogus, good = _run(scenario())
    assert bogus == DESIGNED  # 잘못된 값은 적용되지 않음 (초기 상태 유지)
    assert good == DONE  # 유효한 값은 정상 적용


# ----------------- #5: 빈 spec 의 dangling spec.md 방지 -----------------
def test_blank_spec_writes_placeholder_when_absent(tmp_path: Path):
    target = tmp_path / "proj"
    scaffold(target, "   ", STACK)  # 공백뿐인 spec
    sp = target / ".orchestrator" / "spec.md"
    assert sp.exists(), "프롬프트가 가리키는 spec.md 가 존재해야 한다(dangling 방지)"
    assert sp.read_text(encoding="utf-8").strip()  # 비어있지 않음(플레이스홀더)


def test_blank_spec_preserves_existing_spec_md(tmp_path: Path):
    target = tmp_path / "proj"
    scaffold(target, "real spec body", STACK)
    sp = target / ".orchestrator" / "spec.md"
    assert sp.read_text(encoding="utf-8") == "real spec body"
    # 같은 디렉터리를 빈 spec 으로 재스캐폴딩 → 기존 정상 spec 보존
    scaffold(target, "", STACK)
    assert sp.read_text(encoding="utf-8") == "real spec body"


# ----------------- #17: webui JSON NaN 비방출 -----------------
@pytest.fixture
def make_server(tmp_path):
    servers = []

    def _make(token=None):
        manager = webui.RunManager(tmp_path / f"runs{len(servers)}")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, token))
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        servers.append(httpd)
        return {"base": f"http://127.0.0.1:{port}", "manager": manager}

    yield _make
    for h in servers:
        h.shutdown()
        h.server_close()


def _get(base, path):
    req = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_api_state_does_not_emit_nan(make_server):
    s = make_server()
    # 손상된 board.json(비표준 NaN 토큰)을 디스크에 심는다.
    run_dir = s["manager"].base_dir / "r1" / ".orchestrator"
    run_dir.mkdir(parents=True)
    (run_dir / "board.json").write_text('{"total_cost_usd": NaN, "agents": {}, "units": []}')
    code, body = _get(s["base"], "/api/state?run=r1")
    # 수정 후: allow_nan=False 로 직렬화 실패 → 제네릭 500 (비표준 NaN 을 클라이언트로 보내지 않음)
    assert code == 500
    assert "NaN" not in body
