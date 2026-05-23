"""감사 7차: 숫자 옵션 상한 클램프 · 백엔드 이름 검증 · CLI(--port/--interval) 검증.

병적으로 큰 숫자 값이 OOM/무한작업/hang 으로 이어지지 못하도록 RunConfig 가 상한을 두는지,
프로그래매틱 RunConfig 구성에서도 알 수 없는 백엔드 이름이 안전화되는지(raise 없이),
그리고 RunConfig 를 우회해 serve/run_tui 로 직행하는 --port/--interval 이 argparse 단계에서
범위 밖 입력을 거부하는지 검증한다. 모든 테스트는 오프라인/결정적이며 백엔드를 호출하지 않는다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.__main__ import parse_args
from orchestrator.config import RunConfig

DUMMY = Path("/x")


def _cfg(**kw):
    return RunConfig(spec_path=DUMMY, project_dir=DUMMY, **kw)


# ---------------------------------------------------------------------------
# 숫자 옵션 상한 클램프 (concurrency / max_attempts / retries / poll_interval / retry_backoff)
# ---------------------------------------------------------------------------


def test_concurrency_clamped_to_max():
    assert _cfg(concurrency=10**9).concurrency == 64


def test_max_attempts_clamped_to_max():
    assert _cfg(max_attempts=10**9).max_attempts == 20


def test_retries_clamped_to_max():
    assert _cfg(retries=10**9).retries == 20


def test_poll_interval_huge_finite_clamped():
    # 1e308 같은 유한하지만 거대한 값도 1시간(3600초) 상한으로 클램프된다.
    assert _cfg(poll_interval=1e308).poll_interval == 3600.0


def test_retry_backoff_inf_resets_to_default():
    assert _cfg(retry_backoff=float("inf")).retry_backoff == 2.0


def test_retry_backoff_huge_clamped():
    assert _cfg(retry_backoff=1e9).retry_backoff == 60.0


def test_retry_backoff_negative_resets_to_default():
    assert _cfg(retry_backoff=-5).retry_backoff == 2.0


def test_normal_values_preserved():
    # 정상값은 클램프에 걸리지 않고 그대로 보존되어야 한다.
    cfg = _cfg(concurrency=3, poll_interval=600, max_attempts=2, retries=1, retry_backoff=2.0)
    assert cfg.concurrency == 3
    assert cfg.poll_interval == 600.0
    assert cfg.max_attempts == 2
    assert cfg.retries == 1
    assert cfg.retry_backoff == 2.0


# ---------------------------------------------------------------------------
# 백엔드 이름 검증 (raise 없이 sanitize)
# ---------------------------------------------------------------------------


# warn-only: 알 수 없는 백엔드명은 드롭/치환하지 않고 *유지*하며 경고만 남긴다.
# (드롭하면 fake 백엔드 주입 테스트/런타임 monkeypatch 레지스트리가 깨지고, 알 수 없는 이름은
#  어차피 runner._candidates/available() 가 skip/실패로 안전 처리한다.)
def test_unknown_default_backend_kept_with_warning():
    cfg = _cfg(default_backend="bogus")
    assert cfg.default_backend == "bogus"  # 드롭/치환하지 않고 유지
    assert any("bogus" in w for w in cfg.backend_warnings)


def test_unknown_backend_priority_kept_with_warning():
    cfg = _cfg(backend_priority=["mock", "bogus"])
    assert cfg.backend_priority == ["mock", "bogus"]  # 유지(순서 보존)
    assert any("bogus" in w for w in cfg.backend_warnings)


def test_known_alias_resolved_in_default_backend():
    # 별칭(openai-sdk → openai-agents)은 정식명으로 해소되어 유효하게 유지된다.
    cfg = _cfg(default_backend="openai-sdk")
    assert cfg.default_backend == "openai-agents"
    assert not cfg.backend_warnings  # 유효 별칭은 경고 없음


def test_mock_remains_valid():
    cfg = _cfg(default_backend="mock", backend_priority=["mock"])
    assert cfg.default_backend == "mock"
    assert cfg.backend_priority == ["mock"]
    assert not cfg.backend_warnings


def test_unknown_role_priority_kept_with_warning():
    cfg = _cfg(role_priority={"backend-developer": ["bogus"], "dba": ["mock", "nope"]})
    # 유지(역할 제거/드롭 없음) — 런타임이 알 수 없는 후보를 skip 한다.
    assert cfg.role_priority["backend-developer"] == ["bogus"]
    assert cfg.role_priority["dba"] == ["mock", "nope"]
    assert cfg.backend_warnings


def test_unknown_role_backend_kept_with_warning():
    cfg = _cfg(role_backend={"dba": "bogus"})
    assert cfg.role_backend["dba"] == "bogus"  # 유지
    assert any("bogus" in w for w in cfg.backend_warnings)


def test_sanitize_does_not_raise_and_records_warnings():
    cfg = _cfg(default_backend="bogus", backend_priority=["mock", "bogus"])
    assert cfg.backend_warnings  # 경고가 보존된다
    assert any("bogus" in w for w in cfg.backend_warnings)


# ---------------------------------------------------------------------------
# CLI: --port / --interval 범위 검증 (RunConfig 우회 경로 방어)
# ---------------------------------------------------------------------------


def test_port_out_of_range_exits():
    with pytest.raises(SystemExit):
        parse_args(["--web", "--port", "99999"])


def test_interval_zero_exits():
    with pytest.raises(SystemExit):
        parse_args(["--watch", "--interval", "0"])


def test_interval_negative_exits():
    with pytest.raises(SystemExit):
        parse_args(["--watch", "--interval", "-1"])


def test_port_and_interval_valid_parse():
    a = parse_args(["--port", "8765", "--interval", "2"])
    assert a.port == 8765
    assert a.interval == 2.0
