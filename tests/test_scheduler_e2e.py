"""End-to-end orchestration test using the mock backend (offline, no API keys)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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
