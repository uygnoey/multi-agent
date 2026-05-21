"""audit2 runner 회귀 테스트 — #97(페이즈별 status 검증) 및 기 수정 이슈 재확인.

ONLY orchestrator/runner.py 를 다룬다. #97 을 새로 검증하고, 같은 파일이 책임지는
기 수정 이슈(#8/#11/#25/#107/#112/#93·94)가 현재 코드에서 유지되는지 함께 확인한다.
"""

from __future__ import annotations

import asyncio
import json

from orchestrator import runner as runner_mod
from orchestrator.backends.base import RoleResult
from orchestrator.board import Board
from orchestrator.config import RunConfig

# ── #97: 페이즈별 status 검증 ─────────────────────────────────────────────


def test_coerce_result_phase_none_keeps_baseline():
    # phase 미지정이면 기존 동작(baseline 화이트리스트) 그대로 — 기존 테스트 호환.
    ok = RoleResult(ok=True)
    for good in ("done", "tested", "passed", "complete", "dev_done", "designed"):
        assert runner_mod._coerce_result({"status": good}, ok)["_ok"] is True, good
    for bad in ("fail", "failure", "error", "incomplete", "blocked"):
        assert runner_mod._coerce_result({"status": bad}, ok)["_ok"] is False, bad


def test_coerce_result_dev_phase_rejects_wrong_status():
    # dev 역할이 'designed' 같은 다른 페이즈 status 를 보고하면 _ok=False (명백히 어긋난 조합).
    ok = RoleResult(ok=True)
    bad = runner_mod._coerce_result({"status": "designed"}, ok, phase="dev")
    assert bad["_ok"] is False
    assert any("phase 'dev'" in b for b in bad["blockers"])
    # dev 의 정상 status 는 통과
    for good in ("dev_done", "done", "passed", "complete"):
        out = runner_mod._coerce_result({"status": good}, ok, phase="dev")
        assert out["_ok"] is True, good


def test_coerce_result_test_phase_rejects_wrong_status():
    # test/qa 역할이 'dev_done' 을 보고하면 _ok=False, 'tested'/'passed'/'done' 은 통과.
    ok = RoleResult(ok=True)
    assert runner_mod._coerce_result({"status": "dev_done"}, ok, phase="test")["_ok"] is False
    for good in ("tested", "pass", "passed", "done"):
        assert runner_mod._coerce_result({"status": good}, ok, phase="test")["_ok"] is True, good


def test_coerce_result_design_phase_status_set():
    # design 페이즈: 'designed'/'done' 은 통과, 'dev_done' 은 어긋난 조합으로 거부.
    ok = RoleResult(ok=True)
    # testsheet-creator 처럼 units 없는 design 결과도 status 만 맞으면 통과(role 미지정).
    out = runner_mod._coerce_result({"status": "done"}, ok, phase="design")
    assert out["_ok"] is True
    assert runner_mod._coerce_result({"status": "dev_done"}, ok, phase="design")["_ok"] is False


def test_coerce_result_architect_requires_units():
    # 아키텍트(설계 핵심 역할): units 가 비면 status 가 맞아도 _ok=False (#97/#98 설계 계약).
    ok = RoleResult(ok=True)
    no_units = runner_mod._coerce_result(
        {"status": "designed"}, ok, phase="design", role="architecture-engineer"
    )
    assert no_units["_ok"] is False
    assert any("units" in b for b in no_units["blockers"])
    # units 가 있으면 통과
    with_units = runner_mod._coerce_result(
        {"status": "designed", "units": [{"id": "U1", "title": "a"}]},
        ok,
        phase="design",
        role="architecture-engineer",
    )
    assert with_units["_ok"] is True


def test_coerce_result_testsheet_no_units_ok_in_design():
    # 같은 design 페이즈라도 testsheet-creator 는 units 가 없어도 정상이어야 한다.
    ok = RoleResult(ok=True)
    out = runner_mod._coerce_result(
        {"status": "done"}, ok, phase="design", role="testsheet-creator"
    )
    assert out["_ok"] is True


def test_coerce_result_phase_does_not_loosen_blockers():
    # 페이즈가 맞아도 blocker 가 있으면 여전히 _ok=False (baseline 우선).
    ok = RoleResult(ok=True)
    out = runner_mod._coerce_result(
        {"status": "dev_done", "blockers": ["db down"]}, ok, phase="dev"
    )
    assert out["_ok"] is False
    assert "db down" in out["blockers"]


