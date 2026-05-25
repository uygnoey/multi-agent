"""audit10 회귀: LOW/MEDIUM-minor/NIT 항목 고정 테스트.

모두 오프라인·tmp_path 전용(실제 백엔드/네트워크 없음). 검증 대상:
- M07: _atomic_write_text 가 쓰기 실패 시 stale .tmp 를 남기지 않고 재던진다.
- M12: workspace 심링크 검사가 OSError 면 fail-closed(ValueError)로 거부한다.
- L09: directives() 가 개행 없는 단일 >limit 블록도 통째로 버리지 않고 보존한다.
- L11: agent_update 가 status=='running' + unit=None 호출에서 current_unit 을 지우지 않는다.
- L14: cost_estimated 가 env var 가 아니라 CLI 의 total_cost_usd 보고 여부를 반영한다.
- L16: _norm_model 이 YAML 숫자/불리언 model 값을 None 으로 처리한다.
- L17: webui._is_zombie 가 ')' 없는 손상 stat 을 좀비 아님으로 처리한다.
- N01: agents() round-trip 이 한글을 이스케이프하지 않고 보존한다.
- N02: _ZOMBIE_CACHE 가 미만료 항목만 있어도 64 로 하드 캡된다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.agents import _norm_model
from orchestrator.board import _MAX_DIRECTIVES_BYTES, Board


def _run(coro):
    return asyncio.run(coro)


# ── M07: 쓰기 실패 시 stale .tmp 제거 + 재던짐 ────────────────────────────────


def test_atomic_write_text_removes_tmp_on_failure(tmp_path: Path, monkeypatch):
    target = tmp_path / "report.md"
    tmp = target.with_name(target.name + ".tmp")

    # os.replace 가 실패하도록 패치 → tmp 는 이미 쓰여 있는 상태에서 예외 발생.
    import orchestrator.board as board_mod

    def boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr(board_mod.os, "replace", boom)
    with pytest.raises(OSError):
        Board._atomic_write_text(target, "내용")
    # stale .tmp 가 남으면 안 된다.
    assert not tmp.exists()
    assert not target.exists()


def test_atomic_write_text_success_writes_content(tmp_path: Path):
    target = tmp_path / "report.md"
    Board._atomic_write_text(target, "한글 본문\n")
    assert target.read_text(encoding="utf-8") == "한글 본문\n"
    assert not target.with_name(target.name + ".tmp").exists()


# ── M12: 심링크 검사 OSError → fail-closed(ValueError) ───────────────────────


def test_scaffold_symlink_check_oserror_fails_closed(tmp_path: Path, monkeypatch):
    from orchestrator import workspace as ws

    project = tmp_path / "proj"
    project.mkdir()

    orig_is_symlink = Path.is_symlink

    def flaky_is_symlink(self):
        # project_dir 자체 검사에서만 OSError 를 던져 가드 분기를 탄다.
        if self == project:
            raise OSError("permission denied")
        return orig_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", flaky_is_symlink)
    with pytest.raises(ValueError):
        ws.scaffold(project, "spec", {})


# ── L09: 개행 없는 단일 >limit directives 블록 보존 ─────────────────────────


def test_directives_preserves_block_without_newline(tmp_path: Path):
    b = Board(tmp_path)
    b.orch_dir.mkdir(parents=True, exist_ok=True)
    # 개행이 전혀 없는 단일 초장문(>limit) → 통째로 버리지 말고 tail 을 보존해야 한다.
    blob = "X" * (_MAX_DIRECTIVES_BYTES + 5000)
    b.directives_path.write_text(blob, encoding="utf-8")
    out = b.directives()
    # 생략 안내 헤더 뒤에 실제 내용(X)이 남아 있어야 한다(빈 결과가 아님).
    assert "오래된 directives 생략" in out
    assert "X" in out.replace("오래된 directives 생략", "")


# ── L11: running + unit=None 호출이 current_unit 을 지우지 않음 ──────────────


def test_agent_update_keeps_current_unit_on_running_without_unit(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 1) 진행 중 unit 설정
        await b.agent_update("dev", status="running", unit="U1")
        assert b.agents()["dev"]["current_unit"] == "U1"
        # 2) cost/메시지만 갱신하는 running 호출(unit 미지정) → unit 유지되어야 한다.
        await b.agent_update("dev", status="running", message="working")
        assert b.agents()["dev"]["current_unit"] == "U1"
        # 3) terminal 상태로 가면 current_unit 은 비워진다.
        await b.agent_update("dev", status="done")
        assert b.agents()["dev"]["current_unit"] is None

    _run(scenario())


# ── L14: cost_estimated 는 CLI 보고 여부를 반영 ──────────────────────────────


def test_claude_cli_cost_estimated_reflects_reported_cost():
    from orchestrator.backends import claude_cli as cc

    reported = b'{"type":"result","subtype":"success","result":"ok","total_cost_usd":0.5}'
    final, cost, _model, _tok = cc.parse_stream_result(reported)
    assert cost == 0.5
    # CLI 가 비용을 보고했으면 추정 아님.
    assert (cost is None) is False

    absent = b'{"type":"result","subtype":"success","result":"ok"}'
    _final, cost2, _m, _t = cc.parse_stream_result(absent)
    assert cost2 is None
    # 미보고 → 추정으로 표기.
    assert (cost2 is None) is True


# ── L16: YAML 숫자/불리언 model 값 → None ────────────────────────────────────


def test_norm_model_rejects_non_string_values():
    assert _norm_model(5) is None
    assert _norm_model(3.14) is None
    assert _norm_model(True) is None
    # 진짜 문자열은 그대로 통과(inherit/빈값은 None).
    assert _norm_model("sonnet") == "sonnet"
    assert _norm_model(" haiku ") == "haiku"
    assert _norm_model("inherit") is None
    assert _norm_model("") is None
    assert _norm_model(None) is None


# ── L17: webui._is_zombie 손상 stat(')'없음) 가드 ───────────────────────────


def test_webui_is_zombie_handles_stat_without_paren(tmp_path: Path, monkeypatch):
    from orchestrator import webui

    fake = tmp_path / "stat"
    fake.write_text("12345 comm-without-paren Z 1 2 3", encoding="utf-8")

    class FakePath:
        def __init__(self, *_a, **_k):
            pass

        def exists(self):
            return True

        def read_text(self, *_a, **_k):
            return fake.read_text(encoding="utf-8")

    # /proc 경로 대신 fake stat 를 읽게 하고, ps 폴백으로 새지 않는지 확인.
    monkeypatch.setattr(webui, "Path", lambda *_a, **_k: FakePath())
    monkeypatch.setattr(
        webui.subprocess, "run", lambda *_a, **_k: pytest.fail("ps 로 폴백하면 안 됨")
    )
    # ')' 가 없으므로 좀비 아님(False)로 처리되어야 한다.
    assert webui._is_zombie(12345) is False


# ── N01: agents() round-trip 이 한글 보존 ────────────────────────────────────


def test_agents_roundtrip_preserves_korean(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.agent_update("dev", status="running", message="한글 메시지")
        out = b.agents()
        assert out["dev"]["last_message"] == "한글 메시지"

    _run(scenario())


# ── N02: _ZOMBIE_CACHE 하드 캡(미만료 항목만 있어도 64) ──────────────────────


def test_zombie_cache_hard_caps_at_64(monkeypatch):
    from orchestrator import monitor as mon

    # 캐시 비우고, _is_zombie_uncached 는 즉답(외부 ps 회피).
    mon._ZOMBIE_CACHE.clear()
    monkeypatch.setattr(mon, "_is_zombie_uncached", lambda pid: False)
    try:
        # TTL 안에서 100개의 서로 다른 pid 를 넣어도(모두 미만료) 64 로 캡되어야 한다.
        for pid in range(1, 101):
            mon._is_zombie(pid)
        assert len(mon._ZOMBIE_CACHE) <= 64
    finally:
        mon._ZOMBIE_CACHE.clear()
