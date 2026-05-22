"""감사 5차(2026-05-22) 회귀 테스트: CLI 요약의 NaN/Inf 비용 출력 방어 (#10).

손상/이상 보드 스냅샷의 total_cost_usd 가 NaN/Inf 여도 "$nan"/"$inf"/"$-inf" 가 아니라
"$0.0000" 로 안전 출력되어야 한다. capsys 로 stdout 만 검증(오프라인·결정적).
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.__main__ import _print_summary
from orchestrator.config import RunConfig


def _cfg(tmp_path: Path) -> RunConfig:
    return RunConfig(spec_path=tmp_path / "spec.md", project_dir=tmp_path)


def _summary(snap, tmp_path, capsys) -> str:
    _print_summary(snap, _cfg(tmp_path))
    return capsys.readouterr().out


def test_nan_cost_prints_zero(tmp_path, capsys):
    out = _summary({"phase": "done", "units": [], "total_cost_usd": float("nan")}, tmp_path, capsys)
    assert "$nan" not in out.lower()
    assert "cost        : $0.0000" in out


def test_inf_cost_prints_zero(tmp_path, capsys):
    out = _summary({"phase": "done", "units": [], "total_cost_usd": float("inf")}, tmp_path, capsys)
    assert "$inf" not in out.lower()
    assert "cost        : $0.0000" in out


def test_negative_inf_cost_prints_zero(tmp_path, capsys):
    out = _summary(
        {"phase": "done", "units": [], "total_cost_usd": float("-inf")}, tmp_path, capsys
    )
    # cost 라인만 검사한다(tmp_path 경로에 'inf' 가 들어갈 수 있어 전체 검색은 부적절).
    cost_line = next(ln for ln in out.splitlines() if ln.startswith("cost"))
    assert "inf" not in cost_line.lower()
    assert cost_line == "cost        : $0.0000"


def test_finite_cost_still_formatted(tmp_path, capsys):
    out = _summary({"phase": "done", "units": [], "total_cost_usd": 1.2345}, tmp_path, capsys)
    assert "cost        : $1.2345" in out


def test_non_numeric_cost_prints_zero(tmp_path, capsys):
    # 문자열 등 비-숫자 cost(손상 스냅샷)도 죽지 않고 $0.0000.
    out = _summary({"phase": "done", "units": [], "total_cost_usd": "oops"}, tmp_path, capsys)
    assert "cost        : $0.0000" in out
