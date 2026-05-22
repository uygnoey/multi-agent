"""감사 발견사항 #30/#41 회귀 테스트: report/deliverables 의 비용 강제와 경고 안전화.

전부 결정적·오프라인이며 tmp_path 아래에만 파일을 쓴다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.board import (
    _MAX_WARNING_CHARS,
    Board,
    _safe_report_num,
    _safe_warning,
)


def _run(coro):
    return asyncio.run(coro)


# ---- #30: 리포트 숫자 강제 헬퍼 ----
def test_safe_report_num_coerces_bad_values():
    # 정상 숫자는 그대로
    assert _safe_report_num(1.5) == 1.5
    assert _safe_report_num(0) == 0.0
    assert _safe_report_num("2.5") == 2.5
    # 비-숫자/None/list/dict → 0.0
    assert _safe_report_num("oops") == 0.0
    assert _safe_report_num(None) == 0.0
    assert _safe_report_num([1, 2]) == 0.0
    assert _safe_report_num({"a": 1}) == 0.0
    # bool 은 명시적으로 거부 (float(True)==1.0 회피)
    assert _safe_report_num(True) == 0.0
    assert _safe_report_num(False) == 0.0
    # NaN/Inf → 0.0
    assert _safe_report_num(float("nan")) == 0.0
    assert _safe_report_num(float("inf")) == 0.0
    assert _safe_report_num(float("-inf")) == 0.0


def _board_with_unit(tmp_path: Path) -> Board:
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        return b

    return _run(scenario())


@pytest.mark.parametrize(
    "bad", ["corrupt", None, [1, 2], {"x": 1}, True, float("nan"), float("inf")]
)
def test_write_report_survives_non_numeric_cost(tmp_path: Path, bad):
    b = _board_with_unit(tmp_path)
    # 손상된 비용 값을 보드 내부 상태에 직접 주입
    b._data["total_cost_usd"] = bad
    # report.md 가 예외 없이 기록되어야 함 (복구성)
    report = b.write_report()
    text = report.read_text(encoding="utf-8")
    assert "Run Report" in text
    # 손상 비용은 $0.0000 으로 안전 표기
    assert "- total cost: **$0.0000**" in text


@pytest.mark.parametrize("bad", ["corrupt", None, [1, 2], True, float("inf")])
def test_write_deliverables_survives_non_numeric_cost(tmp_path: Path, bad):
    b = _board_with_unit(tmp_path)
    b._data["total_cost_usd"] = bad
    # EN/KO 산출물이 예외 없이 기록되어야 함 (복구성)
    written = b.write_deliverables()
    assert written == ["docs/DELIVERABLES.md", "docs/DELIVERABLES.ko.md"]
    en = (tmp_path / "docs" / "DELIVERABLES.md").read_text(encoding="utf-8")
    ko = (tmp_path / "docs" / "DELIVERABLES.ko.md").read_text(encoding="utf-8")
    assert "- total cost: **$0.0000**" in en
    assert "- 총비용: **$0.0000**" in ko


def test_write_report_valid_cost_still_formats(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        await b.add_cost(1.2345)
        return b

    b = _run(scenario())
    text = b.write_report().read_text(encoding="utf-8")
    assert "- total cost: **$1.2345**" in text


# ---- #41: 경고 안전화 헬퍼 ----
def test_safe_warning_neutralizes_newlines_and_pipes():
    out = _safe_warning("line1\nline2\rline3 | col")
    # 개행/캐리지리턴이 제거되어 한 줄로 유지
    assert "\n" not in out
    assert "\r" not in out
    # 파이프는 이스케이프되어 표/구조를 깨지 않음
    assert "\\|" in out


def test_safe_warning_caps_length():
    out = _safe_warning("z" * 5000)
    assert len(out) <= _MAX_WARNING_CHARS + len("…(truncated)")
    assert out.endswith("…(truncated)")


def test_write_report_escapes_and_caps_warnings(tmp_path: Path):
    b = _board_with_unit(tmp_path)
    # 마크다운/개행/거대 텍스트를 포함한 경고를 직접 주입
    b._data["warnings"] = [
        "## fake heading\nshould not become a real heading",
        "table | breaker",
        "x" * 5000,
    ]
    text = b.write_report().read_text(encoding="utf-8")
    # 경고 본문의 개행이 중화되어 가짜 제목 줄이 생기지 않음
    assert "- ## fake heading should not become a real heading" in text
    # 파이프 이스케이프
    assert "table \\| breaker" in text
    # 거대 경고가 캡되어 report.md 가 비대화되지 않음
    assert "…(truncated)" in text
    assert "z" * 5000 not in text
    # report 구조(Units 표 헤더)가 보존됨
    assert "| id | status | test | artifacts | title |" in text
