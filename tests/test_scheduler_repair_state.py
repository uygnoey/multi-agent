from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.board import BLOCKED, DONE, FAILED
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


def test_unlimited_dev_repair_escalates_and_continues_until_done(tmp_path, sample_spec_path):
    """제품 완주 모드(max_attempts=0): 동일 dev 실패가 반복돼도 '포기'하지 않고(BLOCKED 중단 X)
    수리 전략을 에스컬레이션하며 계속 진행해, dev 가 결국 고쳐지면 완주(DONE)한다. (#C1)"""

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])
        dev_calls = 0
        repair_contexts: list[str | None] = []

        async def fake(role, unit=None):
            nonlocal dev_calls
            if role == "frontend-developer":
                dev_calls += 1
                repair_contexts.append(unit.get("repair_context") if unit else None)
                if dev_calls <= 5:  # 직전과 '동일한' 환경 실패가 5회 반복
                    return {
                        "_ok": False,
                        "status": "failed",
                        "artifacts": [],
                        "blockers": ["same environment failure"],
                        "failure_kind": "dependency_env",
                    }
                return {"_ok": True, "status": "done", "artifacts": []}  # 6번째에 고쳐짐
            return {"_ok": True, "status": "done", "artifacts": []}  # test-engineer/qa 통과

        sched.runner.run_role = fake
        unit = sched.board.units()[0]
        ok = await sched._develop_unit(unit, asyncio.Semaphore(1), 1)
        assert ok is False
        await sched._repair_failed_dev(unit, asyncio.Semaphore(1), 1)
        return sched.board.snapshot(), dev_calls, repair_contexts

    snap, dev_calls, repair_contexts = asyncio.run(scenario())
    # 옛 동작은 3회에서 BLOCKED 로 포기했지만, 이제 계속 진행해 결국 완주한다.
    assert dev_calls >= 6
    assert snap["units"][0]["status"] == DONE
    # '동일 실패 → 자동 수리 중단' 경고가 더 이상 없어야 한다(포기 금지).
    assert not any("자동 수리 중단" in w for w in snap.get("warnings", []))
    # 반복 실패 후 수리 컨텍스트에 에스컬레이션 지시가 주입돼 매 시도가 달라진다(토큰 낭비 방지).
    assert any("[수리 에스컬레이션]" in (rc or "") for rc in repair_contexts)


def test_dev_failure_signature_is_stable_against_varying_free_text(tmp_path, sample_spec_path):
    """LLM 이 매번 다른 repair_instruction/notes 를 내도, 실제 장애가 같으면 반복 카운트가
    리셋되지 않고 누적돼 에스컬레이션이 발화한다(예전 무한 동일반복 버그 회귀 방지). (#C1)"""
    sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
    for i in range(3):
        sched._remember_dev_failure(
            "U1",
            [
                {
                    "_ok": False,
                    "status": "failed",
                    "failure_kind": "dependency_env",
                    "command": "npm run build",
                    "repair_instruction": f"시도 {i}: 매번 다른 자유 텍스트 {i * 7}",
                    "notes": [f"무작위 노트 {i}"],
                    "blockers": [f"blocker text {i}"],
                }
            ],
        )
    assert sched._dev_failure_repeat_count("U1") == 3  # 자유 텍스트가 달라도 카운트 누적
    assert sched._escalation_note("U1") is not None


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


