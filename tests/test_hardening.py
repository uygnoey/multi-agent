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


def test_wait_for_deps_stalls_only_without_progress(tmp_path, sample_spec_path):
    from orchestrator.board import IN_PROGRESS

    cfg = RunConfig(spec_path=sample_spec_path, project_dir=tmp_path / "p", mock=True)
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("spec", {}))
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}]))
    asyncio.run(sched.board.set_status("U1", IN_PROGRESS))
    # 진행이 전혀 없으면 stall(=2s) 후 실패
    ok = asyncio.run(sched._wait_for_deps({"id": "U2", "deps": ["U1"]}, timeout=2.0))
    assert ok is False


def test_wait_for_deps_keeps_waiting_while_progressing(tmp_path, sample_spec_path):
    from orchestrator.board import DONE, IN_PROGRESS

    cfg = RunConfig(spec_path=sample_spec_path, project_dir=tmp_path / "p", mock=True)
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("spec", {}))
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}]))
    asyncio.run(sched.board.set_status("U1", IN_PROGRESS))

    async def go():
        async def finish():
            await asyncio.sleep(1.2)
            await sched.board.set_status("U1", DONE)  # 진행 → 완료

        t = asyncio.create_task(finish())
        ok = await sched._wait_for_deps({"id": "U2", "deps": ["U1"]}, timeout=5.0)
        await t
        return ok

    # dep 가 진행 중이면 stall(5s)에 걸리지 않고 완료를 기다려 True
    assert asyncio.run(go()) is True


def test_coerce_result_success_whitelist():
    ok = RoleResult(ok=True)
    for bad in ("fail", "failure", "error", "incomplete", "partial", "blocked"):
        assert runner_mod._coerce_result({"status": bad}, ok)["_ok"] is False, bad
    for good in ("done", "tested", "passed", "complete"):
        assert runner_mod._coerce_result({"status": good}, ok)["_ok"] is True, good


def test_normalize_role_handles_common_variants():
    from orchestrator.config import normalize_role

    assert normalize_role("backend developer") == "backend-developer"
    assert normalize_role("backend_developer") == "backend-developer"
    assert normalize_role("front end") == "frontend-developer"
    assert normalize_role("database-admin") == "dba"
    assert normalize_role("DevOps") == "cicd"


def test_runconfig_clamps_numeric_options(tmp_path, sample_spec_path):
    cfg = RunConfig(
        spec_path=sample_spec_path,
        project_dir=tmp_path / "p",
        concurrency=-10,
        max_attempts=0,
        retries=-5,
        max_units=0,
    )
    assert cfg.concurrency == 1 and cfg.max_attempts == 1 and cfg.retries == 0
    assert cfg.max_units is None  # 0/음수 → 제한 없음
    assert RunConfig(spec_path=sample_spec_path, project_dir=tmp_path / "q", max_units=-1).max_units is None


def test_add_units_ignores_dict_and_none_deps_roles(tmp_path):
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    asyncio.run(board.add_units([{"id": "U1", "title": "a", "deps": {"x": 1}, "roles": None}]))
    u = board.units()[0]
    assert u["deps"] == []  # dict → 무시 (repr 문자열화 X)
    assert u["roles"] == ["frontend-developer", "backend-developer", "dba"]  # None → 기본


def test_test_unit_safe_marks_unit_failed_on_exception(tmp_path, sample_spec_path):
    from orchestrator.board import FAILED

    cfg = RunConfig(spec_path=sample_spec_path, project_dir=tmp_path / "p", mock=True)
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("s", {}))
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}]))

    async def boom(role, unit=None):
        return None  # 비정상 반환 → _test_unit 내부에서 예외

    sched.runner.run_role = boom
    asyncio.run(sched._test_unit_safe({"id": "U1"}, asyncio.Semaphore(1)))
    u = next(u for u in sched.board.units() if u["id"] == "U1")
    assert u["status"] == FAILED  # 예외가 삼켜져 비종료로 남지 않고 실패 처리
    assert any("U1" in w for w in sched.board.snapshot()["warnings"])


def test_coerce_result_marks_failure_from_status_and_blockers():
    ok = RoleResult(ok=True)
    assert runner_mod._coerce_result({"status": "done"}, ok)["_ok"] is True
    assert runner_mod._coerce_result({"status": "failed"}, ok)["_ok"] is False
    assert runner_mod._coerce_result({"status": "FAILED"}, ok)["_ok"] is False  # 대소문자
    assert runner_mod._coerce_result({"status": "done", "blockers": ["x"]}, ok)["_ok"] is False
    assert runner_mod._coerce_result({"status": "DONE"}, ok)["status"] == "done"  # 정규화


def test_read_result_broken_json_is_failure(tmp_path):
    # 백엔드 ok=True 라도 결과파일이 깨졌으면 성공으로 오탐하지 않는다.
    rp = tmp_path / "r.json"
    rp.write_text("{not valid json", encoding="utf-8")
    out = runner_mod.Runner._read_result(rp, RoleResult(ok=True))
    assert out["_ok"] is False and out["status"] == "failed"


def test_add_units_handles_numeric_scalar_deps_roles(tmp_path):
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    asyncio.run(board.add_units([{"id": "U1", "title": "a", "deps": 1, "roles": 123}]))
    u = board.units()[0]
    assert u["deps"] == ["1"]  # 숫자 scalar 도 크래시 없이 정규화
    assert isinstance(u["roles"], list)


