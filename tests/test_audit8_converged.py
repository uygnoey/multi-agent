"""교차검증(2026-05-24) 확정 audit8 회귀 테스트.

Claude+Codex 양측이 합의한 수정 6건:
  1) OpenAI run_bash 권한 2-tier: 기본=프로젝트 폴더 OS 샌드박스, --full-access=머신 전역.
  2) OpenAI read/edit 디렉터리 대상 fd 누수.
  3) board add_units 충돌-rename 시 deps raw→final 오결합.
  4) scheduler test_tasks finally 미정리(외부 cancel/예외 경로 고아 태스크).
  5) 손상 board(agents 비-dict / units·artifacts 비-list) 소비자 크래시 → _read_board 스키마 보정.
  6) webui 토큰 쿠키-불가 문자 401 루프 → percent-encode round-trip.

모두 offline·mock 전용이며 tmp_path/임시 포트 아래에서만 동작한다.
"""

from __future__ import annotations

import asyncio
import http.client
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from orchestrator import webui
from orchestrator.backends.openai_agents import (
    _bash_command_spec,
    _macos_sandbox_profile,
    _read_file_bytes_under_root,
    _run_bash_command,
)
from orchestrator.board import Board
from orchestrator.config import RunConfig
from orchestrator.monitor import _coerce_board_schema, _read_board
from orchestrator.scheduler import Scheduler

_HAS_SBX = sys.platform == "darwin" and bool(shutil.which("sandbox-exec"))


def _run(coro):
    return asyncio.run(coro)


# ===========================================================================
# 1) run_bash 권한 2-tier
# ===========================================================================
def test_bash_command_spec_full_access_is_raw():
    # --full-access: 샌드박스 없이 머신 전역 그대로 실행.
    argv, note = _bash_command_spec("echo hi", "/proj", full_access=True)
    assert argv == ["/bin/sh", "-c", "echo hi"]
    assert note == ""


def test_bash_command_spec_default_confines_or_warns(tmp_path):
    # 기본(full_access=False): 가능한 플랫폼은 OS 샌드박스로 감싸고, 불가하면 경고 note.
    argv, note = _bash_command_spec("echo hi", str(tmp_path), full_access=False)
    if _HAS_SBX:
        assert argv[0] == "sandbox-exec"
        assert argv[1] == "-p"
        assert str(tmp_path) in argv[2]  # 프로파일에 프로젝트 루트가 쓰기 허용으로 포함
        assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]
        assert note == ""
    elif sys.platform.startswith("linux") and shutil.which("bwrap"):
        assert argv[0] == "bwrap"
        assert note == ""
    else:
        # 샌드박스 도구 없음 → best-effort 실행 + 경고
        assert argv == ["/bin/sh", "-c", "echo hi"]
        assert "샌드박스" in note


def test_macos_profile_escapes_and_denies_writes():
    prof = _macos_sandbox_profile('/weird/"path')
    assert "(deny file-write*)" in prof
    assert '\\"path' in prof  # 따옴표 이스케이프


@pytest.mark.skipif(not _HAS_SBX, reason="macOS sandbox-exec 전용")
def test_run_bash_default_confines_writes_to_project(tmp_path):
    # 기본 모드: 프로젝트 폴더 안 쓰기는 허용.
    out_in = _run_bash_command("echo y > inside.txt", str(tmp_path), 10, 65536, full_access=False)
    assert "[exit 0]" in out_in
    assert (tmp_path / "inside.txt").read_text().strip() == "y"

    # 프로젝트 폴더 밖($HOME) 쓰기는 샌드박스가 차단해야 한다.
    probe = Path.home() / ".__dev_crew_sbx_probe_audit8"
    if probe.exists():
        probe.unlink()
    try:
        out_out = _run_bash_command(
            f"echo x > {probe}", str(tmp_path), 10, 65536, full_access=False
        )
        assert not probe.exists(), "샌드박스가 프로젝트 밖 쓰기를 막지 못했다"
        assert "[exit 0]" not in out_out  # 쓰기 실패로 비정상 종료
    finally:
        if probe.exists():
            probe.unlink()


@pytest.mark.skipif(not _HAS_SBX, reason="macOS sandbox-exec 전용")
def test_run_bash_full_access_can_write_outside_project(tmp_path):
    # --full-access: 머신 전역 → 프로젝트 밖($HOME)에도 쓸 수 있다(샌드박스 미적용 증명).
    probe = Path.home() / ".__dev_crew_full_probe_audit8"
    if probe.exists():
        probe.unlink()
    try:
        out = _run_bash_command(f"echo z > {probe}", str(tmp_path), 10, 65536, full_access=True)
        assert "[exit 0]" in out
        assert probe.exists()
    finally:
        if probe.exists():
            probe.unlink()


