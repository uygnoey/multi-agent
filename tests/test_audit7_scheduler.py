"""round-7 회귀: scheduler 스케줄링/동시성/재작업 안전성.

audit7 대상 3건 (모두 offline·mock, tmp_path 아래에만 기록):

1) stall 진행신호: scoped 에이전트가 없을 때 전체 에이전트(PM/PL tick) 활동이 idle 타이머를
   리셋하지 못하게 한다 → 진짜 멈춘 dep 가 stall 타임아웃에 도달(False→BLOCKED).
2) test/qa 동시성 캡: test-engineer+qa 호출을 _test_sem(크기=concurrency)으로 묶어 동시
   백엔드 세션 폭증을 막는다.
3) rework 직전 의존성 재검증: dep 이 그사이 FAILED/BLOCKED 로 무너지면 망가진 베이스 위에서
   재개발하지 않고 unit 을 BLOCKED 로 둔다.

test_audit6_scheduler.py 의 패턴(mock=True, sched.runner.run_role 교체)을 그대로 따른다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.board import BLOCKED, DONE, FAILED, IN_PROGRESS
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


# ---------------- 회귀: 전체 mock run 이 여전히 done 에 도달 ----------------
def test_full_mock_run_still_reaches_done(tmp_path, sample_spec_path):
    # audit7 변경(세마포어 도입 + rework 재검증)이 정상 경로를 깨지 않는지 sanity.
    snap = asyncio.run(Scheduler(_cfg(tmp_path, sample_spec_path)).run())
    assert snap["phase"] == "done"
    assert snap["units"]
    for u in snap["units"]:
        assert u["status"] == DONE, f"{u['id']} status={u['status']}"


# ---------------- #1: stall 진행신호 — scoped 없으면 전체 활동 무시 ----------------
def test_progress_sig_ignores_all_agent_activity_without_scope(tmp_path, sample_spec_path):
    """scoped 에이전트(current_unit ∈ pending)가 없으면, 무관한 에이전트의 updated_at
    변화가 진행 신호를 바꾸면 안 된다(= idle 타이머를 리셋하지 못함)."""
    sched = Scheduler(_cfg(tmp_path, sample_spec_path))

    pending = ["U1"]
    # dep unit U1 은 IN_PROGRESS 로 고정(상태/notes 불변).
    units = {"U1": {"id": "U1", "status": IN_PROGRESS, "notes": []}}

    # 무관한 PM 만 활동(어떤 unit 도 잡지 않음 → scoped 없음).
    agents_t0 = {"project-manager": {"current_unit": None, "updated_at": 100.0}}
    agents_t1 = {"project-manager": {"current_unit": None, "updated_at": 999.0}}  # tick 갱신

    sig0 = sched._dep_progress_sig(pending, units, agents_t0)
    sig1 = sched._dep_progress_sig(pending, units, agents_t1)
    # PM 의 updated_at 이 100→999 로 바뀌어도 dep 자체 상태/notes 가 그대로면 신호 동일.
    assert sig0 == sig1, "scoped 없을 때 전체 에이전트 활동이 진행 신호를 리셋하면 안 됨"


def test_progress_sig_uses_scoped_agent_when_present(tmp_path, sample_spec_path):
    """dep 을 직접 작업 중인 scoped 에이전트가 있으면 그 updated_at 으로 진행을 감지(기존 동작)."""
    sched = Scheduler(_cfg(tmp_path, sample_spec_path))
    pending = ["U1"]
    units = {"U1": {"id": "U1", "status": IN_PROGRESS, "notes": []}}

    a0 = {"frontend-developer": {"current_unit": "U1", "updated_at": 100.0}}
    a1 = {"frontend-developer": {"current_unit": "U1", "updated_at": 101.0}}
    assert sched._dep_progress_sig(pending, units, a0) != sched._dep_progress_sig(
        pending, units, a1
    )


def test_progress_sig_tracks_dep_notes_progress(tmp_path, sample_spec_path):
    """scoped 가 없어도 dep unit 자체에 진행(notes 증가)이 있으면 신호가 바뀐다."""
    sched = Scheduler(_cfg(tmp_path, sample_spec_path))
    pending = ["U1"]
    agents = {"project-manager": {"current_unit": None, "updated_at": 5.0}}
    u0 = {"U1": {"id": "U1", "status": IN_PROGRESS, "notes": ["a"]}}
    u1 = {"U1": {"id": "U1", "status": IN_PROGRESS, "notes": ["a", "b"]}}
    assert sched._dep_progress_sig(pending, u0, agents) != sched._dep_progress_sig(
        pending, u1, agents
    )


def test_stall_fires_despite_pm_ticks(tmp_path, sample_spec_path):
    """통합: 멈춘 dep + 무관한 PM tick 이 계속 갱신돼도 stall 타임아웃이 발화해 False 반환."""

    async def scenario() -> bool:
        sched = Scheduler(_cfg(tmp_path, sample_spec_path))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "a"}, {"id": "U2", "title": "b"}])
        await sched.board.set_status("U1", IN_PROGRESS)  # 멈춘 dep (이후 변화 없음)

        async def pm_churn():
            for _ in range(10):
                await sched.board.agent_update(
                    "project-manager", status="running", unit=None, activity="tick"
                )
                await asyncio.sleep(0.3)

        t = asyncio.create_task(pm_churn())
        try:
            return await sched._wait_for_deps({"id": "U2", "deps": ["U1"]}, timeout=1.5)
        finally:
            t.cancel()

    assert asyncio.run(scenario()) is False  # PM tick 이 stall 을 막지 못함


# ---------------- #2: test/qa 동시성 캡 (_test_sem) ----------------
def test_test_sem_exists_and_sized_by_concurrency(tmp_path, sample_spec_path):
    sched = Scheduler(_cfg(tmp_path, sample_spec_path, concurrency=2))
    assert isinstance(sched._test_sem, asyncio.Semaphore)
    # 초기 가용 슬롯 = concurrency (내부 _value 로 확인).
    assert sched._test_sem._value == 2


def test_concurrent_test_unit_calls_respect_cap(tmp_path, sample_spec_path):
    """많은 unit 의 _test_unit 을 동시에 돌려도 동시 qa 호출이 concurrency 캡을 넘지 않는다."""
    cap = 3
    n_units = 12

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, concurrency=cap, max_attempts=1))
        await sched.board.init("spec", {})
        units = [{"id": f"U{i}", "title": f"u{i}"} for i in range(n_units)]
        await sched.board.add_units(units)

        inflight = {"now": 0, "max": 0}
        lock = asyncio.Lock()

        async def fake(role, unit=None):
            if role in ("test-engineer", "qa"):
                async with lock:
                    inflight["now"] += 1
                    inflight["max"] = max(inflight["max"], inflight["now"])
                # 겹치도록 잠깐 양보/대기 (동시성 관찰 창 확보).
                await asyncio.sleep(0.05)
                async with lock:
                    inflight["now"] -= 1
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        sem = asyncio.Semaphore(1)  # dev 슬롯(여기선 쓰이지 않음 — max_attempts=1, 전부 통과)
        board_units = sched.board.units()
        await asyncio.gather(*[sched._test_unit(u, sem, 1) for u in board_units])
        return inflight["max"], sched.board

    max_inflight, board = asyncio.run(scenario())
    # _test_sem 이 test/qa 본작업을 캡 → 동시 in-flight 가 concurrency 를 넘지 않아야 한다.
    assert max_inflight <= cap, f"max in-flight {max_inflight} > cap {cap}"
    # 그리고 실제로 병렬은 일어났다(직렬화 아님) — sanity.
    assert max_inflight >= 2, "동시 실행이 전혀 없었음(테스트가 동시성을 관찰하지 못함)"
    for u in board.units():
        assert u["status"] == DONE


# ---------------- #3: rework 직전 의존성 재검증 ----------------
def test_rework_aborts_when_dep_failed(tmp_path, sample_spec_path):
    """qa 가 한 번 실패해 재작업으로 가려는데, 그사이 dep 가 FAILED 면 재개발하지 않고 BLOCKED."""

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=2))
        await sched.board.init("spec", {})
        # U1(dep) 은 이미 FAILED, U2 는 U1 에 의존.
        await sched.board.add_units(
            [{"id": "U1", "title": "dep"}, {"id": "U2", "title": "t", "deps": ["U1"]}]
        )
        await sched.board.set_status("U1", FAILED, "boom")

        reworked: list[int] = []

        async def fake(role, unit=None):
            if role == "qa":
                # te 성공 → qa 실행 → 실패(시도 남아 rework 경로로 진입 시도).
                return {"_ok": False, "status": "failed", "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}

        async def fake_dev(unit, sem, attempt):
            reworked.append(attempt)  # 재개발이 실제로 호출됐는지 추적
            return True

        sched.runner.run_role = fake
        sched._develop_unit = fake_dev

        unit = next(u for u in sched.board.units() if u["id"] == "U2")
        await sched._test_unit(unit, asyncio.Semaphore(1), 1)
        return sched.board, reworked

    board, reworked = asyncio.run(scenario())
    u2 = next(u for u in board.units() if u["id"] == "U2")
    assert u2["status"] == BLOCKED, (
        f"dep FAILED 시 rework 중단 후 BLOCKED 여야 함 (got {u2['status']})"
    )
    assert reworked == [], "dep 가 깨졌으면 재개발(_develop_unit)을 호출하면 안 됨"
    # 원인을 경고로 표면화.
    assert any("U2" in w and "U1" in w for w in (board.snapshot().get("warnings") or []))


def test_rework_proceeds_when_deps_ok(tmp_path, sample_spec_path):
    """회귀: dep 가 멀쩡하면 qa 실패 시 정상적으로 재작업(_develop_unit)을 호출한다."""

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=2))
        await sched.board.init("spec", {})
        from orchestrator.board import DONE as _DONE

        await sched.board.add_units(
            [{"id": "U1", "title": "dep"}, {"id": "U2", "title": "t", "deps": ["U1"]}]
        )
        await sched.board.set_status("U1", _DONE)  # dep 정상 완료

        reworked: list[int] = []

        async def fake(role, unit=None):
            if role == "qa":
                return {"_ok": False, "status": "failed", "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}

        async def fake_dev(unit, sem, attempt):
            reworked.append(attempt)
            return False  # 재개발 실패로 두어 무한 재귀 방지

        sched.runner.run_role = fake
        sched._develop_unit = fake_dev

        unit = next(u for u in sched.board.units() if u["id"] == "U2")
        await sched._test_unit(unit, asyncio.Semaphore(1), 1)
        return reworked

    reworked = asyncio.run(scenario())
    assert reworked == [2], "dep 정상이면 qa 실패 시 attempt 2 로 재작업해야 함"


def test_qa_test_harness_failure_routes_to_test_engineer_repair(tmp_path, sample_spec_path):
    """QA가 test/config 결함을 보고하면 다음 dev 재작업 전에 test-engineer 수리를 먼저 보낸다."""

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=2))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t"}])

        qa_calls = 0
        repair_contexts: list[str | None] = []

        async def fake(role, unit=None):
            nonlocal qa_calls
            if role == "test-engineer":
                repair_contexts.append(unit.get("repair_context") if unit else None)
                return {"_ok": True, "status": "done", "artifacts": ["tests/test_u1.py"]}
            if role == "qa":
                qa_calls += 1
                if qa_calls == 1:
                    return {
                        "_ok": False,
                        "status": "failed",
                        "artifacts": [],
                        "failure_kind": "test_harness",
                        "repair_owner": "test-engineer",
                        "repair_instruction": "move Payload model to module scope",
                        "blockers": ["pytest forward-ref failure"],
                    }
                return {"_ok": True, "status": "done", "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        unit = sched.board.units()[0]
        await sched._test_unit(unit, asyncio.Semaphore(1), 1)
        return sched.board, repair_contexts

    board, repair_contexts = asyncio.run(scenario())

    assert repair_contexts[0] is None
    assert repair_contexts[1] and "move Payload model" in repair_contexts[1]
    assert board.units()[0]["status"] == DONE