def test_qa_repeated_failure_escalates_and_completes_without_recursion(tmp_path, sample_spec_path):
    """#H03/#H04: dev 는 성공하나 QA 가 반복 실패해도 (예전엔 상호 재귀로 RecursionError) 단일
    루프로 계속 → 결국 통과(DONE). QA 반복 실패에도 에스컬레이션이 주입된다."""

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])
        qa_calls = 0
        qa_contexts: list[str | None] = []

        async def fake(role, unit=None):
            nonlocal qa_calls
            if role == "qa":
                qa_calls += 1
                qa_contexts.append(unit.get("repair_context") if unit else None)
                if qa_calls <= 5:  # QA 가 5회 실패(소스 버그) 후 통과
                    return {
                        "_ok": False,
                        "status": "failed",
                        "artifacts": [],
                        "blockers": ["assertion failed in spec"],
                    }
                return {"_ok": True, "status": "done", "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}  # te/dev 는 성공

        sched.runner.run_role = fake
        await sched._test_unit(sched.board.units()[0], asyncio.Semaphore(1), 1)
        return sched.board.snapshot(), qa_calls, qa_contexts

    snap, qa_calls, qa_contexts = asyncio.run(scenario())
    assert qa_calls >= 6  # 재귀 아닌 루프로 계속 → 결국 통과 (RecursionError 없음)
    assert snap["units"][0]["status"] == DONE
    assert any("[수리 에스컬레이션]" in (c or "") for c in qa_contexts)  # #H04 QA 경로 에스컬레이션


def test_external_blocker_classified_and_stops(tmp_path, sample_spec_path):
    """#H09: 코드 수리로 못 고치는 고신뢰 외부 장애(구조화된 failure_kind=tool_missing)가 반복되면
    무한 반복 대신 external 로 분류·중단(BLOCKED)한다. (반복 게이트 2회 후 — 단발로는 포기 X.)

    #RA3: 원문 텍스트 "command not found" 는 더 이상 external 신호가 아니므로(고칠 수 있는 의존성·
    스크립트 문제), 구조화된 failure_kind 로 고신뢰 외부 분류가 여전히 동작함을 검증한다."""

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
                "failure_kind": "tool_missing",  # Tier A 구조화 신호(신뢰)
                "blockers": ["npx: command not found"],
            }

        sched.runner.run_role = fake
        unit = sched.board.units()[0]
        assert await sched._develop_unit(unit, asyncio.Semaphore(1), 1) is False
        await sched._repair_failed_dev(unit, asyncio.Semaphore(1), 1)
        return sched.board.snapshot(), calls

    snap, calls = asyncio.run(scenario())
    assert snap["units"][0]["status"] == BLOCKED
    assert any("external" in (w or "").lower() for w in snap.get("warnings", []))
    assert calls < 10  # 무한 루프가 아니라 반복 게이트 후 분류·중단


def test_command_not_found_text_is_not_external_blocker(tmp_path, sample_spec_path):
    """#RA3: 원문 "command not found"(vite/pytest 등 고칠 수 있는 의존성·스크립트 문제)는 더 이상
    external 텍스트 신호가 아니다 — 구조화된 failure_kind 가 없으면 EXTERNAL_BLOCKED 로 단정하지
    않고 계속 수리한다(조기 포기 방지)."""
    sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
    # 좁아진 텍스트 패턴은 더 이상 잡지 않는다.
    for txt in (
        "npx: command not found",
        "unauthorized",
        "permission denied",
        "operation not permitted",
        "read-only file system",
    ):
        assert sched._external_blocker_reason([{"blockers": [txt]}]) is None
    # 고신뢰 키 부재 텍스트는 external 로 잡되, 일반 앱 인증 실패에도 흔한
    # "authentication failed" 는 자유 텍스트만으로 external 로 단정하지 않는다.
    assert sched._external_blocker_reason([{"blockers": ["missing api key"]}]) == "missing api key"
    assert sched._external_blocker_reason([{"notes": ["authentication failed"]}]) is None
    # 구조화된 failure_kind 는 그대로 Tier A(신뢰).
    assert sched._external_blocker_reason([{"failure_kind": "tool_missing"}]) == "tool_missing"
    assert sched._external_blocker_reason([{"failure_kind": "permission_denied"}]) is not None


def test_dev_outcome_missing_ok_is_treated_as_failed(tmp_path, sample_spec_path):
    """#RA-devok: _ok 키가 빠진(악성/누락) dev outcome 은 성공이 아니라 실패로 판정해야 한다
    (test/qa 게이트와 동일하게 기본 False)."""

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=2))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])

        async def fake(role, unit=None):
            # _ok 키 자체가 없는 malformed outcome.
            return {"status": "done", "artifacts": [], "blockers": ["malformed: no _ok key"]}

        sched.runner.run_role = fake
        ok = await sched._develop_unit(sched.board.units()[0], asyncio.Semaphore(1), 1)
        return ok, sched.board.snapshot()

    ok, snap = asyncio.run(scenario())
    assert ok is False  # _ok 누락 → 실패
    assert snap["units"][0]["status"] == BLOCKED
    # 실패로 기록돼 dev 실패 시그니처가 남는다(누락 outcome 을 통과로 보지 않는다).
    assert any("개발(dev) 실패" in (w or "") for w in snap.get("warnings", []))