# ===========================================================================
# 2) read/edit 디렉터리 대상 fd 누수
# ===========================================================================
def _open_fd_count() -> int:
    for d in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(d):
            try:
                return len(os.listdir(d))
            except OSError:
                return -1
    return -1


def test_read_file_bytes_directory_does_not_leak_fd(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    base = _open_fd_count()
    if base < 0:
        pytest.skip("이 플랫폼에서 열린 fd 수를 셀 수 없음")
    for _ in range(5):  # 누수가 있으면 5회 반복으로 누적되어 드러난다
        with pytest.raises((IsADirectoryError, OSError)):
            _read_file_bytes_under_root(d, tmp_path, 1000)
    after = _open_fd_count()
    assert after <= base, f"fd 누수 의심: before={base} after={after}"


# ===========================================================================
# 3) board deps 충돌-rename raw→final 매핑
# ===========================================================================
def test_add_units_collision_dep_remaps_to_renamed_unit(tmp_path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # "U/1" 과 "U.1" 은 둘 다 canonical "U-1" 로 sanitize → 둘째는 "U-1-2" 로 rename.
        await b.add_units(
            [
                {"id": "U/1", "title": "first"},
                {"id": "U.1", "title": "second"},
                {"id": "C1", "title": "dep-on-second", "deps": ["U.1"]},
                {"id": "C2", "title": "dep-on-first", "deps": ["U/1"]},
            ]
        )
        return b

    b = _run(scenario())
    units = {u["id"]: u for u in b.units()}
    assert "U-1" in units and "U-1-2" in units  # 충돌 보존(rename)
    # 둘째 unit("U.1" raw)을 가리킨 dep 는 rename 된 "U-1-2" 에 묶여야 한다(첫 unit 아님).
    assert units["C1"]["deps"] == ["U-1-2"]
    # 첫 unit("U/1" raw)을 가리킨 dep 는 "U-1".
    assert units["C2"]["deps"] == ["U-1"]


def test_add_units_canonical_dep_still_resolves(tmp_path):
    # canonical 형태로 적힌 dep 는 첫 소유자(U-1)로 해석된다(회귀 방지).
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units(
            [
                {"id": "U1", "title": "a"},
                {"id": "U2", "title": "b", "deps": ["U1"]},
            ]
        )
        return b

    b = _run(scenario())
    units = {u["id"]: u for u in b.units()}
    assert units["U2"]["deps"] == ["U1"]


# ===========================================================================
# 4) scheduler test_tasks finally 정리
# ===========================================================================
def test_finally_cancels_pending_test_tasks_on_external_cancel(tmp_path, sample_spec_path):
    async def scenario():
        cfg = RunConfig(
            spec_path=sample_spec_path.resolve(),
            project_dir=tmp_path / "p",
            mock=True,
            poll_interval=600.0,
        )
        sched = Scheduler(cfg)
        started = asyncio.Event()
        captured: dict[str, asyncio.Task] = {}

        async def hang(unit, sem):  # test/qa 태스크가 영원히 pending 으로 남도록
            captured["task"] = asyncio.current_task()
            started.set()
            await asyncio.Event().wait()

        sched._test_unit_safe = hang  # type: ignore[method-assign]
        run_task = asyncio.create_task(sched.run())
        await asyncio.wait_for(started.wait(), timeout=30)  # 첫 test_task 생성 대기
        run_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run_task
        return captured.get("task")

    task = _run(scenario())
    assert task is not None, "test_task 가 생성되지 않음"
    assert task.done(), "finally 가 pending test_task 를 정리하지 않음(고아)"
    assert task.cancelled()


# ===========================================================================
# 5) 손상 board 스키마 보정 (_read_board / _coerce_board_schema)
# ===========================================================================
def test_coerce_board_schema_fixes_wrong_types():
    data = _coerce_board_schema(
        {
            "agents": ["not", "a", "dict"],  # 비-dict
            "units": "nope",  # 비-list
            "artifacts": {"bad": 1},  # 비-list
        }
    )
    assert data["agents"] == {}
    assert data["units"] == []
    assert data["artifacts"] == []


def test_coerce_board_schema_filters_nondict_units_and_unit_artifacts():
    data = _coerce_board_schema(
        {
            "units": [
                {"id": "U1", "artifacts": "x.py"},  # unit.artifacts 비-list
                "garbage",  # 비-dict unit
                {"id": "U2", "artifacts": ["ok.py"]},
            ]
        }
    )
    ids = [u["id"] for u in data["units"]]
    assert ids == ["U1", "U2"]  # 비-dict 제거
    assert data["units"][0]["artifacts"] == []  # 비-list → []
    assert data["units"][1]["artifacts"] == ["ok.py"]


def test_read_board_corrupt_agents_does_not_crash_consumers(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir(parents=True)
    # agents 가 dict 가 아닌 손상 board.json
    (orch / "board.json").write_text(
        '{"phase":"build","agents":[1,2,3],"units":"bad","artifacts":"bad"}',
        encoding="utf-8",
    )
    board = _read_board(orch)
    # webui /api/state·/api/agent, monitor 가 하던 접근이 더 이상 터지지 않아야 한다.
    assert isinstance(board.get("agents", {}), dict)
    assert "x" not in board.get("agents", {})  # in 연산 안전
    assert list(board.get("agents", {}).values()) == []
    assert board.get("agents", {}).get("backend-developer", {}) == {}
    assert board["units"] == []
    assert board["artifacts"] == []


# ===========================================================================
# 6) webui 토큰 쿠키-불가 문자 round-trip (401 루프 방지)
# ===========================================================================
@pytest.fixture
def make_server(tmp_path):
    import threading
    from http.server import ThreadingHTTPServer

    servers = []

    def _make(token=None):
        manager = webui.RunManager(tmp_path / f"runs{len(servers)}", spawn=lambda c, p: None)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, token))
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return {"port": port}

    yield _make
    for h in servers:
        h.shutdown()
        h.server_close()


def test_token_with_cookie_unsafe_chars_roundtrips(make_server):
    # ASCII 지만 쿠키-불가 문자(공백·;·:·@)가 섞인 토큰: ?token= 인증은 통과하나 예전 쿠키
    # 정규식([A-Za-z0-9._~+/=-])은 미통과라 Set-Cookie 가 안 심겨 401 루프가 났다.
    # (non-ascii 토큰은 hmac.compare_digest 제약으로 애초에 인증 불가 → 별개의 fail-closed 케이스)
    token = "tok en;a:b@c"
    s = make_server(token=token)

    # 1) ?token=<encoded> 접속 → 303 + percent-encoded Set-Cookie (raw 공백/; 없음)
    conn = http.client.HTTPConnection("127.0.0.1", s["port"])
    conn.request("GET", "/?token=" + quote(token, safe=""))
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 303, "쿠키-불가 문자 토큰도 Set-Cookie + redirect 되어야 함"
    cookie = resp.getheader("Set-Cookie") or ""
    cookie_pair = cookie.split(";", 1)[0]
    assert cookie_pair.startswith("token=")
    raw = cookie_pair[len("token=") :]
    assert " " not in raw and ";" not in raw  # 유효한 단일 쿠키 값
    assert unquote(raw) == token  # round-trip 으로 원본 복원

    # 2) 그 쿠키로 인증된 GET → 401 루프가 아니라 200
    conn2 = http.client.HTTPConnection("127.0.0.1", s["port"])
    conn2.request("GET", "/api/state", headers={"Cookie": cookie_pair})
    resp2 = conn2.getresponse()
    resp2.read()
    assert resp2.status == 200, "round-trip 쿠키로 인증되어야 함(401 루프 아님)"


def test_token_equal_supports_non_ascii():
    # 예전엔 hmac.compare_digest 가 non-ascii str 을 거부해 무조건 False(인증 불가)였다.
    from orchestrator.webui import _token_equal

    assert _token_equal("토큰값🔑", "토큰값🔑") is True
    assert _token_equal("토큰값🔑", "다른값") is False
    assert _token_equal("", "x") is False
    assert _token_equal("x", "") is False
    assert _token_equal("ascii-tok", "ascii-tok") is True


def test_non_ascii_token_authenticates_and_roundtrips(make_server):
    # non-ascii WEB_UI_TOKEN 도 ?token= 인증 → 303 + 쿠키 → 쿠키 인증 200 까지 동작.
    token = "한국어토큰🔑"
    s = make_server(token=token)

    conn = http.client.HTTPConnection("127.0.0.1", s["port"])
    conn.request("GET", "/?token=" + quote(token, safe=""))
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 303
    cookie_pair = (resp.getheader("Set-Cookie") or "").split(";", 1)[0]
    assert cookie_pair.startswith("token=")
    raw = cookie_pair[len("token=") :]
    raw.encode("ascii")  # 쿠키 값은 percent-encode 되어 순수 ASCII 여야 한다(헤더 안전)
    assert unquote(raw) == token

    conn2 = http.client.HTTPConnection("127.0.0.1", s["port"])
    conn2.request("GET", "/api/state", headers={"Cookie": cookie_pair})
    resp2 = conn2.getresponse()
    resp2.read()
    assert resp2.status == 200
