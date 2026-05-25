"""audit11: 4차 교차검증(W/P 항목) 회귀 테스트."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.__main__ import _print_summary
from orchestrator.board import _dumps_safe
from orchestrator.config import RunConfig
from orchestrator.gitcheckpoints import GitCheckpointer
from orchestrator.scheduler import BLOCKED, Scheduler


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    assert _git(path, "init").returncode == 0
    assert _git(path, "config", "user.email", "a@b.c").returncode == 0
    assert _git(path, "config", "user.name", "a").returncode == 0


def _cfg(tmp_path: Path, *, max_attempts: int = 1) -> RunConfig:
    spec = tmp_path / "spec.md"
    spec.write_text("build", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    return RunConfig(
        spec_path=spec,
        project_dir=project,
        default_backend="mock",
        max_attempts=max_attempts,
        auto_commit=False,
    )


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
def test_checkpoint_root_filesystem_rename_does_not_stage_unrelated_root_deletion(tmp_path: Path):
    project = tmp_path / "repo"
    _init_repo(project)
    (project / "old1.txt").write_text("old1", encoding="utf-8")
    (project / "old2.txt").write_text("old2", encoding="utf-8")
    assert _git(project, "add", "-A").returncode == 0
    assert _git(project, "commit", "-m", "base").returncode == 0

    cp = GitCheckpointer(project, enabled=True)
    (project / "old1.txt").unlink()
    (project / "new1.txt").write_text("new1", encoding="utf-8")
    (project / "old2.txt").unlink()

    changed = cp._changed_paths(["new1.txt"])
    assert "old2.txt" not in changed


def test_run_subprocess_large_stdin_early_exit_has_no_unretrieved_future_warning():
    code = """
import asyncio
from orchestrator.backends.base import run_subprocess

async def main():
    await run_subprocess(['/usr/bin/true'], '.', 5, stdin_data=b'x' * (5 * 1024 * 1024))

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "Future exception was never retrieved" not in result.stderr
    assert "BrokenPipeError" not in result.stderr


def test_dev_initial_failure_without_repair_clears_failure_state(tmp_path: Path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, max_attempts=1))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])

        async def fake(_role, _unit=None):
            return {"_ok": False, "status": "failed", "blockers": ["dev failed"]}

        sched.runner.run_role = fake
        ok = await sched._develop_unit(sched.board.units()[0], asyncio.Semaphore(1), 1)
        return ok, sched

    ok, sched = asyncio.run(scenario())
    assert ok is False
    assert "U1" not in sched._dev_failure_signatures
    assert "U1" not in sched._last_dev_failure


def test_dev_repair_stop_is_not_reported_as_dev_failure(tmp_path: Path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, max_attempts=2))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])
        sched._stop.set()
        await sched._dev_repair_loop(sched.board.units()[0], asyncio.Semaphore(1), 2, {"id": "U1"})
        return sched.board.snapshot()

    snap = asyncio.run(scenario())
    unit = snap["units"][0]
    assert unit["status"] == BLOCKED
    assert any("stop requested" in note for note in unit.get("notes", []))


def test_dumps_safe_handles_tuple_nan_and_cycles():
    data: dict[str, object] = {"tuple": (float("nan"),)}
    data["self"] = data
    loaded = json.loads(_dumps_safe(data, default=str))
    assert loaded["tuple"] == [0.0]
    assert loaded["self"] == "<cycle>"


def test_authentication_failed_text_is_not_external_by_itself(tmp_path: Path):
    sched = Scheduler(_cfg(tmp_path, max_attempts=0))
    assert sched._external_blocker_reason([{"notes": ["authentication failed"]}]) is None
    assert sched._external_blocker_reason([{"blockers": ["missing api key"]}]) == "missing api key"


def test_print_summary_tolerates_units_none(tmp_path: Path, capsys):
    cfg = _cfg(tmp_path)
    _print_summary({"phase": "done", "units": None}, cfg)
    out = capsys.readouterr().out
    assert "units       : 0/0 done" in out


def test_webui_run_manager_caps_active_runs(tmp_path: Path):
    from orchestrator.webui import RunManager

    class FakeProc:
        pid = 12345

        def poll(self):
            return None

    manager = RunManager(tmp_path / "runs", spawn=lambda *_args: FakeProc(), max_running=1)
    manager.start("spec", {"backend": "mock"})
    with pytest.raises(RuntimeError, match="too many active runs"):
        manager.start("spec", {"backend": "mock"})
