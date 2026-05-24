"""감사 6차(2026-05-22) 회귀 테스트: orchestrator/monitor.py + orchestrator/__main__.py.

결정적·오프라인(curses/네트워크/API 불필요)으로 다음 6건을 회귀 검증한다.

- #11 rerun argv 에 --help/-h 가 있으면 _validate_rerun_argv 가 거부한다
      (실제 저장된 run 의 argv 에는 help 가 없으며, 허용 시 help 만 출력하고 exit 0 인데도
       _rerun() 이 "재실행 시작됨" 으로 거짓 보고한다).
- #12 값을 요구하는 플래그의 arity 검증 (다음 토큰이 값이어야 함).
- #14 _num 이 NaN/Inf 를 fallback 으로 강제해 int()/:.4f 가 안 터진다.
- #15 손상 board(units 가 list 아님 / 원소가 dict 아님)에도 render_snapshot 이 안 터진다.
- #16 __main__ 의 parse_args 가 --interval(기본 1.0)을 노출하고 --watch 가 이를 쓴다.
- #17 _clamp_interval 가 0/음수/NaN/Inf/비숫자를 안전한 값으로 정규화한다(순수 함수).
"""

from __future__ import annotations

import math

from orchestrator.__main__ import parse_args
from orchestrator.config import ROLES
from orchestrator.monitor import (
    _clamp_interval,
    _num,
    _validate_rerun_argv,
    render_snapshot,
)


# ---------------------------------------------------------------------------
# #11: rerun argv 에 --help/-h 가 있으면 거부
# ---------------------------------------------------------------------------
def test_rerun_argv_rejects_help_long():
    ok, _why = _validate_rerun_argv(["--spec", "foo.md", "--help"])
    assert ok is False


def test_rerun_argv_rejects_help_short():
    ok, _why = _validate_rerun_argv(["--spec", "foo.md", "-h"])
    assert ok is False


def test_rerun_argv_rejects_help_as_first_token():
    ok, _why = _validate_rerun_argv(["--help"])
    assert ok is False


# ---------------------------------------------------------------------------
# #12: 값 요구 플래그의 arity 검증
# ---------------------------------------------------------------------------
def test_rerun_argv_value_flag_followed_by_flag_rejected():
    # --spec 은 값을 요구하는데 다음 토큰이 플래그(--mock) → 거부.
    ok, why = _validate_rerun_argv(["--spec", "--mock"])
    assert ok is False
    assert why  # 명확한 한국어 사유


def test_rerun_argv_value_flag_with_value_then_storetrue_ok():
    ok, _why = _validate_rerun_argv(["--spec", "foo.md", "--mock"])
    assert ok is True


def test_rerun_argv_store_true_only_ok():
    # store-true 플래그(--mock)는 값이 필요 없으므로 단독으로 OK.
    ok, _why = _validate_rerun_argv(["--mock"])
    assert ok is True


def test_rerun_argv_multiple_value_flags_ok():
    ok, _why = _validate_rerun_argv(["--budget", "5", "--model", "x"])
    assert ok is True


def test_rerun_argv_value_flag_missing_value_at_end_rejected():
    ok, why = _validate_rerun_argv(["--spec"])
    assert ok is False
    assert why


def test_rerun_argv_eq_form_counts_as_value():
    # '--flag=value' 형태는 값을 가진 것으로 본다.
    ok, _why = _validate_rerun_argv(["--spec=foo.md"])
    assert ok is True


# ---------------------------------------------------------------------------
# #14: _num 의 NaN/Inf 방어
# ---------------------------------------------------------------------------
def test_num_inf_string_returns_fallback():
    assert _num("inf") == 0.0


def test_num_negative_inf_string_returns_fallback():
    assert _num("-inf") == 0.0


def test_num_nan_string_returns_fallback():
    assert _num("nan") == 0.0


def test_num_inf_float_returns_fallback():
    assert _num(float("inf")) == 0.0


def test_num_int_of_nonfinite_does_not_raise():
    # 이전 버그: int(_num("inf")) 가 OverflowError. 이제 fallback(0.0) 이라 안전.
    assert int(_num("inf")) == 0
    assert int(_num(float("inf"))) == 0


def test_num_finite_values_still_pass():
    assert _num("3.5") == 3.5
    assert _num(2) == 2.0


# ---------------------------------------------------------------------------
# #15: 손상 board(units) 방어
# ---------------------------------------------------------------------------
def test_render_snapshot_units_is_dict_not_list():
    out = render_snapshot({"units": {"x": 1}}, list(ROLES))
    assert isinstance(out, str)


def test_render_snapshot_units_is_string():
    out = render_snapshot({"units": "oops"}, list(ROLES))
    assert isinstance(out, str)


def test_render_snapshot_agents_is_string():
    out = render_snapshot({"agents": "oops", "units": []}, list(ROLES))
    assert isinstance(out, str)


def test_render_snapshot_units_mixed_elements_counts_valid_dict():
    # 비-dict 원소(1, "a")는 건너뛰고, 유효한 dict 원소는 집계된다.
    out = render_snapshot({"units": [1, "a", {"id": "U1", "status": "done"}]}, list(ROLES))
    assert isinstance(out, str)
    # 유효 dict 1개(done) 만 카운트 → "units=1/1" 이 헤더에 포함되어야 한다.
    assert "units=1/1" in out


# ---------------------------------------------------------------------------
# #16: __main__ 의 --interval
# ---------------------------------------------------------------------------
def test_main_parse_args_interval_default():
    a = parse_args(["--watch", "--project-dir", "X"])
    assert a.interval == 1.0


def test_main_parse_args_interval_override():
    a = parse_args(["--watch", "--project-dir", "X", "--interval", "2"])
    assert a.interval == 2.0


# ---------------------------------------------------------------------------
# #17: _clamp_interval 순수 함수
# ---------------------------------------------------------------------------
def test_clamp_interval_zero():
    assert _clamp_interval(0) == 1.0


def test_clamp_interval_negative():
    assert _clamp_interval(-5) == 1.0


def test_clamp_interval_inf():
    assert _clamp_interval(float("inf")) == 1.0


def test_clamp_interval_nan_string():
    assert _clamp_interval("nan") == 1.0


def test_clamp_interval_too_small_floored():
    assert _clamp_interval(0.05) == 0.1


def test_clamp_interval_normal_value():
    assert _clamp_interval(2) == 2.0


def test_clamp_interval_bad_string():
    assert _clamp_interval("bad") == 1.0


def test_clamp_interval_result_is_finite():
    # 어떤 입력이든 결과는 유한·양수여야 한다(timeout(int*1000) 안전).
    for raw in (0, -1, float("inf"), float("nan"), "x", 0.05, 2, None):
        v = _clamp_interval(raw)
        assert math.isfinite(v) and v > 0
