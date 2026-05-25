"""Audit 회귀 테스트 (scheduler.py / __main__.py).

대상 이슈: 3/4/5/6/7/22/39/40/71/98/120/121/130/131/102.
모두 offline·mock 전용이며 tmp_path 아래에만 쓴다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orchestrator.__main__ import build_config, main, parse_args
from orchestrator.board import BLOCKED, DESIGNED, FAILED, IN_PROGRESS, Board
from orchestrator.config import RunConfig
from orchestrator.scheduler import Scheduler

# ---- 헬퍼 ----------------------------------------------------------------


def _sched(tmp_path: Path, sample_spec_path: Path, **kw) -> Scheduler:
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        mock=True,
        poll_interval=600.0,
        **kw,
    )
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("spec", {}))
    return sched


# ---- #3 / #5: --max-units 미처리 unit 경고 + designed 유지 ----------------


def test_max_units_skipped_units_warn_and_stay_designed(tmp_path, sample_spec_path):
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "demo",
        mock=True,
        poll_interval=600.0,
        max_units=1,
    )
    # architect 가 1개만 만들면 검증이 안 되므로, 보드에 직접 2개를 넣고 슬라이싱을 강제하기 위해
    # 전체 run 을 돌린 뒤 결과를 검사한다. mock architect 는 2개 이상의 unit 을 만든다.
    snap = asyncio.run(Scheduler(cfg).run())
    units = snap["units"]
    designed = [u for u in units if u["status"] == DESIGNED]
    # max_units=1 → 1개만 처리, 나머지는 designed 로 남아야 한다(#5: failed 아님).
    assert designed, "max_units 로 잘린 unit 은 designed 로 남아야 함"
    # #3: 조용히 ok 로 끝나지 않도록 경고가 기록되어야 한다.
    assert any("max-units" in w for w in (snap.get("warnings") or []))
    # designed unit 은 failed 로 강제되지 않는다.
    assert all(u["status"] != FAILED for u in designed)


# ---- #4: 경고/failed/blocked 가 있으면 CLI 가 비정상 종료(1) -------------


def test_cli_exit_nonzero_on_max_units_warning(tmp_path, sample_spec_path):
    project_dir = tmp_path / "out"
    rc = main(
        [
            "--spec",
            str(sample_spec_path),
            "--project-dir",
            str(project_dir),
            "--mock",
            "--max-units",
            "1",
            "--poll-interval",
            "600",
        ]
    )
    # 미처리 unit(designed)이 경고로 표면화 → 자동화가 '미완 성공'으로 보지 않게 1.
    board = json.loads((project_dir / ".orchestrator" / "board.json").read_text(encoding="utf-8"))
    if any(u["status"] == DESIGNED for u in board["units"]):
        assert rc == 1
    else:
        # 단일 unit 스펙이면 슬라이싱이 없고 정상 완료 → 0 (정상 완료를 깨지 않음).
        assert rc == 0


def test_cli_exit_zero_on_clean_full_run(tmp_path, sample_spec_path):
    project_dir = tmp_path / "out"
    rc = main(
        [
            "--spec",
            str(sample_spec_path),
            "--project-dir",
            str(project_dir),
            "--mock",
            "--poll-interval",
            "600",
        ]
    )
    assert rc == 0  # 정상 완료는 그대로 0


# ---- #6: 알 수 없는 dep → BLOCKED(False) ---------------------------------


def test_unknown_dep_blocks_unit(tmp_path, sample_spec_path):
    sched = _sched(tmp_path, sample_spec_path)
    asyncio.run(sched.board.add_units([{"id": "U2", "title": "b"}]))  # U1 은 board 에 없음
    ok = asyncio.run(sched._wait_for_deps({"id": "U2", "deps": ["U1"]}))
    assert ok is False  # 존재하지 않는 dep → blocked (typo safety)
    assert any("U1" in w for w in sched.board.snapshot()["warnings"])  # 경고도 유지


def test_known_dep_still_satisfied(tmp_path, sample_spec_path):
    from orchestrator.board import DONE

    sched = _sched(tmp_path, sample_spec_path)
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}]))
    asyncio.run(sched.board.set_status("U1", DONE))
    ok = asyncio.run(sched._wait_for_deps({"id": "U2", "deps": ["U1"]}, timeout=5.0))
    assert ok is True  # 존재하고 완료된 dep 은 정상 통과 (정상 동작 유지)


# ---- #7: stall 진행신호는 dep 작업 에이전트로 한정 ------------------------


def test_stall_ignores_unrelated_agent_activity(tmp_path, sample_spec_path):
    """관련 없는 PM 활동이 계속 갱신돼도 dep 가 멈춰 있으면 stall 로 실패해야 한다."""
    sched = _sched(tmp_path, sample_spec_path)
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}]))
    asyncio.run(sched.board.set_status("U1", IN_PROGRESS))

    async def go() -> bool:
        # U1 과 무관한 PM 이 계속 활동(updated_at 갱신) → 이전 구현이면 idle 이 리셋돼 영원히 대기.
        async def churn():
            for _ in range(8):
                await sched.board.agent_update(
                    "project-manager", status="running", unit=None, activity="tick"
                )
                await asyncio.sleep(0.5)

        t = asyncio.create_task(churn())
        try:
            return await sched._wait_for_deps({"id": "U2", "deps": ["U1"]}, timeout=2.0)
        finally:
            t.cancel()

    ok = asyncio.run(go())
    assert ok is False  # 관련 없는 활동은 idle 타이머를 리셋하지 않음 → stall fail


def test_stall_keeps_waiting_when_dep_agent_active(tmp_path, sample_spec_path):
    """dep unit 을 작업 중인 에이전트가 살아있으면 계속 기다린다(견고성)."""
    sched = _sched(tmp_path, sample_spec_path)
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}]))
    asyncio.run(sched.board.set_status("U1", IN_PROGRESS))

    async def go() -> bool:
        async def working():
            for _ in range(6):
                # U1 을 작업 중인 dev 에이전트가 활동 → 살아있으니 stall 로 죽이면 안 됨.
                await sched.board.agent_update(
                    "frontend-developer", status="running", unit="U1", activity="dev"
                )
                await asyncio.sleep(0.5)

        t = asyncio.create_task(working())
        try:
            return await asyncio.wait_for(
                sched._wait_for_deps({"id": "U2", "deps": ["U1"]}, timeout=1.0),
                timeout=2.5,
            )
        except asyncio.TimeoutError:
            return True  # 계속 기다리는 중 = stall 로 죽지 않음 (기대 동작)
        finally:
            t.cancel()

    assert asyncio.run(go()) is True


# ---- #22: stop 설정 시 새 작업을 시작하지 않음 ----------------------------


def test_stop_skips_new_dev_attempt(tmp_path, sample_spec_path):
    sched = _sched(tmp_path, sample_spec_path)
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}]))
    sched._stop.set()  # graceful shutdown 요청

    called = {"n": 0}

    async def spy(role, unit=None):
        called["n"] += 1
        return {"_ok": True, "artifacts": [], "status": "done"}

    sched.runner.run_role = spy
    ok = asyncio.run(sched._develop_unit({"id": "U1"}, asyncio.Semaphore(1), 1))
    assert ok is False  # stop 이면 새 개발 시도 안 함
    assert called["n"] == 0  # role 호출 자체가 없어야 함


# ---- #98: architect 성공 + units 없음 → 경고 ------------------------------


def test_architect_no_units_warns(tmp_path, sample_spec_path):
    sched = _sched(tmp_path, sample_spec_path)

    async def empty_arch(role, unit=None):
        # 모든 design 역할이 성공하지만 units 는 비어있음 (설계 계약 위반).
        return {"_ok": True, "artifacts": [], "status": "done", "units": []}

    sched.runner.run_role = empty_arch
    sched._stop.set()  # 빌드 페이즈에서 새 작업 시작 안 하도록 (빠른 종료)
    snap = asyncio.run(sched.run())
    assert any("no units" in w for w in (snap.get("warnings") or []))
    # 폴백 core unit 은 여전히 생성된다 (폴백 유지).
    assert snap["units"], "폴백 core unit 이 있어야 함"


# ---- #121: dev role 예외 → unit blocked (전파 안 함) ----------------------


def test_dev_role_exception_marks_blocked(tmp_path, sample_spec_path):
    sched = _sched(tmp_path, sample_spec_path)
    asyncio.run(sched.board.add_units([{"id": "U1", "title": "a"}]))

    async def boom(role, unit=None):
        raise RuntimeError("backend regression")

    sched.runner.run_role = boom
    # 예외가 전파되지 않고 False 반환 + unit blocked.
    ok = asyncio.run(sched._develop_unit({"id": "U1"}, asyncio.Semaphore(1), 1))
    assert ok is False
    u = next(u for u in sched.board.units() if u["id"] == "U1")
    assert u["status"] == BLOCKED
    assert any("U1" in w for w in sched.board.snapshot()["warnings"])


# ---- #120: pipeline 예외에도 cleanup/report 가 항상 돈다 ------------------


def test_pipeline_exception_does_not_skip_cleanup(tmp_path, sample_spec_path):
    # max_attempts=2(유한): dev 가 영구 실패하면 retries 소진 후 FAILED 로 끝나야 검증 가능.
    # 기본 0(제품 완주 모드)은 의도적으로 무한 수리라 영구 실패 시 종료하지 않는다(#C1).
    sched = _sched(tmp_path, sample_spec_path, max_attempts=2)

    async def design_then_boom(role, unit=None):
        # architect 는 unit 2개 반환, dev 호출은 예외를 던진다.
        if unit is None and role == "architecture-engineer":
            return {
                "_ok": True,
                "artifacts": [],
                "status": "done",
                "units": [{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}],
            }
        if unit is None:
            return {"_ok": True, "artifacts": [], "status": "done", "units": []}
        raise RuntimeError("dev exploded")

    sched.runner.run_role = design_then_boom
    snap = asyncio.run(sched.run())
    # 예외가 나도 run 이 완주하고 report 가 생성되어야 한다.
    assert (sched.board.orch_dir / "report.md").exists()
    # 비정상 종료 unit 은 done 으로 오탐되지 않는다.
    assert all(u["status"] != "done" for u in snap["units"])


# ---- #130 / #131: 실패 시 phase/result 가 실패를 반영 ---------------------


def test_failed_units_reflected_in_phase_and_warnings(tmp_path, sample_spec_path):
    # max_attempts=2(유한): 영구 실패가 retries 소진 후 FAILED 로 수렴해야 phase/경고 검증 가능.
    # 기본 0 은 완료까지 무한 수리이므로 영구 실패 시나리오를 종료시키지 않는다(#C1).
    sched = _sched(tmp_path, sample_spec_path, max_attempts=2)

    async def design_then_fail(role, unit=None):
        if unit is None and role == "architecture-engineer":
            return {
                "_ok": True,
                "artifacts": [],
                "status": "done",
                "units": [{"id": "U1", "title": "a"}],
            }
        if unit is None:
            return {"_ok": True, "artifacts": [], "status": "done", "units": []}
        # dev 실패 → unit blocked
        return {"_ok": False, "artifacts": [], "status": "failed", "blockers": ["x"]}

    sched.runner.run_role = design_then_fail
    snap = asyncio.run(sched.run())
    # #131: phase 만 보는 소비자도 실패를 인지하도록 done 이 아니어야 한다.
    assert snap["phase"] == "failed"
    # #130: cicd/docs 가 돌더라도 result != ok 가 되도록 경고가 있어야 한다.
    assert snap.get("warnings")
    # report.md result 가 failed 를 반영.
    report = (sched.board.orch_dir / "report.md").read_text(encoding="utf-8")
    assert "failed" in report


# ---- #71: cross-check 경고는 effective pool 기준 -------------------------


def test_cross_check_no_warning_with_role_pin(tmp_path, sample_spec_path, capsys):
    # --backends 는 1개지만 역할핀으로 2종이 되면 교차가 성립 → 경고 없어야 함.
    a = parse_args(
        [
            "--spec",
            str(sample_spec_path),
            "--project-dir",
            str(tmp_path / "p"),
            "--cross-check",
            "--backends",
            "claude-cli",
            "--role-backend",
            "qa=codex",
        ]
    )
    build_config(a)
    err = capsys.readouterr().err
    assert "cross-check" not in err


def test_cross_check_warns_with_single_backend(tmp_path, sample_spec_path, capsys):
    a = parse_args(
        [
            "--spec",
            str(sample_spec_path),
            "--project-dir",
            str(tmp_path / "p"),
            "--cross-check",
            "--backends",
            "claude-cli",
        ]
    )
    build_config(a)
    err = capsys.readouterr().err
    assert "cross-check" in err  # distinct 백엔드 1종 → 경고 유지


# ---- #39 / #40: rerun.json 의 spec/project-dir 가 절대경로 ----------------


def test_rerun_argv_normalizes_paths(tmp_path, sample_spec_path, monkeypatch):
    import sys

    project_dir = tmp_path / "p"
    # 상대경로처럼 보이는 argv 를 흉내내되, cfg 는 절대경로로 resolve 됨.
    monkeypatch.setattr(
        sys,
        "argv",
        ["orchestrator", "--spec", "rel/spec.md", "--project-dir", "rel/out", "--mock"],
    )
    cfg = RunConfig(spec_path=sample_spec_path.resolve(), project_dir=project_dir, mock=True)
    sched = Scheduler(cfg)
    argv = sched._rerun_argv()
    # spec/project-dir 값이 cfg 의 절대경로로 치환되어야 한다.
    si = argv.index("--spec")
    pi = argv.index("--project-dir")
    assert argv[si + 1] == str(sample_spec_path.resolve())
    assert Path(argv[si + 1]).is_absolute()
    assert argv[pi + 1] == str(project_dir.resolve())
    assert Path(argv[pi + 1]).is_absolute()
    assert "--mock" in argv  # 다른 플래그는 유지


def test_rerun_json_written_with_absolute_paths(tmp_path, sample_spec_path, monkeypatch):
    import sys

    project_dir = tmp_path / "p"
    monkeypatch.setattr(
        sys,
        "argv",
        ["orchestrator", "--spec=rel.md", "--project-dir=rel", "--mock", "--poll-interval", "600"],
    )
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=project_dir,
        mock=True,
        poll_interval=600.0,
    )
    asyncio.run(Scheduler(cfg).run())
    rerun = json.loads((project_dir / ".orchestrator" / "rerun.json").read_text(encoding="utf-8"))
    joined = " ".join(rerun["argv"])
    # "--spec=ABS" 형태도 절대경로로 치환.
    assert f"--spec={sample_spec_path.resolve()}" in joined
    assert f"--project-dir={project_dir.resolve()}" in joined


# ---- #102: --web 는 base-dir(우선) 또는 project-dir 를 베이스로 사용 -------


def test_web_uses_base_dir_then_project_dir(tmp_path, monkeypatch):
    captured = {}

    def fake_serve(port, base, host):
        captured["base"] = base

    import orchestrator.webui as webui

    monkeypatch.setattr(webui, "serve", fake_serve)
    # base-dir 우선
    main(["--web", "--base-dir", str(tmp_path / "runs"), "--project-dir", str(tmp_path / "pd")])
    assert captured["base"] == tmp_path / "runs"
    # base-dir 없으면 project-dir 폴백
    main(["--web", "--project-dir", str(tmp_path / "pd")])
    assert captured["base"] == tmp_path / "pd"


def test_board_unused_import_guard():
    # Board 가 정상 import 되는지 (테스트 모듈 sanity).
    assert Board is not None