def test_remember_dev_failure_counts_missing_ok_outcome(tmp_path, sample_spec_path):
    """#RA-devok: _remember_dev_failure 도 _ok 누락 outcome 을 실패로 집계해 반복 카운트에
    반영한다(에스컬레이션 게이트가 정상 동작)."""
    sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
    for _ in range(2):
        sched._remember_dev_failure(
            "U1",
            [{"status": "failed", "failure_kind": "dependency_env", "command": "npm run build"}],
        )
    assert sched._dev_failure_repeat_count("U1") == 2  # _ok 없어도 실패로 누적


def test_escalation_header_preserved_in_long_repair_context(tmp_path, sample_spec_path):
    """#RA4: 매우 긴 본문 + 에스컬레이션이면 예전엔 끝 자르기(body[-MAX:])로 앞에 붙인
    에스컬레이션 헤더가 통째로 잘렸다. 이제 헤더는 보존되고 base 본문의 꼬리만 잘린다."""
    sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
    long_blocker = "X" * 8000  # _MAX_REPAIR_CONTEXT_CHARS(4000)를 훨씬 초과
    escalation = sched._escalation_text(3, "dev")
    assert escalation is not None
    repaired = sched._with_repair_context(
        {"id": "U1", "title": "t"},
        {"_ok": False, "status": "failed", "blockers": [long_blocker]},
        "dev",
        escalation=escalation,
    )
    ctx = repaired["repair_context"]
    assert "[수리 에스컬레이션]" in ctx  # 헤더가 살아있어야 함(예전엔 끝 자르기로 사라졌다)
    assert ctx.startswith("[수리 에스컬레이션]")  # 헤더가 맨 앞에 보존
    # 길이 예산은 여전히 지켜진다.
    from orchestrator.scheduler import _MAX_REPAIR_CONTEXT_CHARS

    assert len(ctx) <= _MAX_REPAIR_CONTEXT_CHARS


def test_failure_signatures_cleared_on_terminal_states(tmp_path, sample_spec_path):
    """#RA5: unit 이 terminal(DONE/FAILED/BLOCKED)에 도달하면 dev/verify 실패 시그니처가
    정리돼 누수/오염되지 않는다."""

    async def done_scenario():
        # DONE 경로: _test_unit 에서 통과 시 verify 시그니처 정리.
        sched = Scheduler(_cfg(tmp_path / "a", sample_spec_path, max_attempts=2))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t"}])
        qa_calls = 0

        async def fake(role, unit=None):
            nonlocal qa_calls
            if role == "qa":
                qa_calls += 1
                if qa_calls == 1:
                    return {"_ok": False, "status": "failed", "blockers": ["x"], "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        await sched._test_unit(sched.board.units()[0], asyncio.Semaphore(1), 1)
        return sched

    async def failed_scenario():
        # FAILED 경로: dev 재작업이 attempts 소진으로 실패하면 dev 시그니처 정리.
        sched = Scheduler(_cfg(tmp_path / "b", sample_spec_path, max_attempts=2))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])

        async def fake(role, unit=None):
            return {"_ok": False, "status": "failed", "blockers": ["x"], "artifacts": []}

        sched.runner.run_role = fake
        unit = sched.board.units()[0]
        assert await sched._develop_unit(unit, asyncio.Semaphore(1), 1) is False
        await sched._repair_failed_dev(unit, asyncio.Semaphore(1), 1)
        return sched

    done_sched = asyncio.run(done_scenario())
    assert done_sched.board.units()[0]["status"] == DONE
    assert "U1" not in done_sched._verify_failure_signatures
    assert "U1" not in done_sched._dev_failure_signatures
    assert "U1" not in done_sched._last_dev_failure

    failed_sched = asyncio.run(failed_scenario())
    assert failed_sched.board.units()[0]["status"] == FAILED
    assert "U1" not in failed_sched._dev_failure_signatures
    assert "U1" not in failed_sched._last_dev_failure
    assert "U1" not in failed_sched._verify_failure_signatures
