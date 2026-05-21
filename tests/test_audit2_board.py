"""감사 발견사항 #139 회귀 테스트: write_deliverables 가 에이전트 산출물을 덮어쓰지 않음.

전부 결정적·오프라인이며 tmp_path 아래에만 파일을 쓴다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.board import Board


def _run(coro):
    return asyncio.run(coro)


# ---- #139: 에이전트가 쓴 deliverables 를 보드 요약이 덮어쓰지 않음 ----
def test_write_deliverables_creates_fallback_when_absent(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        return b

    b = _run(scenario())
    written = b.write_deliverables()
    # 에이전트가 쓰지 않았으면 보드 요약을 fallback 으로 생성
    assert written == ["docs/DELIVERABLES.md", "docs/DELIVERABLES.ko.md"]
    en = (tmp_path / "docs" / "DELIVERABLES.md").read_text(encoding="utf-8")
    ko = (tmp_path / "docs" / "DELIVERABLES.ko.md").read_text(encoding="utf-8")
    assert "# Development Deliverables" in en
    assert "# 개발 산출물" in ko


def test_write_deliverables_does_not_clobber_agent_authored(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        return b

    b = _run(scenario())
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    # docs-writer 백엔드가 이번 런에서 작성한 풍부한 산출물을 미리 둔다
    rich_en = "# Hand-authored richer deliverables EN\n"
    rich_ko = "# 손으로 작성한 풍부한 산출물 KO\n"
    (docs_dir / "DELIVERABLES.md").write_text(rich_en, encoding="utf-8")
    (docs_dir / "DELIVERABLES.ko.md").write_text(rich_ko, encoding="utf-8")

    written = b.write_deliverables()
    # 두 경로 모두 반환되어 전역 아티팩트로 추가 가능
    assert written == ["docs/DELIVERABLES.md", "docs/DELIVERABLES.ko.md"]
    # 에이전트가 쓴 원본 내용이 그대로 보존됨 (보드 요약이 덮어쓰지 않음)
    assert (docs_dir / "DELIVERABLES.md").read_text(encoding="utf-8") == rich_en
    assert (docs_dir / "DELIVERABLES.ko.md").read_text(encoding="utf-8") == rich_ko


def test_write_deliverables_fills_only_missing_side(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        return b

    b = _run(scenario())
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    # 에이전트가 EN 만 작성한 경우: EN 은 보존하고 KO 만 보드 요약으로 채운다
    rich_en = "# Only EN authored\n"
    (docs_dir / "DELIVERABLES.md").write_text(rich_en, encoding="utf-8")

    written = b.write_deliverables()
    assert written == ["docs/DELIVERABLES.md", "docs/DELIVERABLES.ko.md"]
    assert (docs_dir / "DELIVERABLES.md").read_text(encoding="utf-8") == rich_en
    # KO 는 보드 요약으로 생성됨
    ko = (docs_dir / "DELIVERABLES.ko.md").read_text(encoding="utf-8")
    assert "# 개발 산출물" in ko