def test_wait_for_deps_warns_on_unknown_dep(tmp_path, sample_spec_path):
    cfg = RunConfig(spec_path=sample_spec_path, project_dir=tmp_path / "p", mock=True)
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("s", {}))
    asyncio.run(sched.board.add_units([{"id": "U2", "title": "b"}]))  # U1 은 board 에 없음
    ok = asyncio.run(sched._wait_for_deps({"id": "U2", "deps": ["U1"]}))
    assert ok is True  # 알 수 없는 dep 은 무한 대기 X
    assert any("U1" in w for w in sched.board.snapshot()["warnings"])  # 경고로 표면화


def test_add_units_normalizes_scalar_deps_and_roles(tmp_path):
    # architect 가 "deps":"U0", "roles":"backend-developer" 처럼 스칼라를 줘도 문자 분해되면 안 됨.
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    asyncio.run(
        board.add_units([{"id": "U1", "title": "a", "deps": "U0", "roles": "backend-developer"}])
    )
    u = board.units()[0]
    assert u["deps"] == ["U0"]  # ["U","0"] 아님
    assert u["roles"] == ["backend-developer"]  # 문자 단위 분해 아님


def test_warnings_recorded_and_reported(tmp_path):
    # 설계/CI/docs 실패는 경고로 기록되고 리포트에 'done with warnings' 로 표시돼야 함.
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    asyncio.run(board.add_warning("cicd failed"))
    assert board.snapshot()["warnings"] == ["cicd failed"]
    text = board.write_report().read_text(encoding="utf-8")
    assert "cicd failed" in text and "done with warnings" in text


def test_wait_for_deps_handles_none_session_timeout(tmp_path, sample_spec_path):
    # --timeout 0 → session_timeout=None. stall 계산에서 None*2 크래시하면 안 됨.
    from orchestrator.board import DONE

    cfg = RunConfig(
        spec_path=sample_spec_path, project_dir=tmp_path / "p", mock=True, session_timeout=None
    )
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("spec", {}))
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}]))
    asyncio.run(sched.board.set_status("U1", DONE))
    assert asyncio.run(sched._wait_for_deps({"id": "U2", "deps": ["U1"]})) is True


def test_test_engineer_failure_fails_unit(tmp_path, sample_spec_path):
    # test-engineer 가 실패하면 QA 가 통과해도 unit 은 done/pass 가 되면 안 됨.
    from orchestrator.board import FAILED

    cfg = RunConfig(spec_path=sample_spec_path, project_dir=tmp_path / "p", mock=True, max_attempts=1)
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("spec", {}))
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}]))

    async def fake(role, unit=None):
        ok = role != "test-engineer"
        return {"_ok": ok, "status": "tested" if ok else "failed", "artifacts": []}

    sched.runner.run_role = fake
    asyncio.run(sched._test_unit({"id": "U1"}, asyncio.Semaphore(1), 1))
    u = next(u for u in sched.board.units() if u["id"] == "U1")
    assert u["status"] == FAILED and u["test_status"] == "fail"


def test_run_role_failover_on_backend_exception(tmp_path, sample_spec_path, monkeypatch):
    # 후보가 예외를 던져도 다음 후보로 폴오버해야 한다 (전체 role 실패 X).
    from orchestrator.backends.mock import MockBackend

    class Boom:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            raise RuntimeError("boom")

    monkeypatch.setattr(runner_mod, "get_backend", lambda n: Boom() if n == "boom" else MockBackend())
    cfg = RunConfig(
        spec_path=sample_spec_path,
        project_dir=tmp_path / "p",
        backend_priority=["boom", "mock"],
        retries=0,
    )
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    out = asyncio.run(runner_mod.Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert out["_ok"] is True  # boom 예외 → mock 으로 폴오버 성공


def test_concurrency_zero_does_not_hang(tmp_path, sample_spec_path):
    # concurrency=0 이면 Semaphore(0) 로 영원히 멈출 수 있다 → 최소 1 로 클램프되어 완료돼야 함.
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "demo",
        mock=True,
        concurrency=0,
        poll_interval=600.0,
    )

    async def go():
        return await asyncio.wait_for(Scheduler(cfg).run(), timeout=30)

    snap = asyncio.run(go())
    assert snap["phase"] == "done"


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


def test_codex_cost_from_token_pricing():
    from orchestrator.backends.codex_cli import codex_cost

    # OpenAI 공식 표 기준 (uncached_input*price + cached*price + output*price)
    assert codex_cost("gpt-5.5", 100_000, 20_000, 10_000) == 0.71
    assert codex_cost("gpt-5.4-mini", 100_000, 20_000, 10_000) == 0.1065
    assert codex_cost("gpt-5.5-pro-2026", 1000, 0, 1000) is not None  # prefix 매칭
    assert codex_cost("unknown-model", 1, 1, 1) is None  # 단가표에 없으면 None


def test_codex_pricing_from_config_file_and_env(tmp_path, monkeypatch):
    from orchestrator.backends.codex_cli import codex_cost, load_pricing

    assert "gpt-5.5" in load_pricing()  # 동봉 JSON 에서 로드
    f = tmp_path / "px.json"
    f.write_text('{"gpt-5.5":[1.0,0.1,2.0]}', encoding="utf-8")
    monkeypatch.setenv("CODEX_PRICING_FILE", str(f))  # 환경변수로 단가표 교체
    assert codex_cost("gpt-5.5", 100_000, 0, 10_000) == 0.12


def test_board_cost_estimated_flag(tmp_path):
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    asyncio.run(board.agent_update("backend-developer", cost_add=1.0, cost_est=True))
    snap = board.snapshot()
    assert snap["cost_estimated"] is True
    assert snap["agents"]["backend-developer"]["cost_est"] is True


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
