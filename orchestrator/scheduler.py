"""asyncio 오케스트레이션 본체.

스캐폴딩 → 보드 초기화 → PM/PL 상시 감독(백그라운드) →
Phase A(설계+테스트시트 병렬) → Phase B/C(unit별 동시개발+테스트 트리거) →
CI/CD → graceful shutdown.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys

from . import workspace
from .board import (
    BLOCKED,
    DEV_DONE,
    DONE,
    FAILED,
    IN_PROGRESS,
    TERMINAL_OK,
    TESTING,
    Board,
)
from .config import DESIGN_ROLES, DEV_ROLES, SUPERVISOR_ROLES, RunConfig
from .runner import Runner

DEFAULT_STACK = {"frontend": "React/Vite", "backend": "FastAPI", "db": "SQLite"}


class Scheduler:
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.board = Board(cfg.project_dir)
        self.runner = Runner(cfg, self.board)
        self._stop = asyncio.Event()

    async def run(self) -> dict:
        spec_text = self.cfg.spec_path.read_text(encoding="utf-8")
        workspace.scaffold(self.cfg.project_dir, spec_text, DEFAULT_STACK)
        self.board.spec_text = spec_text
        await self.board.init(spec_text, DEFAULT_STACK)

        # 생존 확인용 PID 파일 (웹 서버 재시작에도 running 상태를 정확히 판단)
        pid_file = self.board.orch_dir / "run.pid"
        try:
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except Exception:
            pass
        # 재실행(rerun)용 커맨드 저장 (웹/CLI/TUI 어디서 띄웠든 동일하게 재실행 가능)
        try:
            (self.board.orch_dir / "rerun.json").write_text(
                json.dumps({"argv": sys.argv[1:]}), encoding="utf-8"
            )
        except Exception:
            pass

        # graceful shutdown on SIGINT/SIGTERM (stops supervisors; phases unwind via finally)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, ValueError):
                pass

        # 상시 감독 (백그라운드 태스크)
        sup_tasks = [asyncio.create_task(self._supervise(r)) for r in SUPERVISOR_ROLES]

        try:
            # Phase A — 설계 + 테스트시트 병렬
            await self.board.set_phase("design")
            await self.board.log_event("scheduler", "Phase A: design ‖ testsheet")
            design_outcomes = await asyncio.gather(*[self.runner.run_role(r) for r in DESIGN_ROLES])
            # 아키텍트 결과를 역할명으로 선택 (DESIGN_ROLES 순서가 바뀌어도 안전)
            by_role = dict(zip(DESIGN_ROLES, design_outcomes, strict=False))
            arch_outcome = by_role.get("architecture-engineer", design_outcomes[0])
            for o in design_outcomes:  # 설계/테스트시트 산출물 → 전역 노출
                await self.board.add_global_artifacts(o.get("artifacts", []))
            units = arch_outcome.get("units") or []
            if units:
                await self.board.add_units(units)
            if not self.board.units():
                # 아키텍트가 units 를 못 만든 경우 — 명시적으로 기록 후 폴백
                await self.board.log_event(
                    "scheduler", "architect produced no units; falling back to single core unit"
                )
                await self.board.add_units([{"id": "U1", "title": "core", "roles": DEV_ROLES}])

            # Phase B/C — unit별 동시 개발 + 완료 시 테스트 트리거
            unit_list = self.board.units()
            if self.cfg.max_units:
                unit_list = unit_list[: self.cfg.max_units]
            await self.board.set_phase("build")
            await self.board.log_event(
                "scheduler",
                f"Phase B/C: {len(unit_list)} unit(s), concurrency={self.cfg.concurrency}",
            )
            sem = asyncio.Semaphore(self.cfg.concurrency)
            test_tasks: list[asyncio.Task] = []

            async def pipeline(unit: dict) -> None:
                uid = unit["id"]
                if not await self._wait_for_deps(unit):
                    await self.board.set_status(uid, BLOCKED, "deps unmet or failed")
                    return
                # dev 가 끝나면(dev_done) test/qa 는 별도 태스크로 즉시 실행하고,
                # 개발 슬롯은 반납 → 개발은 곧바로 다음 unit 으로 진행한다.
                if await self._develop_unit(unit, sem, 1):
                    test_tasks.append(asyncio.create_task(self._test_unit(unit, sem, 1)))

            await asyncio.gather(*[pipeline(u) for u in unit_list])
            if test_tasks:  # 마지막 unit 들의 test/qa 완료 대기
                await asyncio.gather(*test_tasks, return_exceptions=True)

            # Phase D — CI/CD
            await self.board.set_phase("cicd")
            await self.board.log_event("scheduler", "Phase D: CI/CD")
            cicd_out = await self.runner.run_role("cicd")
            await self.board.add_global_artifacts(cicd_out.get("artifacts", []))

            # Phase E — 문서화: 실행 가이드(EN/KO) + 개발 산출물(EN/KO)
            await self.board.set_phase("docs")
            await self.board.log_event("scheduler", "Phase E: docs (EN/KO)")
            docs_out = await self.runner.run_role("docs-writer")
            await self.board.add_global_artifacts(docs_out.get("artifacts", []))
            # 보드 기반 산출물 문서는 백엔드와 무관하게 항상 EN/KO 생성
            await self.board.add_global_artifacts(self.board.write_deliverables())

            # 모든 작업 완료 → 감독(PM/PL)을 graceful 종료(현재 tick 끝까지 대기, 취소 X).
            # 감독이 다 멈춘 뒤에야 done — done 시점엔 어떤 에이전트도 돌고 있지 않다.
            await self.board.set_phase("finishing")
            self._stop.set()
            await asyncio.gather(*sup_tasks, return_exceptions=True)
            sup_tasks = []
            await self.board.set_phase("done")
        finally:
            self._stop.set()
            for t in sup_tasks:
                t.cancel()
            if sup_tasks:
                await asyncio.gather(*sup_tasks, return_exceptions=True)
            # (예외 경로 등) running 으로 남은 에이전트 정리
            for role, a in self.board.agents().items():
                if a.get("status") == "running":
                    await self.board.agent_update(role, status="idle", activity="run ended")
            self.board.write_report()
            try:
                (self.board.orch_dir / "run.pid").unlink()  # 종료 표시
            except Exception:
                pass

        return self.board.snapshot()

    async def _develop_unit(self, unit: dict, sem: asyncio.Semaphore, attempt: int) -> bool:
        """개발 3인(FE/BE/DBA) 동시 실행. 개발 동시성 슬롯(sem)은 이 동안만 점유.

        성공 시 dev_done 으로 두고 True. (test/qa 는 호출자가 별도 태스크로 돌린다.)
        """
        uid = unit["id"]
        dev_roles = [r for r in unit.get("roles", DEV_ROLES) if r in DEV_ROLES] or DEV_ROLES
        async with sem:
            await self.board.set_status(
                uid, IN_PROGRESS, f"dev attempt {attempt}/{self.cfg.max_attempts}"
            )
            dev_outcomes = await asyncio.gather(*[self.runner.run_role(r, unit) for r in dev_roles])
            for o in dev_outcomes:
                await self.board.add_artifacts(uid, o.get("artifacts", []))
            if any(not o.get("_ok", True) for o in dev_outcomes):
                await self.board.set_status(uid, BLOCKED, "dev failed")
                return False
            await self.board.set_status(uid, DEV_DONE)
            return True

    async def _test_unit(self, unit: dict, sem: asyncio.Semaphore, attempt: int) -> None:
        """unit 개발 완료 직후 test-engineer → qa 를 실행 (개발 슬롯 비점유).

        QA 실패 시 max_attempts 내에서 개발 슬롯을 다시 잡아 재작업 후 재검증.
        """
        uid = unit["id"]
        await self.board.set_status(uid, TESTING)
        te = await self.runner.run_role("test-engineer", unit)
        await self.board.add_artifacts(uid, te.get("artifacts", []))
        qa = await self.runner.run_role("qa", unit)
        await self.board.add_artifacts(uid, qa.get("artifacts", []))

        passed = qa.get("_ok", True) and qa.get("status") != "failed"
        await self.board.set_test_status(uid, "pass" if passed else "fail")
        if passed:
            await self.board.set_status(uid, DONE)
            return
        if attempt < self.cfg.max_attempts:
            await self.board.log_event(uid, f"QA failed → 재작업 (attempt {attempt + 1})")
            if await self._develop_unit(unit, sem, attempt + 1):
                await self._test_unit(unit, sem, attempt + 1)
                return
        await self.board.set_status(uid, FAILED, "QA failed after retries")

    async def _wait_for_deps(self, unit: dict, timeout: float | None = None) -> bool:
        """deps 가 모두 완료되면 True. 실패/blocked dep 이 있으면 즉시 False(패스트페일).

        존재하지 않는 dep id 는 무시한다. 타임아웃은 의존 unit 의 재작업(max_attempts)까지
        고려해 넉넉히 잡는다(빠른 실패는 FAILED/BLOCKED 로만, 오래 걸린다고 막지 않음).
        """
        deps = unit.get("deps", [])
        if not deps:
            return True
        uid = unit["id"]
        # 단순 시간초과가 아니라 '진행이 멈췄을 때'만 포기한다(stall). dep 가 아직 작업 중이면
        # (상태가 바뀌거나 에이전트가 활동 중이면) 계속 기다린다 — 한 role 호출이 상태를 붙잡는
        # 최대 시간(session_timeout)보다 넉넉한 윈도. timeout 인자를 주면 그 값을 stall 로 쓴다.
        stall = timeout if timeout is not None else max(1800.0, self.cfg.session_timeout * 2)
        prev_sig = None
        idle = 0.0
        while True:
            units = {u["id"]: u for u in self.board.units()}
            pending = [d for d in deps if d in units]  # 미지 dep 은 스킵
            if any(units.get(d, {}).get("status") in (FAILED, BLOCKED) for d in pending):
                await self.board.log_event(uid, f"deps failed/blocked → fast-fail: {pending}")
                return False
            if all(units.get(d, {}).get("status") in TERMINAL_OK for d in pending):
                return True
            # 진행 신호: dep 상태 + 에이전트 활동(updated_at). 변하면 작업이 살아있다는 뜻.
            agents = self.board.agents()
            sig = (
                tuple(units.get(d, {}).get("status") for d in pending),
                round(max((a.get("updated_at", 0.0) for a in agents.values()), default=0.0), 1),
            )
            if sig != prev_sig:
                prev_sig, idle = sig, 0.0  # 진행 있음 → 리셋 (살아있으면 막지 않음)
            else:
                idle += 1.0
                if idle >= stall:
                    await self.board.log_event(
                        uid, f"deps stalled {stall:.0f}s (no progress) → fail: {pending}"
                    )
                    return False
            await asyncio.sleep(1.0)

    async def _supervise(self, role: str) -> None:
        """PM/PL 상시 감독: 매 tick 마다 1-shot 리뷰 → 디렉티브 기록.

        루프 내 예외는 삼켜서 감독이 조용히 죽지 않게 한다(다음 tick 에 재시도).
        """
        try:
            while not self._stop.is_set():
                try:
                    outcome = await self.runner.run_role(role)
                    if self._stop.is_set():
                        break  # stop 이후 디렉티브를 쓰지 않음
                    notes = outcome.get("notes") or []
                    await self.board.append_directive(
                        role, "; ".join(notes) if notes else f"{role} review tick"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await self.board.log_event(role, f"supervisor error: {e}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
