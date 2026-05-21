"""asyncio 오케스트레이션 본체.

스캐폴딩 → 보드 초기화 → PM/PL 상시 감독(백그라운드) →
Phase A(설계+테스트시트 병렬) → Phase B/C(unit별 동시개발+테스트 트리거) →
CI/CD → graceful shutdown.
"""

from __future__ import annotations

import asyncio
import signal

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
            await asyncio.gather(*[self._process_unit(u, sem) for u in unit_list])

            # Phase D — CI/CD
            await self.board.set_phase("cicd")
            await self.board.log_event("scheduler", "Phase D: CI/CD")
            await self.runner.run_role("cicd")
            await self.board.set_phase("done")
        finally:
            self._stop.set()
            for t in sup_tasks:
                t.cancel()
            await asyncio.gather(*sup_tasks, return_exceptions=True)
            self.board.write_report()

        return self.board.snapshot()

    async def _process_unit(self, unit: dict, sem: asyncio.Semaphore) -> None:
        uid = unit["id"]
        if not await self._wait_for_deps(unit):
            await self.board.set_status(uid, BLOCKED, "deps unmet or failed")
            return
        async with sem:
            dev_roles = [r for r in unit.get("roles", DEV_ROLES) if r in DEV_ROLES] or DEV_ROLES
            passed = False
            for attempt in range(1, self.cfg.max_attempts + 1):
                await self.board.set_status(
                    uid, IN_PROGRESS, f"attempt {attempt}/{self.cfg.max_attempts}"
                )

                # 개발 3인 동시 실행
                dev_outcomes = await asyncio.gather(
                    *[self.runner.run_role(r, unit) for r in dev_roles]
                )
                for o in dev_outcomes:
                    await self.board.add_artifacts(uid, o.get("artifacts", []))
                if any(not o.get("_ok", True) for o in dev_outcomes):
                    if attempt < self.cfg.max_attempts:
                        await self.board.log_event(uid, "dev failed; reworking")
                        continue
                    await self.board.set_status(uid, BLOCKED, "dev failed after retries")
                    return
                await self.board.set_status(uid, DEV_DONE)

                # Phase C — 테스트 코드 → QA 검증
                await self.board.set_status(uid, TESTING)
                te = await self.runner.run_role("test-engineer", unit)
                await self.board.add_artifacts(uid, te.get("artifacts", []))
                qa = await self.runner.run_role("qa", unit)
                await self.board.add_artifacts(uid, qa.get("artifacts", []))

                passed = qa.get("_ok", True) and qa.get("status") != "failed"
                await self.board.set_test_status(uid, "pass" if passed else "fail")
                if passed:
                    await self.board.set_status(uid, DONE)
                    break
                if attempt < self.cfg.max_attempts:
                    await self.board.log_event(uid, f"QA failed; rework attempt {attempt + 1}")

            if not passed:
                await self.board.set_status(uid, FAILED, "QA failed after retries")

    async def _wait_for_deps(self, unit: dict, timeout: float = 1800.0) -> bool:
        """deps 가 모두 완료되면 True. 실패/blocked dep 이 있으면 즉시 False(패스트페일).

        존재하지 않는 dep id 는 무시한다. 타임아웃 시 False.
        """
        deps = unit.get("deps", [])
        if not deps:
            return True
        uid = unit["id"]
        waited = 0.0
        while waited < timeout:
            status = {u["id"]: u["status"] for u in self.board.units()}
            pending = [d for d in deps if d in status]  # 미지 dep 은 스킵
            if any(status.get(d) in (FAILED, BLOCKED) for d in pending):
                await self.board.log_event(uid, f"deps failed/blocked → fast-fail: {pending}")
                return False
            if all(status.get(d) in TERMINAL_OK for d in pending):
                return True
            await asyncio.sleep(1.0)
            waited += 1.0
        await self.board.log_event(uid, f"deps timeout after {timeout:.0f}s: {deps}")
        return False

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
