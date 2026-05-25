"""감사 9차(2026-05-25) 회귀 테스트: __main__ CLI 검증/모드 배타/요약/실행 래핑.

순수·오프라인. parse_args/main 의 검증 경로와 _print_summary 출력만 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.__main__ import _print_summary, build_config, main, parse_args
from orchestrator.config import RunConfig

# ---------------------------------------------------------------------------
# #audit9-16/17: CLI 가 잘못된 숫자 입력을 친절히 거부 (SystemExit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--timeout", "inf"],
        ["--timeout", "nan"],
        ["--timeout", "-1"],
        ["--budget", "-5"],
        ["--budget", "inf"],
        ["--concurrency", "0"],
        ["--concurrency", "-2"],
        ["--max-units", "0"],
        ["--max-units", "-1"],
        ["--max-attempts", "-1"],
        ["--retries", "-1"],
    ],
)
def test_cli_rejects_invalid_numbers(args):
    with pytest.raises(SystemExit):
        parse_args(["--spec", "s.md", "--project-dir", "p", *args])


@pytest.mark.parametrize(
    "args",
    [
        ["--timeout", "0"],  # 0=무제한, 유효
        ["--budget", "0"],  # 0=무비용 차단 의도, 유효
        ["--concurrency", "1"],
        ["--max-units", "1"],
        ["--max-attempts", "0"],
        ["--retries", "0"],
    ],
)
def test_cli_accepts_valid_boundary_numbers(args):
    ns = parse_args(["--spec", "s.md", "--project-dir", "p", *args])
    assert ns is not None


def test_timeout_zero_normalized_to_unlimited_by_runconfig(tmp_path, sample_spec_path):
    # #audit9-16: --timeout 의 canonical 정규화는 RunConfig 가 한다(이중 정규화 제거).
    a = parse_args(
        [
            "--spec",
            str(sample_spec_path.resolve()),
            "--project-dir",
            str(tmp_path / "p"),
            "--mock",
            "--timeout",
            "0",
        ]
    )
    cfg = build_config(a)
    assert cfg.session_timeout is None


def test_timeout_positive_preserved(tmp_path, sample_spec_path):
    a = parse_args(
        [
            "--spec",
            str(sample_spec_path.resolve()),
            "--project-dir",
            str(tmp_path / "p"),
            "--mock",
            "--timeout",
            "300",
        ]
    )
    cfg = build_config(a)
    assert cfg.session_timeout == 300.0


# ---------------------------------------------------------------------------
# #audit9-18: 모드 플래그(--check/--web/--watch) 상호 배타
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--check", "--web"],
        ["--check", "--watch"],
        ["--web", "--watch"],
        ["--check", "--web", "--watch"],
    ],
)
def test_conflicting_mode_flags_rejected(args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert "동시에" in str(exc.value)


def test_check_alone_still_works(capsys):
    rc = main(["--check"])
    assert rc == 0
    assert "backend availability" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# #audit9-19: 누락 test_status 는 "None" 대신 빈칸으로 출력
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> RunConfig:
    return RunConfig(spec_path=tmp_path / "spec.md", project_dir=tmp_path)


def test_summary_missing_test_status_blank(tmp_path, capsys):
    snap = {
        "phase": "done",
        "units": [{"id": "U1", "status": "done", "title": "t"}],
        "total_cost_usd": 0.0,
    }
    _print_summary(snap, _cfg(tmp_path))
    out = capsys.readouterr().out
    unit_line = next(ln for ln in out.splitlines() if ln.strip().startswith("U1"))
    assert "None" not in unit_line
    assert "test=" in unit_line


def test_summary_present_test_status_shown(tmp_path, capsys):
    snap = {
        "phase": "done",
        "units": [{"id": "U1", "status": "done", "test_status": "pass", "title": "t"}],
        "total_cost_usd": 0.0,
    }
    _print_summary(snap, _cfg(tmp_path))
    out = capsys.readouterr().out
    assert "test=pass" in out


# ---------------------------------------------------------------------------
# #audit9-20: 최상위 실행 래핑 (KeyboardInterrupt→130, Exception→1, raw traceback X)
# ---------------------------------------------------------------------------


def test_run_keyboard_interrupt_returns_130(tmp_path, sample_spec_path, monkeypatch, capsys):
    import orchestrator.__main__ as m

    class _Sched:
        def __init__(self, cfg):
            pass

        async def run(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(m, "Scheduler", _Sched)
    rc = main(
        [
            "--spec",
            str(sample_spec_path.resolve()),
            "--project-dir",
            str(tmp_path / "p"),
            "--mock",
        ]
    )
    assert rc == 130
    assert "중단" in capsys.readouterr().err


def test_run_exception_returns_one_friendly(tmp_path, sample_spec_path, monkeypatch, capsys):
    import orchestrator.__main__ as m

    class _Sched:
        def __init__(self, cfg):
            pass

        async def run(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(m, "Scheduler", _Sched)
    rc = main(
        [
            "--spec",
            str(sample_spec_path.resolve()),
            "--project-dir",
            str(tmp_path / "p"),
            "--mock",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "boom" in err
