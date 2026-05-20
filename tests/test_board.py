"""Tests for orchestrator.board.Board state machine and persistence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orchestrator import board as board_mod
from orchestrator.board import (
    BLOCKED,
    DESIGNED,
    DEV_DONE,
    DONE,
    FAILED,
    IN_PROGRESS,
    TESTED,
    TESTING,
    TODO,
    Board,
)


def test_state_constants_exist():
    assert TODO == "todo"
    assert DESIGNED == "designed"
    assert IN_PROGRESS == "in_progress"
    assert DEV_DONE == "dev_done"
    assert TESTING == "testing"
    assert TESTED == "tested"
    assert DONE == "done"
    assert BLOCKED == "blocked"
    assert FAILED == "failed"


def test_full_state_transition_and_persistence(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec body text", {"backend": "FastAPI"})
        await b.add_units(
            [{"id": "U1", "title": "Auth", "deps": [], "roles": ["backend-developer"]}]
        )
        # Newly added units start in DESIGNED.
        assert b.units()[0]["status"] == DESIGNED

        await b.set_status("U1", IN_PROGRESS, note="attempt 1/2")
        await b.set_status("U1", DEV_DONE)
        await b.set_status("U1", TESTING)
        await b.add_artifacts("U1", ["backend/app/U1.py", "tests/test_u1.py"])
        await b.add_artifacts("U1", ["backend/app/U1.py"])  # duplicate ignored
        await b.set_test_status("U1", "pass")
        await b.set_status("U1", DONE)
        await b.add_cost(0.0)  # mock cost
        await b.add_cost(1.25)
        await b.add_cost(0.75)
        return b

    b = asyncio.run(scenario())

    unit = b.units()[0]
    assert unit["status"] == DONE
    assert unit["test_status"] == "pass"
    assert unit["artifacts"] == ["backend/app/U1.py", "tests/test_u1.py"]
    assert "attempt 1/2" in unit["notes"]

    # cost accumulates into the snapshot.
    snap = b.snapshot()
    assert snap["total_cost_usd"] == 2.0
    assert snap["phase"] == "init"

    # board.json persisted and is valid JSON.
    board_path = tmp_path / ".orchestrator" / "board.json"
    assert board_path.exists()
    on_disk = json.loads(board_path.read_text(encoding="utf-8"))
    assert on_disk["units"][0]["id"] == "U1"
    assert on_disk["units"][0]["status"] == DONE
    assert on_disk["total_cost_usd"] == 2.0


def test_set_phase_and_recent_events(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.set_phase("build")
        await b.log_event("scheduler", "hello world")
        return b

    b = asyncio.run(scenario())
    assert b.snapshot()["phase"] == "build"
    events = b.recent_events(20)
    assert "initialized" in events
    assert "hello world" in events


def test_directives_roundtrip(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        assert b.directives() == ""
        await b.append_directive("project-manager", "stay on track")
        return b

    b = asyncio.run(scenario())
    assert "stay on track" in b.directives()
    assert "project-manager" in b.directives()


def test_add_units_skips_duplicate_ids(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "first"}])
        await b.add_units([{"id": "U1", "title": "dupe"}, {"id": "U2", "title": "second"}])
        return b

    b = asyncio.run(scenario())
    ids = [u["id"] for u in b.units()]
    assert ids == ["U1", "U2"]
    assert b.units()[0]["title"] == "first"  # original kept


def test_write_report_contains_unit_id(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {"backend": "FastAPI"})
        await b.add_units([{"id": "U1", "title": "Auth"}])
        await b.set_status("U1", DONE)
        await b.set_test_status("U1", "pass")
        return b

    b = asyncio.run(scenario())
    report = b.write_report()
    assert isinstance(report, Path)
    assert report.exists()
    assert report == tmp_path / ".orchestrator" / "report.md"
    text = report.read_text(encoding="utf-8")
    assert "U1" in text
    assert "Run Report" in text


def test_module_exposes_state_constants():
    # Sanity that the constants are module-level (used by scheduler imports).
    assert board_mod.DONE == "done"
    assert board_mod.TERMINAL_OK == ("done", "tested")
