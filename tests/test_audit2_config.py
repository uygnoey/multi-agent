"""감사 2차: 교차 검증 풀이 레거시 role_backend 핀을 반영하는지 검증 (#137/#138).

또한 #37(숫자 옵션 안전화) 및 #7 관련 cross_check 풀 로직을 함께 확인한다.
모든 테스트는 오프라인/결정적이며 mock 백엔드도 호출하지 않는다(순수 설정 로직).
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.config import ROLES, RunConfig

DUMMY = Path("/x")


def _cfg(**kw):
    return RunConfig(spec_path=DUMMY, project_dir=DUMMY, **kw)


# ---------------------------------------------------------------------------
# #137: 교차 검증 풀이 role_backend(레거시 단일 핀) 값을 포함해야 한다.
# ---------------------------------------------------------------------------


def test_cross_pool_includes_role_backend_values():
    # 단일 기본 백엔드 + QA를 레거시 role_backend 로 codex 에 핀 + cross_check.
    # 풀이 {claude-cli, codex} 로 넓어져 나머지 역할들이 교차 배정되어야 한다.
    cfg = _cfg(
        default_backend="claude-cli",
        cross_check=True,
        role_backend={"qa": "codex"},
    )
    # QA 는 role_backend 핀 준수
    assert cfg.backends_for("qa") == ["codex"]
    firsts = [cfg.backends_for(r)[0] for r in ROLES]
    # role_priority 핀 사용자와 동일하게 두 프로바이더가 교차되어야 한다 (한쪽 몰림 X)
    assert set(firsts) == {"claude-cli", "codex"}


def test_cross_pool_role_backend_matches_role_priority_distribution():
    # 동일 의미의 두 표기(role_priority vs role_backend)가 같은 교차 분포를 내야 한다 (#137).
    cfg_priority = _cfg(
        default_backend="claude-cli",
        cross_check=True,
        role_priority={"qa": ["codex"]},
    )
    cfg_backend = _cfg(
        default_backend="claude-cli",
        cross_check=True,
        role_backend={"qa": "codex"},
    )
    firsts_priority = [cfg_priority.backends_for(r)[0] for r in ROLES]
    firsts_backend = [cfg_backend.backends_for(r)[0] for r in ROLES]
    assert firsts_priority == firsts_backend


def test_cross_pool_role_backend_no_duplicates():
    # role_backend 값이 이미 base 풀에 있으면 중복 추가되지 않는다(순서 유지).
    cfg = _cfg(
        backend_priority=["claude-cli", "codex"],
        cross_check=True,
        role_backend={"qa": "codex"},
    )
    pool = cfg._cross_pool(["claude-cli", "codex"])
    assert pool == ["claude-cli", "codex"]


# ---------------------------------------------------------------------------
# #138: 미핀 역할 계산이 role_backend 핀을 핀으로 취급해야 한다(오프셋 왜곡 방지).
# ---------------------------------------------------------------------------


def test_role_backend_pin_excluded_from_unpinned_list():
    cfg = _cfg(
        default_backend="claude-cli",
        cross_check=True,
        role_backend={"qa": "codex"},
    )
    # 핀된 qa 는 미핀 목록에서 제외되어야 한다.
    unpinned = [r for r in ROLES if not cfg._is_pinned(r)]
    assert "qa" not in unpinned


def test_role_backend_pin_does_not_skew_other_roles():
    # role_backend 로 한 역할을 핀해도, 다른 미핀 역할들의 교차 오프셋이
    # 동일 의미의 role_priority 핀과 동일해야 한다 (#138 회귀 방지).
    cfg_backend = _cfg(
        default_backend="claude-cli",
        cross_check=True,
        role_backend={"qa": "codex"},
    )
    cfg_priority = _cfg(
        default_backend="claude-cli",
        cross_check=True,
        role_priority={"qa": ["codex"]},
    )
    for r in ROLES:
        if r == "qa":
            continue
        assert cfg_backend.backends_for(r)[0] == cfg_priority.backends_for(r)[0]


def test_is_pinned_recognizes_both_pin_sources():
    cfg = _cfg(
        role_priority={"dba": ["codex"]},
        role_backend={"qa": "claude-sdk"},
    )
    assert cfg._is_pinned("dba") is True
    assert cfg._is_pinned("qa") is True
    assert cfg._is_pinned("frontend-developer") is False


def test_role_backend_pin_still_honored_under_cross_check():
    # cross_check 가 켜져도 role_backend 핀 자체는 그대로 존중되어야 한다.
    cfg = _cfg(
        backend_priority=["claude-cli", "codex"],
        cross_check=True,
        role_backend={"backend-developer": "claude-sdk"},
    )
    assert cfg.backends_for("backend-developer") == ["claude-sdk"]


# ---------------------------------------------------------------------------
# #7 관련: cross_check 풀 로직 일반 검증 (풀 < 2 면 교차 미적용).
# ---------------------------------------------------------------------------


def test_cross_check_single_backend_no_alternation():
    # 풀이 하나뿐이면 교차할 수 없으므로 기본 백엔드만 반환한다.
    cfg = _cfg(default_backend="claude-cli", cross_check=True)
    assert cfg.backends_for("qa") == ["claude-cli"]


def test_role_priority_pin_takes_precedence_over_role_backend():
    # role_priority 가 role_backend 보다 우선한다(기존 backends_for 순서 유지).
    cfg = _cfg(
        role_priority={"qa": ["claude-sdk"]},
        role_backend={"qa": "codex"},
    )
    assert cfg.backends_for("qa") == ["claude-sdk"]


# ---------------------------------------------------------------------------
# #37: 숫자 옵션 안전화(라이브러리 호출부의 이상값에도 crash 없이 기본값).
# ---------------------------------------------------------------------------


def test_numeric_options_coerced_safely():
    cfg = _cfg(concurrency="bad", max_attempts=0, retries=-5, max_units=0)
    assert cfg.concurrency == 3
    assert cfg.max_attempts == 0
    assert cfg.retries == 0
    assert cfg.max_units is None  # 0/음수/이상값 → 제한 없음
