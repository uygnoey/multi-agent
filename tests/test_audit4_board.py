"""감사 발견사항 #7/#8/#11 회귀 테스트: deps 안전화·전역 산출물 이스케이프·음수 비용 차단.

전부 결정적·오프라인이며 tmp_path 아래에만 파일을 쓴다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.board import Board


def _run(coro):
    return asyncio.run(coro)


# ---- #7: deps 도 unit id 와 동일하게 _safe_unit_id 로 안전화 ----
def test_add_units_sanitizes_deps_like_ids(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # id "U/1" 은 "U-1" 로 안전화됨. dep "U/1" 도 동일하게 안전화되어 매칭되어야 함.
        await b.add_units(
            [
                {"id": "U/1", "title": "first"},
                {"id": "U2", "title": "second", "deps": ["U/1"]},
            ]
        )
        return b

    b = _run(scenario())
    units = {u["id"]: u for u in b.units()}
    # id 가 안전화됨
    assert "U-1" in units
    # dep 도 동일하게 안전화되어 실제 unit id 와 매칭됨 (unknown dep 으로 막히지 않음)
    assert units["U2"]["deps"] == ["U-1"]


def test_add_units_drops_empty_deps_after_sanitize(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # "///" 같은 항목은 안전화 후 빈 문자열 → drop 되어야 함
        await b.add_units([{"id": "U1", "title": "t", "deps": ["U/2", "///", "", "U 3"]}])
        return b

    b = _run(scenario())
    deps = b.units()[0]["deps"]
    # "U/2"→"U-2", "U 3"→"U-3", 빈 항목은 drop
    assert deps == ["U-2", "U-3"]


def test_add_units_dep_matches_sanitized_id_unblocks(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units(
            [
                {"id": "feat/login", "title": "login"},
                {"id": "feat/profile", "title": "profile", "deps": ["feat/login"]},
            ]
        )
        return b

    b = _run(scenario())
    units = {u["id"]: u for u in b.units()}
    # 양쪽 id 가 안전화되고, dep 도 안전화되어 알려진 id 집합에 포함됨
    known = set(units)
    profile_deps = units["feat-profile"]["deps"]
    assert profile_deps == ["feat-login"]
    assert all(dep in known for dep in profile_deps)


# ---- #8: 전역(설계·공통) 산출물도 마크다운 이스케이프 ----
def test_deliverables_escapes_global_artifacts_en_and_ko(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        # 검증을 우회해 개행/파이프를 포함한 전역 산출물을 직접 주입
        b._data["artifacts"] = ["docs/a.md", "evil|pipe", "line1\nline2"]
        return b

    b = _run(scenario())
    b.write_deliverables()
    en = (tmp_path / "docs" / "DELIVERABLES.md").read_text(encoding="utf-8")
    ko = (tmp_path / "docs" / "DELIVERABLES.ko.md").read_text(encoding="utf-8")
    for text in (en, ko):
        # 파이프가 이스케이프되어 표/구조를 깨지 않음
        assert "evil\\|pipe" in text
        # 개행이 중화되어 별도 줄로 새지 않음
        assert "line1 line2" in text
        # 원시 파이프/개행이 그대로 남지 않음
        assert "- evil|pipe" not in text


def test_write_report_has_no_raw_global_artifact_section(tmp_path: Path):
    # write_report 는 전역 산출물 섹션을 별도로 emit 하지 않음을 회귀로 고정.
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        b._data["artifacts"] = ["evil|pipe", "line1\nline2"]
        return b

    b = _run(scenario())
    text = b.write_report().read_text(encoding="utf-8")
    # 전역 산출물이 리포트 본문에 원시로 누출되지 않음
    assert "evil|pipe" not in text
    assert "line1\nline2" not in text


# ---- #11: 음수 비용은 무시(비용은 절대 감소하지 않음) ----
def test_add_cost_ignores_negative(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_cost(2.0)
        await b.add_cost(-1.0)  # 음수: 무시되어야 함
        await b.add_cost(-0.0001)
        await b.add_cost(0.5)
        return b

    b = _run(scenario())
    total = b.snapshot()["total_cost_usd"]
    # 음수가 차감되지 않고 양수만 누적됨
    assert total == 2.5


def test_add_cost_negative_never_decreases(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_cost(1.0)
        before = b.snapshot()["total_cost_usd"]
        await b.add_cost(-100.0)
        after = b.snapshot()["total_cost_usd"]
        return before, after

    before, after = _run(scenario())
    assert after == before == 1.0


def test_agent_update_cost_add_ignores_negative(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.agent_update("backend-developer", cost_add=2.0)
        await b.agent_update("backend-developer", cost_add=-1.0)  # 무시
        return b

    b = _run(scenario())
    a = b.agents()["backend-developer"]
    # per-agent 비용도 음수로 감소하지 않음
    assert a["cost_usd"] == 2.0
