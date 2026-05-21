"""Tests for the monitor TUI's pure pieces + per-agent run-state tracking."""

from __future__ import annotations

import asyncio

from orchestrator.config import ROLES, RunConfig
from orchestrator.monitor import _read_agent_log, _read_board, render_snapshot
from orchestrator.scheduler import Scheduler


def test_render_snapshot_lists_all_roles_and_running_icon():
    board = {
        "phase": "done",
        "total_cost_usd": 1.5,
        "units": [{"status": "done"}],
        "agents": {"qa": {"status": "running", "cost_usd": 0.5, "calls": 2, "current_unit": "U1"}},
    }
    out = render_snapshot(board, list(ROLES))
    assert "phase=done" in out
    assert "cost=$1.5000" in out
    for role in ROLES:
        assert role in out
    assert "●" in out  # running icon present for qa
    assert "○" in out  # idle icon present for the others


def test_agents_tracked_after_mock_run(tmp_path, sample_spec_path):
    project = tmp_path / "demo"
    cfg = RunConfig(spec_path=sample_spec_path, project_dir=project, mock=True, poll_interval=600)
    snap = asyncio.run(Scheduler(cfg).run())

    agents = snap.get("agents", {})
    assert agents, "board should track per-agent state"
    assert agents["backend-developer"]["calls"] >= 1
    assert agents["architecture-engineer"]["calls"] >= 1

    orch = project / ".orchestrator"
    assert _read_board(orch).get("agents"), "monitor reads agents from board.json"
    log = _read_agent_log(orch, "backend-developer")
    assert "done" in log  # activity log captured start/done lines


def test_wrap_line_soft_wraps_by_display_width():
    from orchestrator.monitor import _wrap_line

    assert _wrap_line("abcdefghij", 4) == ["abcd", "efgh", "ij"]  # ASCII
    assert _wrap_line("가나다", 4) == ["가나", "다"]  # CJK 폭 2칸
    assert _wrap_line("", 10) == [""]  # 빈 줄 유지


def test_tui_stop_and_rerun_helpers(tmp_path):
    from orchestrator.monitor import _rerun, _stop_run

    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    assert _stop_run(orch) is False  # run.pid 없음 → False (예외 없이)
    ok, msg = _rerun(orch)
    assert ok is False and ("재실행" in msg or "rerun" in msg.lower())  # rerun.json 없음
    (orch / "rerun.json").write_text('{"argv":["--help"]}', encoding="utf-8")
    # rerun.json 있으면 launch 시도 (argv 파싱 성공 경로)
    ok2, _ = _rerun(orch)
    assert ok2 is True