def test_coerce_result_unknown_phase_falls_back_to_baseline():
    # 알 수 없는 페이즈(supervisor 등 _PHASE_SUCCESS_STATUSES 미등록)는 baseline 만 적용.
    ok = RoleResult(ok=True)
    assert runner_mod._coerce_result({"status": "done"}, ok, phase="supervisor")["_ok"] is True


def test_read_result_passes_phase_through(tmp_path):
    # _read_result 가 phase/role 을 _coerce_result 로 전달하는지(아키텍트 units 계약) 확인.
    rp = tmp_path / "r.json"
    rp.write_text('{"status":"designed","units":[]}', encoding="utf-8")
    out = runner_mod.Runner._read_result(
        rp, RoleResult(ok=True), phase="design", role="architecture-engineer"
    )
    assert out["_ok"] is False  # units 비어있음 → 계약 위반
    rp.write_text('{"status":"designed","units":[{"id":"U1"}]}', encoding="utf-8")
    out2 = runner_mod.Runner._read_result(
        rp, RoleResult(ok=True), phase="design", role="architecture-engineer"
    )
    assert out2["_ok"] is True


def test_dev_role_passed_status_accepted(tmp_path):
    # 회귀 방지: dev 역할이 'passed' 를 써도(흔한 LLM 표현) 거부되지 않아야 한다(over-tighten 방지).
    ok = RoleResult(ok=True)
    assert runner_mod._coerce_result({"status": "passed"}, ok, phase="dev")["_ok"] is True


# ── 기 수정 이슈 재확인(이 파일 책임 범위) ────────────────────────────────


def test_already_fixed_107_non_dict_json(tmp_path):
    # #107: 비-객체 JSON([] / "done")은 계약 위반 → _ok=False.
    ok = RoleResult(ok=True)
    assert runner_mod._coerce_result([], ok)["_ok"] is False
    assert runner_mod._coerce_result("done", ok)["_ok"] is False


def test_already_fixed_11_artifact_path_validation():
    # #11: 절대경로/'..' traversal 아티팩트는 drop, 상대경로만 유지.
    ok = RoleResult(ok=True)
    out = runner_mod._coerce_result(
        {"status": "done", "artifacts": ["src/a.py", "/etc/passwd", "../x", "ok/b.py", 5]}, ok
    )
    assert out["artifacts"] == ["src/a.py", "ok/b.py"]


def test_already_fixed_25_unlink_guarded_by_results_dir(tmp_path):
    # #25: _under_results_dir 가 results_dir 밖 경로를 False 로 막는지 확인.
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    inside = board.results_dir / "dev__U1.json"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text("{}", encoding="utf-8")
    assert runner_mod._under_results_dir(inside, board) is True
    outside = tmp_path / "p" / "evil.json"
    outside.write_text("{}", encoding="utf-8")
    assert runner_mod._under_results_dir(outside, board) is False


def test_already_fixed_112_budget_blocks_before_backend(tmp_path, sample_spec_path, monkeypatch):
    # #112: 누적비용이 budget 이상이면 백엔드 호출 없이 blocked 반환(_ok=False).
    called = {"n": 0}

    class Spy:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            called["n"] += 1
            return RoleResult(ok=True)

    monkeypatch.setattr(runner_mod, "get_backend", lambda n: Spy())
    cfg = RunConfig(
        spec_path=sample_spec_path, project_dir=tmp_path / "p", backend_priority=["spy"], budget=1.0
    )
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    asyncio.run(board.add_cost(2.0))  # 예산 초과
    out = asyncio.run(runner_mod.Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert out["_ok"] is False and out["status"] == "blocked"
    assert called["n"] == 0  # 백엔드는 호출되지 않음


def test_full_mock_run_still_done_with_phase_checks(tmp_path, sample_spec_path):
    # 회귀 방지: 페이즈 검증을 추가해도 mock e2e 가 여전히 done 에 도달해야 한다.
    from orchestrator.scheduler import Scheduler

    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "demo",
        mock=True,
        poll_interval=600.0,
    )
    snap = asyncio.run(Scheduler(cfg).run())
    assert snap["phase"] == "done"
    for u in snap["units"]:
        assert u["status"] == "done", f"{u['id']} status={u['status']}"

    # 아키텍트 결과파일이 실제로 units 를 담고 _coerce_result 가 _ok 로 본다.
    arch_rp = tmp_path / "demo" / ".orchestrator" / "results" / "architecture-engineer__global.json"
    data = json.loads(arch_rp.read_text(encoding="utf-8"))
    out = runner_mod._coerce_result(
        data, RoleResult(ok=True), phase="design", role="architecture-engineer"
    )
    assert out["_ok"] is True and out["units"]
