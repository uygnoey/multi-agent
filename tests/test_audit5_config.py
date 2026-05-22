"""감사 5차(2026-05-22) 회귀 테스트: RunConfig 의 NaN/Inf 숫자 옵션 정규화.

- #8  budget=NaN/Inf 는 비교를 무력화(committed >= nan 은 항상 False)해 예산 enforcement 가
      조용히 꺼진다 → None(예산 없음)으로 정규화.
- #9  poll_interval=Inf 는 supervisor 의 asyncio.wait_for 를 사실상 정지시킨다 → 기본 20초로.
      session_timeout=NaN/Inf/≤0 → None(무제한)으로 정규화.

전부 순수·오프라인이며 RunConfig 생성만으로 검증한다.
"""

from __future__ import annotations

import math
from pathlib import Path

from orchestrator.config import RunConfig


def _cfg(**kw) -> RunConfig:
    return RunConfig(spec_path=Path("spec.md"), project_dir=Path("proj"), **kw)


# ---- #8: budget ----
def test_budget_nan_becomes_none():
    assert _cfg(budget=float("nan")).budget is None


def test_budget_inf_becomes_none():
    assert _cfg(budget=float("inf")).budget is None
    assert _cfg(budget=float("-inf")).budget is None


def test_budget_finite_value_preserved():
    assert _cfg(budget=12.5).budget == 12.5
    # 0 은 유효한 유한값(모든 호출 차단 의도) → 그대로 유지.
    assert _cfg(budget=0.0).budget == 0.0


def test_budget_none_stays_none():
    assert _cfg(budget=None).budget is None


def test_budget_non_numeric_becomes_none():
    # 문자열 등 비-숫자 예산도 None 으로 안전화(웹 검증과 더불어 2중 방어).
    assert _cfg(budget="oops").budget is None


# ---- #9: poll_interval ----
def test_poll_interval_inf_falls_back_to_default():
    # Inf 가 그대로 wait_for 로 가면 감독 폴링이 멈춘다 → 기본 20초.
    assert _cfg(poll_interval=float("inf")).poll_interval == 20.0


def test_poll_interval_nan_falls_back_to_default():
    assert _cfg(poll_interval=float("nan")).poll_interval == 20.0


def test_poll_interval_finite_clamp_unchanged():
    # 유한값의 기존 클램프 동작은 유지: 정상 큰 값 그대로, 0/음수→5초, 소수는 최소 1초.
    assert _cfg(poll_interval=600).poll_interval == 600
    assert _cfg(poll_interval=0).poll_interval == 5.0
    assert _cfg(poll_interval=0.5).poll_interval == 1.0


# ---- #9: session_timeout ----
def test_session_timeout_inf_becomes_none():
    assert _cfg(session_timeout=float("inf")).session_timeout is None


def test_session_timeout_nan_becomes_none():
    assert _cfg(session_timeout=float("nan")).session_timeout is None


def test_session_timeout_zero_or_negative_becomes_none():
    # 0/음수는 의미가 없다 → None(무제한, CLI 의 0=무제한 정책과 일치).
    assert _cfg(session_timeout=0).session_timeout is None
    assert _cfg(session_timeout=-5).session_timeout is None


def test_session_timeout_finite_value_preserved():
    cfg = _cfg(session_timeout=900.0)
    assert cfg.session_timeout == 900.0
    assert math.isfinite(cfg.session_timeout)


def test_session_timeout_default_is_finite():
    # 기본값(1200초)은 유한하게 유지된다.
    assert _cfg().session_timeout == 1200.0
