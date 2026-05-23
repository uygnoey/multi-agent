"""End-to-end orchestration test using the mock backend (offline, no API keys)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.config import RunConfig
from orchestrator.scheduler import Scheduler


def _run(sample_spec_path: Path, project_dir: Path) -> dict:
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=project_dir,
        mock=True,
        # High poll interval so PM/PL supervisors tick once then unwind.
        poll_interval=600.0,
    )
    return asyncio.run(Scheduler(cfg).run())


def test_full_mock_run_reaches_done(tmp_path: Path, sample_spec_path: Path):
    project_dir = tmp_path / "demo"
    snap = _run(sample_spec_path, project_dir)

    assert snap["phase"] == "done"

    units = snap["units"]
    assert len(units) >= 1
    for u in units:
        assert u["status"] == "done", f"{u['id']} status={u['status']}"
        assert u["test_status"] == "pass", f"{u['id']} test={u['test_status']}"

    # Generated source trees exist under the target.
    backend_files = list((project_dir / "backend").rglob("*.py"))
    frontend_files = list((project_dir / "frontend").rglob("*.jsx"))
    assert backend_files, "expected backend/ files"
    assert frontend_files, "expected frontend/ files"

    # CI/CD phase produced a workflow.
    assert (project_dir / ".github" / "workflows" / "ci.yml").exists()


def test_full_mock_run_persists_board_and_report(tmp_path: Path, sample_spec_path: Path):
    project_dir = tmp_path / "demo"
    snap = _run(sample_spec_path, project_dir)

    board_path = project_dir / ".orchestrator" / "board.json"
    assert board_path.exists()
    on_disk = json.loads(board_path.read_text(encoding="utf-8"))
    assert on_disk["phase"] == "done"
    assert on_disk["units"]

    report = project_dir / ".orchestrator" / "report.md"
    assert report.exists()
    report_text = report.read_text(encoding="utf-8")
    # Every unit id should appear in the report table.
    for u in snap["units"]:
        assert u["id"] in report_text


def test_full_mock_run_creates_git_checkpoint_commits(
    tmp_path: Path, sample_spec_path: Path, monkeypatch
):
    if not shutil.which("git"):
        pytest.skip("git executable not available")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "dev-crew-orchestrator")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "dev-crew-orchestrator@brillianttiger.io")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "dev-crew-orchestrator")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "dev-crew-orchestrator@brillianttiger.io")
    project_dir = tmp_path / "demo"
    _run(sample_spec_path, project_dir)

    log = subprocess.run(
        ["git", "-C", str(project_dir), "log", "--format=%s"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()

    assert "orchestrator: scaffold project" in log
    assert "orchestrator: design work units" in log
    assert "orchestrator: cicd artifacts" in log
    assert "orchestrator: docs artifacts" in log
    assert any(line.endswith(" verified") for line in log)


def test_auto_commit_can_be_disabled(tmp_path: Path, sample_spec_path: Path):
    project_dir = tmp_path / "demo"
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=project_dir,
        mock=True,
        poll_interval=600.0,
        auto_commit=False,
    )
    asyncio.run(Scheduler(cfg).run())
    assert not (project_dir / ".git").exists()
