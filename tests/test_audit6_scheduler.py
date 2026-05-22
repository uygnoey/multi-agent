"""round-6 회귀: scheduler 비용 정책.

- #18 test-engineer(테스트 작성) 실패 시 qa 를 건너뛰고(비용 절감), 시도가 남으면 dev 재작업.
- #19 설계(architecture-engineer) 실패 시 의미 없는 fallback U1 빌드를 만들지 않고 즉시 중단.

모두 offline·mock 전용이며 tmp_path 아래에만 쓴다. runner.run_role 을 가짜로 교체해
역할별 결과를 제어한다(실제 백엔드/네트워크 없음).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.board import DONE, FAILED
from orchestrator.config import RunConfig
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


# ---------------- #18: test-engineer 실패 → qa skip + rework ----------------
def test_test_engineer_failure_skips_qa(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=1))
        calls: list[str] = []

        async def fake(role, unit=None):
            calls.append(role)
            if role == "test-engineer":
                return {"_ok": False, "status": "failed", "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t"}])
        unit = sched.board.units()[0]
        await sched._test_unit(unit, asyncio.Semaphore(1), 1)
        return sched.board, calls

    board, calls = asyncio.run(scenario())
    assert "test-engineer" in calls
    assert "qa" not in calls  # te 실패 → qa 호출 안 함(비용 절감)
    u = board.units()[0]
    assert u["test_status"] == "fail"
    assert u["status"] == FAILED


def test_test_engineer_failure_reworks_when_attempts_remain(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=2))
        reworked: list[int] = []

        async def fake(role, unit=None):
            if role == "test-engineer":
                return {"_ok": False, "status": "failed", "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}

        async def fake_dev(unit, sem, attempt):
            reworked.append(attempt)  # 재작업 호출 추적(실제 dev 안 돌림)
            return False  # 재개발 실패로 두어 무한 재귀 방지

        sched.runner.run_role = fake
        sched._develop_unit = fake_dev
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t"}])
        unit = sched.board.units()[0]
        await sched._test_unit(unit, asyncio.Semaphore(1), 1)
        return reworked

    reworked = asyncio.run(scenario())
    # te 실패 + 시도 남음 → dev 재작업(attempt 2) 시도 ("실패 → 고쳐서 다시")
    assert reworked == [2]


def test_test_engineer_ok_runs_qa(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=1))
        calls: list[str] = []

        async def fake(role, unit=None):
            calls.append(role)
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t"}])
        unit = sched.board.units()[0]
        await sched._test_unit(unit, asyncio.Semaphore(1), 1)
        return sched.board, calls

    board, calls = asyncio.run(scenario())
    assert "qa" in calls  # te 성공 → qa 실행
    u = board.units()[0]
    assert u["test_status"] == "pass"
    assert u["status"] == DONE


# ---------------- #19: 설계 실패 → fallback 빌드 중단 ----------------
def test_design_failure_aborts_build(tmp_path, sample_spec_path):
    sched = Scheduler(_cfg(tmp_path, sample_spec_path))
    ran: list[str] = []

    async def fake(role, unit=None):
        if unit is None and role == "architecture-engineer":
            # 설계 실패
            return {
                "_ok": False,
                "status": "failed",
                "artifacts": [],
                "blockers": ["design boom"],
                "units": [],
            }
        ran.append(role)
        if unit is None:
            return {"_ok": True, "status": "done", "artifacts": [], "units": []}
        return {"_ok": True, "status": "done", "artifacts": []}

    sched.runner.run_role = fake
    snap = asyncio.run(sched.run())

    assert snap["phase"] == "failed"  # 설계 실패 → done 아님
    assert snap["units"] == []  # fallback U1 을 만들지 않는다
    assert "cicd" not in ran  # 빌드 후속 페이즈(cicd/docs) 미실행
    assert "docs-writer" not in ran
    assert any("빌드 중단" in w or "design" in w.lower() for w in (snap.get("warnings") or []))


def test_design_success_still_builds(tmp_path, sample_spec_path):
    # 회귀: 설계가 성공하면 정상적으로 빌드/문서 페이즈가 돌고 done 으로 끝난다(mock).
    snap = asyncio.run(Scheduler(_cfg(tmp_path, sample_spec_path)).run())
    assert snap["phase"] == "done"
    assert snap["units"]  # 정상 빌드에는 unit 이 있다
