"""프로덕션 하드닝 회귀 테스트 (멀티에이전트 감사에서 발견된 버그들)."""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from orchestrator import runner as runner_mod
from orchestrator import webui
from orchestrator.backends.base import RoleResult, run_subprocess
from orchestrator.board import FAILED, Board
from orchestrator.config import RunConfig
from orchestrator.scheduler import Scheduler


def test_subprocess_timeout_kills_and_flags():
    # 멈춘 자식이 무한 정지시키지 않고 timed_out=True 로 끊긴다.
    rc, out, err, timed_out = asyncio.run(
        run_subprocess([sys.executable, "-c", "import time;time.sleep(5)"], ".", 0.3)
    )
    assert timed_out is True
    assert rc is None


def test_webui_project_dir_blocks_path_traversal(tmp_path):
    m = webui.RunManager(tmp_path / "runs")
    for bad in ["../../etc", "../../../../etc/passwd", "/etc", "../" + "x"]:
        with pytest.raises(ValueError):
            m.project_dir(bad)
    # 정상 run id 는 통과
    ok = m.project_dir("my-app-20260101-000000")
    assert str(ok).startswith(str((tmp_path / "runs").resolve()))


def test_read_result_not_trusted_on_failure(tmp_path):
    # 백엔드 실패 시, 남아있는 'done' 결과파일을 성공으로 오탐하지 않는다.
    rp = tmp_path / "r.json"
    rp.write_text('{"status":"done","artifacts":["x"]}', encoding="utf-8")
    failed = RoleResult(ok=False, error="boom")
    out = runner_mod.Runner._read_result(rp, failed)
    assert out["_ok"] is False
    assert out["status"] == "failed"


def test_wait_for_deps_fast_fails_on_failed_dep(tmp_path, sample_spec_path):
    cfg = RunConfig(spec_path=sample_spec_path, project_dir=tmp_path / "p", mock=True)
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("spec", {}))
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}]))
    asyncio.run(sched.board.set_status("U1", FAILED))
    # U2 가 실패한 U1 에 의존 → 1800s 대기 없이 즉시 False
    ok = asyncio.run(sched._wait_for_deps({"id": "U2", "deps": ["U1"]}, timeout=5.0))
    assert ok is False


def test_run_subprocess_streams_to_log(tmp_path):
    # 긴 호출 중에도 출력이 실시간으로 로그파일에 쌓이도록 tee 한다.
    lp = tmp_path / "live.log"
    rc, out, err, timed_out = asyncio.run(
        run_subprocess([sys.executable, "-c", "print('L1');print('L2')"], ".", 10, lp)
    )
    assert rc == 0 and not timed_out
    assert lp.exists() and "L1" in lp.read_text() and "L2" in lp.read_text()


def test_run_alive_detects_pidfile(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    assert webui._run_alive(orch) is False  # pidfile 없음
    (orch / "run.pid").write_text(str(os.getpid()), encoding="utf-8")
    assert webui._run_alive(orch) is True  # 살아있는 PID
    (orch / "run.pid").write_text("999999999", encoding="utf-8")
    assert webui._run_alive(orch) is False  # 죽은 PID


def test_global_artifacts_tracked(tmp_path):
    board = Board(tmp_path / "p")
    asyncio.run(board.init("spec", {}))
    asyncio.run(board.add_global_artifacts(["docs/design/architecture.md", "docs/design/api.md"]))
    asyncio.run(board.add_global_artifacts(["docs/design/architecture.md"]))  # 중복 무시
    assert board.snapshot()["artifacts"] == [
        "docs/design/architecture.md",
        "docs/design/api.md",
    ]


def test_board_tracks_tokens(tmp_path):
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    asyncio.run(board.agent_update("backend-developer", tokens_add=1000))
    asyncio.run(board.agent_update("backend-developer", tokens_add=500))
    snap = board.snapshot()
    assert snap["total_tokens"] == 1500
    assert snap["agents"]["backend-developer"]["tokens"] == 1500


def test_parse_stream_result_extracts_tokens_cost_model():
    from orchestrator.backends.claude_cli import parse_stream_result

    out = b"\n".join(
        [
            b'{"type":"system","subtype":"init","model":"claude-x"}',
            b'{"type":"result","result":"done","total_cost_usd":0.5,'
            b'"usage":{"input_tokens":100,"output_tokens":20}}',
        ]
    )
    final, cost, model, tokens = parse_stream_result(out)
    assert final == "done" and cost == 0.5 and model == "claude-x" and tokens == 120


def test_run_role_never_raises_on_backend_exception(tmp_path, sample_spec_path, monkeypatch):
    class Boom:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            raise RuntimeError("backend exploded")

    monkeypatch.setattr(runner_mod, "get_backend", lambda n: Boom())
    cfg = RunConfig(
        spec_path=sample_spec_path,
        project_dir=tmp_path / "p",
        backend_priority=["boom"],
        retries=0,
    )
    board = Board(cfg.project_dir)
    board.spec_text = "s"
    asyncio.run(board.init("s", {}))
    # 예외가 전파되지 않고 실패 outcome 으로 수렴해야 한다 (gather 형제 취소 방지)
    out = asyncio.run(runner_mod.Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert out["_ok"] is False
