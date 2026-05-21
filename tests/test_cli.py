"""Tests for the CLI entry point orchestrator.__main__.main."""

from __future__ import annotations

from pathlib import Path

from orchestrator.__main__ import main


def test_check_returns_zero(capsys):
    rc = main(["--check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "backend availability" in out
    # mock backend reported as available.
    assert "mock" in out


def test_mock_run_returns_zero(tmp_path: Path, sample_spec_path: Path):
    project_dir = tmp_path / "cli"
    rc = main(
        [
            "--spec",
            str(sample_spec_path.resolve()),
            "--project-dir",
            str(project_dir),
            "--mock",
        ]
    )
    assert rc == 0
    # The run produced a board for the target project.
    assert (project_dir / ".orchestrator" / "board.json").exists()
