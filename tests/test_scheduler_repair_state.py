from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.board import BLOCKED, DONE
from orchestrator.config import DEV_ROLES, RunConfig
from orchestrator.scheduler import Scheduler


def _cfg(tmp_path: Path, sample_spec_path: Path, **kw) -> RunConfig:
    base = dict(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        mock=True,
        poll_interval=600.0,
    )
    base.update(kw)
    return RunConfig(**base)


def test_initial_dev_failure_enters_repair_loop(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=2))
        frontend_calls = 0
        repair_contexts: list[str | None] = []

        async def fake(role, unit=None):
            nonlocal frontend_calls
            if unit is None and role == "architecture-engineer":
                return {
                    "_ok": True,
                    "status": "done",
                    "artifacts": [],
                    "units": [{"id": "U1", "title": "t", "roles": list(DEV_ROLES)}],
                }
            if unit is None:
                return {"_ok": True, "status": "done", "artifacts": [], "units": []}
            if role == "frontend-developer":
                frontend_calls += 1
                repair_contexts.append(unit.get("repair_context"))
                if frontend_calls == 1:
                    return {
                        "_ok": False,
                        "status": "failed",
                        "artifacts": [],
                        "blockers": "vite config broken",
                        "notes": "dev server cannot start",
                    }
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        snap = await sched.run()
        return snap, frontend_calls, repair_contexts

    snap, frontend_calls, repair_contexts = asyncio.run(scenario())
    assert snap["phase"] == "done"
    assert snap["units"][0]["status"] == DONE
    assert frontend_calls == 2
    assert "vite config broken" in (repair_contexts[-1] or "")
    assert "- vite config broken" in (repair_contexts[-1] or "")


def test_unlimited_dev_repair_stops_on_repeated_identical_failure(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])
        calls = 0

        async def fake(role, unit=None):
            nonlocal calls
            calls += 1
            return {
                "_ok": False,
                "status": "failed",
                "artifacts": [],
                "blockers": ["same environment failure"],
            }

        sched.runner.run_role = fake
        unit = sched.board.units()[0]
        ok = await sched._develop_unit(unit, asyncio.Semaphore(1), 1)
        assert ok is False
        await sched._repair_failed_dev(unit, asyncio.Semaphore(1), 1)
        return sched.board.snapshot(), calls

    snap, calls = asyncio.run(scenario())
    assert calls == 3
    assert snap["units"][0]["status"] == BLOCKED
    assert any("동일 dev 실패" in w for w in snap.get("warnings", []))


def test_test_config_repair_success_goes_straight_to_qa(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=2))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t"}])
        calls: list[str] = []
        test_calls = 0

        async def fake(role, unit=None):
            nonlocal test_calls
            calls.append(role)
            if role == "test-engineer":
                test_calls += 1
                if test_calls == 1:
                    return {
                        "_ok": False,
                        "status": "failed",
                        "artifacts": [],
                        "failure_kind": "test_config",
                    }
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        await sched._test_unit(sched.board.units()[0], asyncio.Semaphore(1), 1)
        return sched.board.snapshot(), calls

    snap, calls = asyncio.run(scenario())
    assert calls == ["test-engineer", "test-engineer", "qa"]
    assert snap["units"][0]["status"] == DONE
