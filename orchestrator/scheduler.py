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
import time

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

# 기본 스택은 특정 도메인(web 등)을 가정하지 않는다 — 아키텍트가 spec 을 보고 실제 스택을
# 결정한다. (이 값은 {{STACK}} 로 템플릿/보드에 문자열로만 들어가며 코드가 키를 읽지 않는다.)
DEFAULT_STACK = {"stack": "아키텍트가 spec 기반으로 결정 (architect decides from the spec)"}


class Scheduler:
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.board = Board(cfg.project_dir)
        self.runner = Runner(cfg, self.board)
        self._stop = asyncio.Event()
        # test/qa 동시성 캡: dev 와 별개로 test-engineer+qa 호출을 묶는 세마포어.
        # dev 슬롯(sem)만 캡되어 있고 test/qa 는 unit 마다 자유 태스크로 떠서, unit 이 많으면
        # 동시 백엔드 세션이 폭증할 수 있었다(과금/리소스). concurrency 에서 크기를 끌어와
        # test/qa 병렬도도 같은 상한으로 제한한다(0/음수 방어는 RunConfig 가 이미 함).
        self._test_sem = asyncio.Semaphore(max(1, self.cfg.concurrency))

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
        # --spec/--project-dir 의 상대경로 인자는 cfg 의 절대경로로 치환한다. 그래야 다른 cwd 에서
        # rerun 해도 같은 spec/타깃을 가리키며(#40), 상대경로가 raw 로 새지 않는다(#39).
        try:
            (self.board.orch_dir / "rerun.json").write_text(
                json.dumps({"argv": self._rerun_argv()}), encoding="utf-8"
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
            arch_outcome = by_role.get("architecture-engineer")
            if arch_outcome is None:
                await self.board.add_warning("architecture-engineer outcome missing; design failed")
                await self.board.set_phase("failed")
                return self.board.snapshot()
            if not arch_outcome.get("_ok", True):
                # #19: 설계(architecture-engineer) 실패 시, 의미 없는 fallback U1 빌드로 실비를
                #      쓰지 않고 즉시 중단한다. 설계 실패는 spec/설계를 고쳐 재실행해야 풀린다
                #      ("실패 → 고쳐서 다시"). 조기 return 이어도 finally 가 report 기록·감독
                #      정리·run.pid 제거를 수행하므로 run 상태는 깨끗하게 닫힌다.
                await self.board.add_warning(
                    f"design(architecture-engineer) failed: {arch_outcome.get('blockers') or '?'}"
                    " → 빌드 중단(fallback 미생성). spec/설계를 수정해 재실행하세요."
                )
                await self.board.log_event(
                    "scheduler", "design failed; aborting build (no fallback unit)"
                )
                await self.board.set_phase("failed")
                return self.board.snapshot()
            for o in design_outcomes:  # 성공한 설계/테스트시트 산출물만 전역 노출
                if o.get("_ok", True):
                    await self.board.add_global_artifacts(o.get("artifacts", []))
            units = arch_outcome.get("units") or []
            if units:
                await self.board.add_units(units)
            if not units and arch_outcome.get("_ok", True):
                # 설계 계약 위반: architect 가 '성공'했는데 units 가 비었음 → 경고로 표면화.
                # (성공으로 오해되면 안 됨. 폴백은 유지하되 result != ok 가 되도록 기록.)
                await self.board.add_warning(
                    "architecture-engineer succeeded but produced no units (design contract: "
                    "must decompose spec into units)"
                )
            if not self.board.units():
                # 아키텍트가 units 를 못 만든 경우 — 명시적으로 기록 후 폴백
                await self.board.log_event(
                    "scheduler", "architect produced no units; falling back to single core unit"
                )
                await self.board.add_units([{"id": "U1", "title": "core", "roles": DEV_ROLES}])

            # Phase B/C — unit별 동시 개발 + 완료 시 테스트 트리거
            unit_list = self.board.units()
            if self.cfg.max_units and self.cfg.max_units > 0:  # 음수면 슬라이싱 오작동 → 무시
                skipped_units = unit_list[self.cfg.max_units :]
                unit_list = unit_list[: self.cfg.max_units]
                if skipped_units:
                    # --max-units 로 잘린 unit 들은 designed 로 남는다. 조용히 'ok' 로 끝나지
                    # 않도록 경고로 표면화 (의도적 스킵이므로 failed 로 만들지는 않는다).
                    skipped_ids = [u["id"] for u in skipped_units]
                    await self.board.add_warning(
                        f"--max-units={self.cfg.max_units} 로 {len(skipped_ids)}개 unit 미처리"
                        f"(designed 유지): {skipped_ids}"
                    )
            await self.board.set_phase("build")
            await self.board.log_event(
                "scheduler",
                f"Phase B/C: {len(unit_list)} unit(s), concurrency={self.cfg.concurrency}",
            )
            sem = asyncio.Semaphore(max(1, self.cfg.concurrency))  # 0/음수면 hang → 최소 1
            test_tasks: list[asyncio.Task] = []

            async def pipeline(unit: dict) -> None:
                uid = unit["id"]
                if self._stop.is_set():  # graceful shutdown: 새 unit 작업을 시작하지 않음
                    await self.board.log_event(uid, "stop requested → skip (designed 유지)")
                    return
                if not await self._wait_for_deps(unit):
                    await self.board.set_status(uid, BLOCKED, "deps unmet or failed")
                    return
                # dev 가 끝나면(dev_done) test/qa 는 별도 태스크로 즉시 실행하고,
                # 개발 슬롯은 반납 → 개발은 곧바로 다음 unit 으로 진행한다.
                if await self._develop_unit(unit, sem, 1):
                    test_tasks.append(asyncio.create_task(self._test_unit_safe(unit, sem)))

            # pipeline 이 예기치 못한 예외를 던져도 아래 정리/리포트 블록이 항상 돌도록
            # return_exceptions=True (한 unit 의 내부 오류가 전체 cleanup 을 스킵시키지 않게).
            pipe_results = await asyncio.gather(
                *[pipeline(u) for u in unit_list], return_exceptions=True
            )
            for u, r in zip(unit_list, pipe_results, strict=False):
                if isinstance(r, Exception):
                    await self.board.set_status(u["id"], FAILED, f"pipeline error: {r}")
                    await self.board.add_warning(f"{u['id']}: 파이프라인 예외: {r}")
            if test_tasks:  # 마지막 unit 들의 test/qa 완료 대기
                await asyncio.gather(*test_tasks, return_exceptions=True)

            # 비종료 상태로 남은 unit 정리 (태스크 비정상 종료로 testing/in_progress 멈춘 경우 등).
            # done 으로 오탐하지 않도록 실패 처리 + 경고. (DESIGNED=미처리는 건드리지 않음)
            for u in self.board.units():
                if u["status"] in (IN_PROGRESS, DEV_DONE, TESTING):
                    await self.board.set_status(u["id"], FAILED, "left non-terminal")
                    await self.board.add_warning(f"{u['id']}: '{u['status']}' 상태로 비정상 종료")

            # CI/CD·docs 는 이후에도 돌지만(부분 산출물 생성 허용) 최종 result/phase 가 실패를
            # 반영하도록, failed/blocked unit 을 경고로 표면화한다. (QA 재시도 실패·deps blocked
            # 등 개별 경고가 없던 경로도 result != ok 가 되도록 요약 경고 기록.)
            broken = [u["id"] for u in self.board.units() if u["status"] in (FAILED, BLOCKED)]
            if broken:
                await self.board.add_warning(
                    f"빌드 미완료: {len(broken)}개 unit failed/blocked: {broken}"
                )
                # 결정(#28): cicd/docs 산출물은 부분이라도 유용하므로 계속 실행한다. 단, 불완전
                # 빌드 위에서 돌았음을 로그로 분명히 남겨 '완전 완료'로 오해받지 않게 한다.
                # (위 broken 경고 + 아래 still_broken→phase=failed 가 result≠ok 를 보장한다.)
                await self.board.log_event(
                    "scheduler",
                    f"cicd/docs run on INCOMPLETE build ({len(broken)} unit failed/blocked: "
                    f"{broken}); 산출물은 부분일 수 있으며 run 은 완전 완료가 아님",
                )

            # Phase D — CI/CD
            await self.board.set_phase("cicd")
            await self.board.log_event("scheduler", "Phase D: CI/CD")
            cicd_out = await self.runner.run_role("cicd")
            await self.board.add_global_artifacts(cicd_out.get("artifacts", []))
            if not cicd_out.get("_ok", True):
                await self.board.add_warning("cicd failed (배포 파이프라인 산출물 미완)")

            # Phase E — 문서화: 실행 가이드(EN/KO) + 개발 산출물(EN/KO)
            await self.board.set_phase("docs")
            await self.board.log_event("scheduler", "Phase E: docs (EN/KO)")
            docs_out = await self.runner.run_role("docs-writer")
            await self.board.add_global_artifacts(docs_out.get("artifacts", []))
            if not docs_out.get("_ok", True):
                await self.board.add_warning("docs-writer failed (산출물 문서 미완)")
            # 보드 기반 산출물 문서는 백엔드와 무관하게 항상 EN/KO 생성
            await self.board.add_global_artifacts(self.board.write_deliverables())

            # 모든 작업 완료 → 감독(PM/PL)을 graceful 종료(현재 tick 끝까지 대기, 취소 X).
            # 감독이 다 멈춘 뒤에야 done — done 시점엔 어떤 에이전트도 돌고 있지 않다.
            await self.board.set_phase("finishing")
            self._stop.set()
            await asyncio.gather(*sup_tasks, return_exceptions=True)
            sup_tasks = []
            # phase 만 보는 소비자가 실패 런을 'done' 으로 오해하지 않도록, failed/blocked unit 이
            # 하나라도 있으면 최종 phase 를 'failed' 로 둔다. (성공 런은 그대로 'done')
            still_broken = any(u["status"] in (FAILED, BLOCKED) for u in self.board.units())
            await self.board.set_phase("failed" if still_broken else "done")
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

    def _rerun_argv(self) -> list[str]:
        """rerun.json 에 저장할 argv 를 만든다 (--spec/--project-dir 는 절대경로로 정규화).

        다른 cwd 에서 rerun 해도 동일 spec/타깃을 가리키도록(#40), 상대경로 인자를
        cfg 의 resolve 된 절대경로로 치환한다.
        """
        argv = list(sys.argv[1:])
        replace = {
            "--spec": str(self.cfg.spec_path),
            "--project-dir": str(self.cfg.project_dir),
        }
        out: list[str] = []
        i = 0
        while i < len(argv):
            tok = argv[i]
            # "--spec=PATH" 형태
            if "=" in tok and tok.split("=", 1)[0] in replace:
                key = tok.split("=", 1)[0]
                out.append(f"{key}={replace[key]}")
                i += 1
                continue
            # "--spec PATH" 형태
            if tok in replace and i + 1 < len(argv):
                out.append(tok)
                out.append(replace[tok])
                i += 2
                continue
            out.append(tok)
            i += 1
        return out

    async def _develop_unit(self, unit: dict, sem: asyncio.Semaphore, attempt: int) -> bool:
        """개발 3인(FE/BE/DBA) 동시 실행. 개발 동시성 슬롯(sem)은 이 동안만 점유.

        성공 시 dev_done 으로 두고 True. (test/qa 는 호출자가 별도 태스크로 돌린다.)
        """
        uid = unit["id"]
        dev_roles = [r for r in unit.get("roles", DEV_ROLES) if r in DEV_ROLES] or DEV_ROLES
        async with sem:
            if self._stop.is_set():  # graceful shutdown: 새 개발 시도를 시작하지 않음
                await self.board.log_event(uid, "stop requested → skip dev attempt")
                return False
            await self.board.set_status(
                uid, IN_PROGRESS, f"dev attempt {attempt}/{self.cfg.max_attempts}"
            )
            # dev role 호출이 예외를 던져도 unit 을 blocked 로 내리고 파이프라인을 죽이지 않는다.
            dev_outcomes = await asyncio.gather(
                *[self.runner.run_role(r, unit) for r in dev_roles], return_exceptions=True
            )
            for o in dev_outcomes:
                if isinstance(o, Exception):
                    continue
                await self.board.add_artifacts(uid, o.get("artifacts", []))
            failed = any(isinstance(o, Exception) or not o.get("_ok", True) for o in dev_outcomes)
            if failed:
                await self.board.set_status(uid, BLOCKED, "dev failed")
                await self.board.add_warning(f"{uid}: 개발(dev) 실패 → blocked")
                return False
            await self.board.set_status(uid, DEV_DONE)
            return True

    async def _test_unit_safe(self, unit: dict, sem: asyncio.Semaphore) -> None:
        """test/qa 파이프라인 래퍼 — 예외가 gather 에서 조용히 삼켜져 unit 이 비종료로 남지 않게."""
        try:
            await self._test_unit(unit, sem, 1)
        except Exception as e:
            await self.board.set_status(unit["id"], FAILED, f"test pipeline error: {e}")
            await self.board.add_warning(f"{unit['id']}: test/qa 파이프라인 예외: {e}")

    def _broken_deps(self, unit: dict) -> list[str]:
        """unit 의 deps 중 현재 FAILED/BLOCKED 인 dep id 들을 반환 (경량 재검증용).

        _wait_for_deps 처럼 대기/타임아웃을 하지 않고, 스냅샷 한 번으로 '지금 깨진 dep' 만
        본다. 재작업(rework) 직전 호출해, 의존성이 그사이 무너졌으면 망가진 베이스 위에서
        다시 개발하지 않도록 한다.
        """
        deps = unit.get("deps") or []
        if not deps:
            return []
        units = {u["id"]: u for u in self.board.units()}
        return [d for d in deps if units.get(d, {}).get("status") in (FAILED, BLOCKED)]

    async def _test_unit(self, unit: dict, sem: asyncio.Semaphore, attempt: int) -> None:
        """unit 개발 완료 직후 test-engineer → qa 를 실행 (개발 슬롯 비점유).

        QA 실패 시 max_attempts 내에서 개발 슬롯을 다시 잡아 재작업 후 재검증.

        test/qa 백엔드 호출은 _test_sem 으로 동시성을 캡한다(dev 슬롯과 별개). rework 는
        _develop_unit 가 dev 슬롯(sem)을 다시 잡으므로, 데드락을 피하려고 _test_sem 을 먼저
        풀고 나서(아래 'with' 블록 밖에서) rework 를 수행한다(두 세마포어를 중첩 점유하지 않음).
        """
        uid = unit["id"]
        await self.board.set_status(uid, TESTING)

        # ---- test/qa 본작업: _test_sem 으로 동시성 캡 (이 블록 안에서만 점유) ----
        rework = False  # 블록 밖에서 재작업할지 여부 ('fail + 시도 남음')
        async with self._test_sem:
            te = await self.runner.run_role("test-engineer", unit)
            await self.board.add_artifacts(uid, te.get("artifacts", []))

            # #18: test-engineer(테스트 작성)가 실패하면 그 산출물 위에서 qa 비용을 쓰지 않는다.
            #      미통과로 표시하고, 시도가 남았으면 dev 부터 재작업해 '고쳐서' 다시 검증한다
            #      (단순 중단이 아니라 rework 경로 — "실패 → 버그픽스 → 재검증").
            te_ok = te.get("_ok", True) and te.get("status") != "failed"
            if not te_ok:
                await self.board.log_event(uid, "test-engineer 실패 → qa 건너뜀(비용 절감)")
                await self.board.set_test_status(uid, "fail")
                if attempt < self.cfg.max_attempts:
                    rework = True
                else:
                    await self.board.set_status(uid, FAILED, "test-engineer failed after retries")
                    return
            else:
                qa = await self.runner.run_role("qa", unit)
                await self.board.add_artifacts(uid, qa.get("artifacts", []))
                # test-engineer 가 성공한 경우에만 qa 를 돌렸으므로, 통과 판정은 qa 결과만 본다.
                passed = qa.get("_ok", True) and qa.get("status") != "failed"
                await self.board.set_test_status(uid, "pass" if passed else "fail")
                if passed:
                    await self.board.set_status(uid, DONE)
                    return
                if attempt < self.cfg.max_attempts:
                    rework = True
                else:
                    await self.board.set_status(uid, FAILED, "QA failed after retries")
                    return

        # ---- rework: _test_sem 점유를 푼 뒤 수행 (dev 슬롯과 중첩 점유 회피) ----
        if not rework:
            return
        # 재작업 전 의존성 재검증: 검증/대기 동안 dep 이 FAILED/BLOCKED 로 무너졌다면
        # 망가진 베이스 위에서 다시 개발하지 않는다. 이 unit 을 BLOCKED 로 두고 중단한다.
        broken = self._broken_deps(unit)
        if broken:
            await self.board.log_event(
                uid, f"rework 중단: 의존성이 failed/blocked 로 무너짐 → blocked: {broken}"
            )
            await self.board.set_status(uid, BLOCKED, f"rework aborted: deps broken {broken}")
            await self.board.add_warning(f"{uid}: 재작업 직전 의존성 붕괴 → blocked: {broken}")
            return
        await self.board.log_event(uid, f"재검증 통과 → 재작업 (attempt {attempt + 1})")
        if await self._develop_unit(unit, sem, attempt + 1):
            await self._test_unit(unit, sem, attempt + 1)
            return
        # 재개발이 실패하면 _develop_unit 이 이미 BLOCKED 로 두지만, '재시도 후 실패' 의미를
        # 명확히 남기도록 FAILED 로 마무리한다(기존 동작 유지: rework dev 실패 → failed).
        await self.board.set_status(uid, FAILED, "rework dev failed after retries")

    async def _wait_for_deps(self, unit: dict, timeout: float | None = None) -> bool:
        """deps 가 모두 완료되면 True. 실패/blocked dep 이 있으면 즉시 False(패스트페일).

        존재하지 않는 dep id 는 BLOCKED 로 처리한다(False). 타임아웃은 의존 unit 의
        재작업(max_attempts)까지 고려해 넉넉히 잡는다(빠른 실패는 FAILED/BLOCKED 로만,
        오래 걸린다고 막지 않음).
        """
        deps = unit.get("deps", [])
        if not deps:
            return True
        uid = unit["id"]
        # 보드에 없는 dep id 는 무시하지 않고 BLOCKED 로 처리한다(architect 누락/오타 안전장치).
        # 존재하지 않는 의존성을 만족했다고 보고 먼저 실행하면 필요한 선행 작업 없이 진행될 위험.
        known_ids = {u["id"] for u in self.board.units()}
        unknown = [d for d in deps if d not in known_ids]
        if unknown:
            await self.board.add_warning(f"{uid}: 존재하지 않는 의존성 → blocked: {unknown}")
            await self.board.log_event(uid, f"unknown deps → blocked: {unknown}")
            return False
        # 단순 시간초과가 아니라 '진행이 멈췄을 때'만 포기한다(stall). dep 가 아직 작업 중이면
        # (상태가 바뀌거나 에이전트가 활동 중이면) 계속 기다린다 — 한 role 호출이 상태를 붙잡는
        # 최대 시간(session_timeout)보다 넉넉한 윈도. timeout 인자를 주면 그 값을 stall 로 쓴다.
        base_to = self.cfg.session_timeout if self.cfg.session_timeout is not None else 1200.0
        stall = timeout if timeout is not None else max(1800.0, base_to * 2)
        prev_sig = None
        idle = 0.0
        last_check = time.monotonic()
        while True:
            units = {u["id"]: u for u in self.board.units()}
            pending = [d for d in deps if d in units]  # 미지 dep 은 스킵
            if any(units.get(d, {}).get("status") in (FAILED, BLOCKED) for d in pending):
                await self.board.log_event(uid, f"deps failed/blocked → fast-fail: {pending}")
                return False
            if all(units.get(d, {}).get("status") in TERMINAL_OK for d in pending):
                return True
            # 진행 신호 계산은 _dep_progress_sig 로 위임한다. 변하면 작업이 살아있다는 뜻.
            sig = self._dep_progress_sig(pending, units, self.board.agents())
            if sig != prev_sig:
                prev_sig, idle = sig, 0.0  # 진행 있음 → 리셋 (살아있으면 막지 않음)
            else:
                now = time.monotonic()
                idle += max(0.0, now - last_check)
                if idle >= stall:
                    await self.board.log_event(
                        uid, f"deps stalled {stall:.0f}s (no progress) → fail: {pending}"
                    )
                    return False
            last_check = time.monotonic()
            await asyncio.sleep(1.0)

    def _dep_progress_sig(self, pending: list[str], units: dict, agents: dict) -> tuple:
        """미완료 dep 들의 '진행 신호' 시그니처. 값이 바뀌면 작업이 살아있다는 뜻이다.

        진행 신호의 핵심은 idle 타이머를 언제 리셋하느냐다. 두 경우로 나눈다.

        1) dep unit 을 직접 작업 중인 에이전트(scoped: current_unit ∈ pending)가 있으면,
           그 에이전트들의 updated_at 을 신호에 포함한다 — 실제로 살아있으니 stall 로 죽이지 않는다.
        2) scoped 에이전트가 없으면(아무도 이 dep 을 잡고 있지 않음), 신호를 dep unit '자체의
           상태'로만 한정한다(상태 + notes 누적). 전체 에이전트(updated_at)로 폴백하지 않는다 —
           PM/PL 감독은 매 poll tick 마다 updated_at 을 갱신하므로, 전체 폴백을 쓰면 dep 가 진짜로
           멈춰 있어도 PM/PL tick 이 idle 타이머를 계속 리셋해 stall 타임아웃이 영영 발화하지 않는다
           (이게 이번 수정 대상 버그). dep 자체 상태/notes 만 보면 무관한 PM/PL tick 이 stall 을
           방해하지 못하고, 진짜 멈춘 dep 은 timeout 에 도달해 False(→ BLOCKED) 가 된다.
        """
        status_sig = tuple(units.get(d, {}).get("status") for d in pending)
        pending_set = set(pending)
        scoped = [a for a in agents.values() if a.get("current_unit") in pending_set]
        if scoped:
            # 기존 동작 유지: dep 을 작업 중인 에이전트의 updated_at 을 진행 신호로 사용.
            return (
                status_sig,
                round(max(a.get("updated_at", 0.0) for a in scoped), 1),
            )
        # scoped 에이전트 없음 → dep unit 자체의 상태/진행(notes)만으로 신호 구성.
        # (status 가 안 바뀌어도 notes 가 늘면 진행이 있는 것으로 본다. 전체 에이전트 활동은 무시.)
        notes_sig = tuple(len(units.get(d, {}).get("notes") or ()) for d in pending)
        return (status_sig, notes_sig)

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
