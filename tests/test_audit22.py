"""audit22 회귀 테스트 — 묶음 2 (정확성/성능 보정).

수정 항목(합의된 최종표):
  #1 (MED)  CR-2 board.add_units 에 deps DAG 사이클 검출이 없어, scheduler 의 _wait_for_deps
            가 stall timeout(기본 1800~3600s)까지 기다리다 실패했다. DFS 3색으로 즉시 검출
            하고 cycle unit 들을 FAILED 마킹 + warning 으로 가시화한다.
  #2 (MED)  H3 in-flight $0.50 고정 예약을 백엔드/모델별 추정으로 변경. 고비용 모델(opus 등)
            에서 동시 N 개 호출이 모두 예산 통과 후 합산이 예산을 크게 초과하던 문제 완화.
  #3 (MED)  H4 Board.total_cost() 경량 getter 추가. runner._budget_lock 안의 무거운
            snapshot() (_dumps_safe + json.loads 전체 직렬화)을 단일 필드 조회로 대체.
            scheduler._budget_exhausted 도 동일 경량화.
  #4 (MED)  CR-12 claude_cli._UNKNOWN_OPTION_HINTS 의 'did you mean' 과대매칭 제거.
            CLI 가 값 유효성 오류에 'did you mean a smaller number?' 같은 문구를 쓸 때
            예산 플래그를 빼고 재실행 → silent budget cap 우회 위험을 차단.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


# ---------------------------------------------------------------------------
# #1 — CR-2: deps DAG 사이클 검출 + 자동 FAILED 마킹
# ---------------------------------------------------------------------------
def test_add_units_detects_simple_cycle_and_auto_fails() -> None:
    """A→B→A 같은 단순 사이클을 board 단에서 잡아 cycle unit 을 FAILED 마킹."""
    from orchestrator.board import FAILED, Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            board = Board(Path(td))
            await board.init("spec", "stack")
            await board.add_units(
                [
                    {"id": "A", "title": "A", "deps": ["B"], "roles": ["frontend-developer"]},
                    {"id": "B", "title": "B", "deps": ["A"], "roles": ["frontend-developer"]},
                ]
            )
            units = {u["id"]: u for u in board.units()}
            assert units["A"]["status"] == FAILED, "cycle 의 A 가 FAILED 마킹 안 됨"
            assert units["B"]["status"] == FAILED, "cycle 의 B 가 FAILED 마킹 안 됨"
            # 노트에 audit22 마커
            assert any("audit22" in n for n in units["A"]["notes"])
            assert any("audit22" in n for n in units["B"]["notes"])
            # warning 도 기록됨
            warnings = board.snapshot().get("warnings", [])
            assert any("deps cycle" in w for w in warnings), "cycle warning 누락"

    asyncio.run(run())


def test_add_units_detects_long_cycle() -> None:
    """A→B→C→D→A 처럼 긴 사이클도 검출."""
    from orchestrator.board import FAILED, Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            board = Board(Path(td))
            await board.init("spec", "stack")
            await board.add_units(
                [
                    {"id": "A", "deps": ["D"], "roles": ["dba"]},
                    {"id": "B", "deps": ["A"], "roles": ["dba"]},
                    {"id": "C", "deps": ["B"], "roles": ["dba"]},
                    {"id": "D", "deps": ["C"], "roles": ["dba"]},
                ]
            )
            for uid in ("A", "B", "C", "D"):
                u = next(x for x in board.units() if x["id"] == uid)
                assert u["status"] == FAILED, f"{uid} not FAILED"

    asyncio.run(run())


def test_add_units_acyclic_dag_unchanged() -> None:
    """정상 DAG(사이클 없음) 은 designed 상태로 그대로 추가되어야 한다."""
    from orchestrator.board import DESIGNED, Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            board = Board(Path(td))
            await board.init("spec", "stack")
            await board.add_units(
                [
                    {"id": "A", "deps": [], "roles": ["frontend-developer"]},
                    {"id": "B", "deps": ["A"], "roles": ["frontend-developer"]},
                    {"id": "C", "deps": ["A", "B"], "roles": ["frontend-developer"]},
                ]
            )
            for u in board.units():
                assert u["status"] == DESIGNED, f"{u['id']} status={u['status']}"
            warnings = board.snapshot().get("warnings", [])
            assert not any("deps cycle" in w for w in warnings)

    asyncio.run(run())


def test_add_units_self_loop_is_cycle() -> None:
    """A→A self-loop 도 사이클로 잡아야 한다."""
    from orchestrator.board import FAILED, Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            board = Board(Path(td))
            await board.init("spec", "stack")
            await board.add_units(
                [{"id": "A", "deps": ["A"], "roles": ["dba"]}]
            )
            u = board.units()[0]
            assert u["status"] == FAILED

    asyncio.run(run())


# ---------------------------------------------------------------------------
# #2 — H3: in-flight 예약이 백엔드/모델별로 추정됨
# ---------------------------------------------------------------------------
def test_inflight_reserve_estimate_by_backend_and_model() -> None:
    from orchestrator.runner import (
        _INFLIGHT_RESERVE_DEFAULT_USD,
        _estimate_inflight_reserve,
    )

    # mock 은 0 — 회계 오염 방지
    assert _estimate_inflight_reserve("mock", "anything") == 0.0
    assert _estimate_inflight_reserve("mock", None) == 0.0

    # opus 가 가장 비쌈
    opus = _estimate_inflight_reserve("claude-cli", "claude-opus-4-7")
    sonnet = _estimate_inflight_reserve("claude-cli", "claude-sonnet-4-6")
    haiku = _estimate_inflight_reserve("claude-cli", "claude-haiku-4-5-20251001")
    assert opus > sonnet > haiku > 0
    assert opus >= 1.0, "opus 추정이 너무 작아 동시 N-way 초과 차단에 부적합"

    # gpt-5/gpt-4o 도 sonnet 등급
    assert _estimate_inflight_reserve("openai-agents", "gpt-5-codex") >= 0.20
    assert _estimate_inflight_reserve("openai-agents", "gpt-4o-mini") <= 0.10

    # 모델 unknown → 백엔드 기본
    assert (
        _estimate_inflight_reserve("codex-cli", None)
        < _INFLIGHT_RESERVE_DEFAULT_USD
    ), "codex-cli 기본은 default 보다 작아야 (gpt-5-codex turn 추정)"
    assert (
        _estimate_inflight_reserve("unknown-backend", None)
        == _INFLIGHT_RESERVE_DEFAULT_USD
    )


# ---------------------------------------------------------------------------
# #3 — H4: Board.total_cost() 경량 getter 가 snapshot() 과 동일 값을 반환
# ---------------------------------------------------------------------------
def test_board_total_cost_matches_snapshot() -> None:
    from orchestrator.board import Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            board = Board(Path(td))
            await board.init("spec", "stack")
            # total_cost_usd 누적 — add_cost() 가 board 전체 누적용 API
            await board.add_cost(0.123)
            await board.add_cost(0.456)
            via_snapshot = board.snapshot().get("total_cost_usd", 0.0)
            via_getter = board.total_cost()
            assert abs(via_getter - via_snapshot) < 1e-9
            assert abs(via_getter - 0.579) < 1e-6

    asyncio.run(run())


def test_board_total_cost_handles_corrupted_value() -> None:
    """손상된 total_cost_usd(문자열/NaN/Inf)에서도 0.0 폴백."""
    import math

    from orchestrator.board import Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            board = Board(Path(td))
            await board.init("spec", "stack")
            # 손상 시뮬레이션
            board._data["total_cost_usd"] = "garbage"
            assert board.total_cost() == 0.0
            board._data["total_cost_usd"] = float("inf")
            assert board.total_cost() == 0.0
            board._data["total_cost_usd"] = float("nan")
            assert math.isnan(board.total_cost()) is False
            board._data["total_cost_usd"] = None
            assert board.total_cost() == 0.0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# #4 — CR-12: 'did you mean' 과대매칭 제거
# ---------------------------------------------------------------------------
def test_claude_cli_did_you_mean_no_longer_matches_value_error() -> None:
    """CLI 가 값 유효성 오류에 'did you mean' 을 쓰면 silent 예산 우회가 생겼었다.

    audit22 에서 'did you mean' 힌트를 _UNKNOWN_OPTION_HINTS 에서 제거. 진짜 미지 옵션
    힌트만 유지(unknown/unrecognized/no such option/unexpected option/unknown argument/
    not found/not recognized).
    """
    from orchestrator.backends.claude_cli import _is_unknown_budget_flag_error

    # 진짜 unknown 옵션 — True (확정 표현)
    assert _is_unknown_budget_flag_error(
        "error: unknown option '--max-budget-usd'"
    )
    assert _is_unknown_budget_flag_error(
        "error: unrecognized argument: --max-budget-usd"
    )
    assert _is_unknown_budget_flag_error(
        "no such option: --max-budget-usd"
    )

    # 값 오류 + 'did you mean' — False (이전엔 True 였음 → silent 예산 우회 회귀)
    assert not _is_unknown_budget_flag_error(
        "error: invalid value for --max-budget-usd, did you mean a smaller number?"
    )
    assert not _is_unknown_budget_flag_error(
        "max-budget-usd: did you mean to use --max-tokens?"
    )

    # 플래그 이름 없음 — 항상 False (다른 플래그 오류와 분리)
    assert not _is_unknown_budget_flag_error("error: unknown option '--bogus'")


def test_claude_cli_not_found_and_not_recognized_are_unknown_option() -> None:
    """#audit22-amend (Codex 검증 보정): 진짜 unknown 옵션 표현 false-negative 보강.

    'did you mean' 제거가 너무 좁아 다음 형태들이 unknown 옵션으로 인식되지 않았다 →
    예산 플래그 빼고 재실행 못 함 → 사용자가 호환성 문제로 오인.
    'not found'/'not recognized' 추가로 보강. '--max-budget-usd' 동시 포함 조건이
    false-positive 를 막아준다.
    """
    from orchestrator.backends.claude_cli import _is_unknown_budget_flag_error

    # Codex 보고 false-negative 2건 (보정 후 True 여야 함)
    assert _is_unknown_budget_flag_error(
        "error: option '--max-budget-usd' not found, did you mean '--max-tokens'?"
    )
    assert _is_unknown_budget_flag_error(
        "error: --max-budget-usd is not a recognized option"
    )
    # 변형: is not recognized
    assert _is_unknown_budget_flag_error(
        "error: --max-budget-usd is not recognized"
    )

    # false-positive 안전성: 무관 'command not found' 는 플래그 이름 없어 통과 못 함
    assert not _is_unknown_budget_flag_error("claude: command not found")
    assert not _is_unknown_budget_flag_error(
        "error: file 'spec.md' not found"
    )


# ---------------------------------------------------------------------------
# #5 — runner.run_role 예약 추정 통합 동작 — mock 백엔드는 0 예약(예산 회계 비오염)
# ---------------------------------------------------------------------------
def test_runner_estimate_no_false_block_when_mock() -> None:
    """mock 백엔드는 예약 0 — 동시 호출이 예산 정확성에 영향 안 줌."""
    from orchestrator.runner import _estimate_inflight_reserve

    assert _estimate_inflight_reserve("mock", "anything") == 0.0
    assert _estimate_inflight_reserve("claude-cli", "claude-sonnet-4-6") > 0
