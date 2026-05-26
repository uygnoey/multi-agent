"""asyncio 오케스트레이션 본체.

스캐폴딩 → 보드 초기화 → PM/PL 상시 감독(백그라운드) →
Phase A(설계+테스트시트 병렬) → Phase B/C(unit별 동시개발+테스트 트리거) →
CI/CD → graceful shutdown.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import signal
import sys
import time

from . import procutil, workspace
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
from .gitcheckpoints import GitCheckpointer
from .runner import Runner

# 기본 스택은 특정 도메인(web 등)을 가정하지 않는다 — 아키텍트가 spec 을 보고 실제 스택을
# 결정한다. (이 값은 {{STACK}} 로 템플릿/보드에 문자열로만 들어가며 코드가 키를 읽지 않는다.)
DEFAULT_STACK = {"stack": "아키텍트가 spec 기반으로 결정 (architect decides from the spec)"}
MAX_SPEC_BYTES = 4 * 1024 * 1024

_TEST_REPAIR_KINDS = {"test_harness", "test_config", "dependency_env"}
_TEST_REPAIR_WORDS = (
    "test-harness",
    "test harness",
    "test defect",
    "test_config",
    "test config",
    "config defect",
    "vitest",
    "pytest",
    "forwardref",
    "forward-ref",
    "pydanticusererror",
    "no test files found",
)
_MAX_REPAIR_CONTEXT_CHARS = 4000

# #H09 external blocker classification — 코드 수리로 풀 수 없는 '고신뢰 영구 외부 장애'(Tier A)만
# 분류한다. 오분류 시 사용자가 명시 제거한 '조기 포기'가 재발하므로 보수적으로 잡는다.
# network/DNS/registry/403(Tier B)은 transient 가능성이 있어 external 로 단정하지 않는다(계속 수리).
# (후속 합의 열린 항목: backend available()/failover 를 1차 신호로, Tier B 반복게이트 임계값 도입.)
_EXTERNAL_TIER_A_KINDS = {
    "auth_missing",
    "tool_missing",
    "permission_denied",
    "sandbox_unavailable",
}
# #RA3: 원문 텍스트 매칭은 '고신뢰 인증/키 부재' 신호로만 좁힌다. "command not found"(vite/pytest
# 등 고칠 수 있는 의존성·스크립트 문제), "unauthorized"/"permission denied"/"operation not
# permitted"/"read-only file system" 은 정상 코드 수리 실패에도 나타나 조기 EXTERNAL_BLOCKED 를
# 유발하므로 제거한다. 구조화된 failure_kind(_EXTERNAL_TIER_A_KINDS)는 그대로 신뢰한다.
_EXTERNAL_TIER_A_PATTERNS = (
    "missing api key",
    "no api key",
    "api key is not",
    "api key not set",
    "invalid api key",
)


def _exception_outcome(role: str, exc: Exception) -> dict:
    return {
        "status": "failed",
        "artifacts": [],
        "notes": [],
        "blockers": [f"{role} raised: {exc}"],
        "units": [],
        "_ok": False,
    }


class Scheduler:
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.board = Board(cfg.project_dir)
        self.runner = Runner(cfg, self.board)
        self.git = GitCheckpointer(cfg.project_dir, enabled=cfg.auto_commit)
        self._stop = asyncio.Event()
        self._dev_failure_signatures: dict[str, tuple[str, int]] = {}
        self._last_dev_failure: dict[str, dict] = {}
        # #H04: QA/test 검증 실패의 진행(반복) 추적 — dev 실패와 대칭. dev 는 성공하나 QA 가 계속
        # 실패하는 경우에도 '진전 없음'을 감지해 에스컬레이션·외부장애 분류를 적용한다.
        self._verify_failure_signatures: dict[str, tuple[str, int]] = {}
        # #audit15: 외부장애(Tier A) 반복 카운터 — escalation 시그니처(source 포함)와 분리한다.
        # source 가 시그니처에 들어가면 te↔qa 교대 외부장애가 count 를 매번 리셋해 자동중단
        # (>=2)이 안 터지던 회귀를 막기 위해, 외부장애 반복은 source 무관 별도 카운터로 센다.
        # #audit18(A1): verify(test/qa)용 외부장애 카운터.
        self._external_repeat: dict[str, int] = {}
        # #audit18(A1): dev 수리(_dev_repair_loop)용 외부장애 카운터를 verify 와 *분리*한다.
        # 예전엔 둘이 같은 _external_repeat 를 공유해, _test_unit 내부 dev 재작업이 남긴
        # dev-외부장애 카운트가 verify 루프로 새어(첫 verify 외부장애 1회로 >=2) 조기 BLOCK 했다.
        self._dev_external_repeat: dict[str, int] = {}
        # test/qa 동시성 캡: dev 와 별개로 test-engineer+qa 호출을 묶는 세마포어.
        # dev 슬롯(sem)만 캡되어 있고 test/qa 는 unit 마다 자유 태스크로 떠서, unit 이 많으면
        # 동시 백엔드 세션이 폭증할 수 있었다(과금/리소스). concurrency 에서 크기를 끌어와
        # test/qa 병렬도도 같은 상한으로 제한한다(0/음수 방어는 RunConfig 가 이미 함).
        self._test_sem = asyncio.Semaphore(max(1, self.cfg.concurrency))

    def _compose_feature_spec(self) -> str:
        """#feature incremental mode: compose one spec = feature request + existing-project context.

        The spec is AGENT-FACING, so it is written in English (AI-friendly). The incremental
        instruction + feature request go FIRST so prompts' spec_excerpt (the head) captures the
        essentials; the existing file tree/excerpts follow (full spec is readable by agents at
        .orchestrator/spec.md). If --spec is also given, include it as extra context.
        """
        feat = str(self.cfg.feature or "").strip()
        extra = ""
        try:
            sp = self.cfg.spec_path
            # #audit19(F1): do NOT ingest our own composed spec. Without --spec, spec_path
            # defaults to project_dir/.orchestrator/spec.md — the very file run()/scaffold writes
            # the composed feature spec into. Reading it back as "extra" made each re-run absorb
            # composed spec (unbounded growth + duplicated/stale instructions). Only read an
            # explicitly-provided, DISTINCT --spec file.
            placeholder = (self.cfg.project_dir / ".orchestrator" / "spec.md").resolve()
            if (
                sp
                and sp.is_file()
                and sp.resolve() != placeholder
                and sp.stat().st_size <= MAX_SPEC_BYTES
            ):
                extra = sp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            extra = ""
        repo = workspace.gather_repo_context(self.cfg.project_dir)
        parts = [
            "# INCREMENTAL FEATURE REQUEST (development-continuity / feature-addition mode)",
            "",
            "This task ADDS a new feature to the *existing project* below. Do NOT rebuild from",
            "scratch. Reuse the existing code, structure, stack, and dependencies; only edit",
            "(Edit) the files you must, or add new ones.",
            "",
            "## Required order of work (follow this order)",
            "1) **Blank-slate audit & code analysis FIRST**: do not rely on prior assumptions or",
            "   docs; re-read the existing project code from scratch and find bugs/defects of ALL",
            "   severities (critical to minor).",
            "2) **Fix found issues FIRST**: turn audit findings into work units and fix them",
            "   BEFORE adding the feature. (If no issues are found, state that explicitly.)",
            "3) **THEN add the feature**: implement the requested feature by reusing/editing code.",
            "4) **No regressions**: existing tests + new tests must all pass. Developers must",
            "   actually run them to confirm.",
            "",
            "The architect must plan BOTH (a) issue-fix units from the audit AND (b) feature",
            "units, specifying each unit's deps/artifacts against the existing files.",
            "",
            "## Feature to add",
            feat or "(no feature text provided)",
            "",
        ]
        if extra.strip():
            parts += ["## Additional spec / context (--spec)", extra, ""]
        parts.append(repo)
        return "\n".join(parts)

    async def run(self) -> dict:
        if self.cfg.feature:
            # #feature: 증분 모드 — spec 파일을 읽는 대신 기능 요청 + 기존 프로젝트 컨텍스트를 합성.
            spec_text = self._compose_feature_spec()
        else:
            try:
                if self.cfg.spec_path.stat().st_size > MAX_SPEC_BYTES:
                    raise ValueError(
                        f"spec too large (> {MAX_SPEC_BYTES} bytes): {self.cfg.spec_path}"
                    )
            except OSError:
                pass
            spec_text = self.cfg.spec_path.read_text(encoding="utf-8")
        workspace.scaffold(self.cfg.project_dir, spec_text, DEFAULT_STACK)
        self.board.spec_text = spec_text
        await self.board.init(spec_text, DEFAULT_STACK)
        await self._checkpoint("orchestrator: scaffold project")

        # 생존 확인용 PID 파일 (웹 서버 재시작에도 running 상태를 정확히 판단). pid 와 함께
        # 시작시각 토큰을 둘째 줄에 기록해, pid 재사용 시 무관한 프로세스를 우리 run 으로 오인해
        # stop 때 엉뚱한 프로세스에 시그널 보내는 것을 막는다(#M6). 토큰을 못 구하면 pid 한 줄만.
        pid_file = self.board.orch_dir / "run.pid"
        try:
            pid_file.write_text(procutil.format_pidfile(os.getpid()), encoding="utf-8")
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
        # test/qa 백그라운드 태스크. finally 에서 정리해야 하므로 try 밖에서 미리 바인딩한다
        # (try 안에서 예외가 gather 도달 전에 나도 finally 가 NameError 없이 정리 가능).
        test_tasks: list[asyncio.Task] = []

        try:
            # Phase A — 설계 + 테스트시트 병렬
            await self.board.set_phase("design")
            await self.board.log_event("scheduler", "Phase A: design ‖ testsheet")
            design_outcomes = await asyncio.gather(
                *[self.runner.run_role(r) for r in DESIGN_ROLES], return_exceptions=True
            )
            design_outcomes = [
                _exception_outcome(r, o) if isinstance(o, Exception) else o
                for r, o in zip(DESIGN_ROLES, design_outcomes, strict=False)
            ]
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
            design_paths = [
                p
                for o in design_outcomes
                if not isinstance(o, Exception) and o.get("_ok", True)
                for p in o.get("artifacts", [])
            ]
            await self._checkpoint("orchestrator: design work units", design_paths)

            # Phase B/C — unit별 동시 개발 + 완료 시 테스트 트리거
            unit_list = self.board.units()
            # #audit16: --max-units 로 의도적으로 건너뛴 unit 의 id 집합. 이들은 designed 로 남는데,
            # 최종 still_incomplete 판정에서 제외해야 의도적 스킵이 run 을 failed 로 만들지 않는다.
            skipped_unit_ids: set[str] = set()
            if self.cfg.max_units and self.cfg.max_units > 0:  # 음수면 슬라이싱 오작동 → 무시
                skipped_units = unit_list[self.cfg.max_units :]
                unit_list = unit_list[: self.cfg.max_units]
                if skipped_units:
                    # --max-units 로 잘린 unit 들은 designed 로 남는다. 조용히 'ok' 로 끝나지
                    # 않도록 경고로 표면화 (의도적 스킵이므로 failed 로 만들지는 않는다).
                    skipped_ids = [u["id"] for u in skipped_units]
                    skipped_unit_ids = {uid for uid in skipped_ids if uid}
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

            async def pipeline(unit: dict) -> None:
                uid = unit["id"]
                if self._stop.is_set():  # graceful shutdown: 새 unit 작업을 시작하지 않음
                    await self.board.log_event(uid, "stop requested → skip (designed 유지)")
                    await self.board.set_status(uid, BLOCKED, "stop requested before unit started")
                    await self.board.add_warning(f"{uid}: stop 요청으로 unit 미처리 → blocked")
                    return
                if not await self._wait_for_deps(unit, skipped_unit_ids=skipped_unit_ids):
                    await self.board.set_status(uid, BLOCKED, "deps unmet or failed")
                    await self.board.add_warning(f"{uid}: 의존성 미충족/실패로 blocked")
                    return
                # dev 가 끝나면(dev_done) test/qa 는 별도 태스크로 즉시 실행하고,
                # 개발 슬롯은 반납 → 개발은 곧바로 다음 unit 으로 진행한다.
                if await self._develop_unit(unit, sem, 1):
                    test_tasks.append(asyncio.create_task(self._test_unit_safe(unit, sem)))
                    return
                if self._stop.is_set():
                    return
                if self._can_repair(1):
                    test_tasks.append(
                        asyncio.create_task(self._repair_failed_dev_safe(unit, sem, 1))
                    )

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
            await self._checkpoint("orchestrator: cicd artifacts", cicd_out.get("artifacts", []))

            # Phase E — 문서화: 실행 가이드(EN/KO) + 개발 산출물(EN/KO)
            await self.board.set_phase("docs")
            await self.board.log_event("scheduler", "Phase E: docs (EN/KO)")
            docs_out = await self.runner.run_role("docs-writer")
            await self.board.add_global_artifacts(docs_out.get("artifacts", []))
            if not docs_out.get("_ok", True):
                await self.board.add_warning("docs-writer failed (산출물 문서 미완)")
            # 보드 기반 산출물 문서는 백엔드와 무관하게 항상 EN/KO 생성
            # #audit15: docs/ 가 symlink 이거나 권한 문제로 가드/쓰기가 실패해도 전체 run 을
            # failed 로 만들지 않는다. 빌드·테스트가 성공했는데 산출물 문서 단계 예외로 죽지
            # 않도록 warning 으로 degrade 한다.
            try:
                deliverables = self.board.write_deliverables()
            except Exception as e:  # noqa: BLE001
                deliverables = []
                await self.board.add_warning(f"산출물 문서(DELIVERABLES) 작성 건너뜀: {e}")
            await self.board.add_global_artifacts(deliverables)
            await self._checkpoint(
                "orchestrator: docs artifacts", [*docs_out.get("artifacts", []), *deliverables]
            )

            # 모든 작업 완료 → 감독(PM/PL)을 graceful 종료(현재 tick 끝까지 대기, 취소 X).
            # 감독이 다 멈춘 뒤에야 done — done 시점엔 어떤 에이전트도 돌고 있지 않다.
            await self.board.set_phase("finishing")
            self._stop.set()
            await asyncio.gather(*sup_tasks, return_exceptions=True)
            sup_tasks = []
            # phase 만 보는 소비자가 실패 런을 'done' 으로 오해하지 않도록, failed/blocked unit 이
            # 하나라도 있으면 최종 phase 를 'failed' 로 둔다. (성공 런은 그대로 'done')
            terminal = set(TERMINAL_OK) | {FAILED, BLOCKED}
            still_broken = any(u.get("status") in (FAILED, BLOCKED) for u in self.board.units())
            # #audit16: --max-units 로 의도적으로 건너뛴(designed 유지) unit 은 미완료로 치지 않는다
            # (스킵은 실패가 아니며 위에서 이미 경고로 표면화됨). 그래야 부분 실행이 failed 로
            # 오분류되지 않는다.
            still_incomplete = any(
                u.get("status") not in terminal and u.get("id") not in skipped_unit_ids
                for u in self.board.units()
            )
            if still_incomplete:
                await self.board.add_warning("미완료 unit 이 남아 있어 run 을 failed 로 표시합니다")
            await self.board.set_phase("failed" if (still_broken or still_incomplete) else "done")
        finally:
            self._stop.set()
            # sup_tasks 뿐 아니라 test/qa 백그라운드(test_tasks)도 반드시 정리한다. 정상
            # 경로에선 test_tasks 가 위에서 await 완료돼 cancel 이 무해하나, gather 도달 전
            # 예외/외부 cancel 경로에선 정리 안 하면 고아 태스크가 report 이후 board 를 mutate.
            pending = [*sup_tasks, *test_tasks]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
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

    async def _checkpoint(self, message: str, paths: list[str] | None = None) -> None:
        committed, detail = await self.git.checkpoint(message, paths)
        if committed:
            await self.board.log_event("git", f"checkpoint {detail}: {message}")
        elif detail not in ("disabled", "no changes"):
            await self.board.log_event("git", f"checkpoint skipped: {detail}")

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
            attempt_label = self._attempt_label(attempt)
            await self.board.set_status(uid, IN_PROGRESS, f"dev attempt {attempt_label}")
            # dev role 호출이 예외를 던져도 unit 을 blocked 로 내리고 파이프라인을 죽이지 않는다.
            dev_outcomes = await asyncio.gather(
                *[self.runner.run_role(r, unit) for r in dev_roles], return_exceptions=True
            )
            normalized_outcomes = []
            for role, o in zip(dev_roles, dev_outcomes, strict=False):
                if isinstance(o, Exception):
                    normalized_outcomes.append(_exception_outcome(role, o))
                    continue
                normalized_outcomes.append(o)
                await self.board.add_artifacts(uid, o.get("artifacts", []))
            # #RA-devok: _ok 누락 outcome 은 실패로 본다(test/qa 게이트와 동일하게 기본 False).
            failed = any(not o.get("_ok", False) for o in normalized_outcomes)
            if failed:
                await self.board.set_status(uid, BLOCKED, "dev failed")
                self._remember_dev_failure(uid, normalized_outcomes)
                if not self._can_repair(attempt):
                    self._clear_failure_state(uid)
                await self.board.add_warning(f"{uid}: 개발(dev) 실패 → blocked")
                return False
            self._dev_failure_signatures.pop(uid, None)
            self._last_dev_failure.pop(uid, None)
            await self.board.set_status(uid, DEV_DONE)
            dev_paths = [
                p
                for o in normalized_outcomes
                if o.get("_ok", False)
                for p in o.get("artifacts", [])
            ]
            await self._checkpoint(f"orchestrator: {uid} dev attempt {attempt}", dev_paths)
            return True

    async def _repair_failed_dev_safe(
        self, unit: dict, sem: asyncio.Semaphore, attempt: int
    ) -> None:
        try:
            await self._repair_failed_dev(unit, sem, attempt)
        except Exception as e:
            await self.board.set_status(unit["id"], FAILED, f"dev repair pipeline error: {e}")
            await self.board.add_warning(f"{unit['id']}: dev repair 파이프라인 예외: {e}")

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

    def _text_list(self, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
        return [str(value)]

    def _with_repair_context(
        self, unit: dict, outcome: dict, source: str, escalation: str | None = None
    ) -> dict:
        notes = self._text_list(outcome.get("notes"))
        blockers = self._text_list(outcome.get("blockers"))
        fields = []
        for key in ("failure_kind", "repair_owner", "repair_instruction", "command", "stderr_tail"):
            if outcome.get(key):
                fields.append(f"{key}: {outcome[key]}")
        body = "\n".join(
            [
                f"source: {source}",
                *fields,
                "blockers:",
                *(f"- {b}" for b in blockers[:8]),
                "notes:",
                *(f"- {n}" for n in notes[:12]),
            ]
        ).strip()
        # 반복 실패 시 강화 지시를 본문 앞에 붙여 가장 먼저 읽히게 한다(#C1).
        # #RA4: 예전엔 escalation 을 앞에 붙인 뒤 body[-_MAX:] 로 '끝'을 잘라, 방금 앞에 붙인
        # 에스컬레이션 헤더가 통째로 잘려나갔다. 헤더는 보존하고 base 본문의 '꼬리'만 자른다.
        if escalation:
            prefix = escalation.strip() + "\n\n"
            budget = _MAX_REPAIR_CONTEXT_CHARS - len(prefix)
            context = prefix + (body[-budget:] if budget > 0 else "")
        else:
            context = body[-_MAX_REPAIR_CONTEXT_CHARS:]
        # #M2: dict(unit) 얕은 복사는 deps/roles/notes 등 list 필드를 원본과 공유해, runner/백엔드가
        # repair_unit 을 변형하면 원본 unit 까지 오염됐다. deepcopy 로 완전히 분리한다.
        repaired = copy.deepcopy(unit)
        repaired["repair_context"] = context
        return repaired

    def _needs_test_repair(self, outcome: dict) -> bool:
        kind = str(outcome.get("failure_kind") or "").strip().lower().replace("-", "_")
        owner = str(outcome.get("repair_owner") or "").strip().lower()
        if kind in _TEST_REPAIR_KINDS or owner == "test-engineer":
            return True
        text = " ".join(
            str(x)
            for x in [
                outcome.get("repair_instruction", ""),
                *self._text_list(outcome.get("blockers")),
                *self._text_list(outcome.get("notes")),
            ]
        ).lower()
        return any(word in text for word in _TEST_REPAIR_WORDS)

    def _remember_dev_failure(self, uid: str, outcomes: list[dict]) -> None:
        # #RA-devok: _ok 누락 outcome 은 실패로 본다(_develop_unit 의 판정과 일치).
        failed = [o for o in outcomes if not o.get("_ok", False)]
        notes: list[str] = []
        blockers: list[str] = []
        fields: dict[str, object] = {}
        for o in failed:
            notes.extend(self._text_list(o.get("notes")))
            blockers.extend(self._text_list(o.get("blockers")))
            for key in (
                "failure_kind",
                "repair_owner",
                "repair_instruction",
                "command",
                "stderr_tail",
            ):
                if key not in fields and o.get(key):
                    fields[key] = o[key]
        signature = self._failure_signature(fields, failed)
        prev_sig, prev_count = self._dev_failure_signatures.get(uid, ("", 0))
        count = prev_count + 1 if prev_sig == signature else 1
        self._dev_failure_signatures[uid] = (signature, count)
        self._last_dev_failure[uid] = {
            "status": "failed",
            "artifacts": [],
            "notes": notes,
            "blockers": blockers,
            "_ok": False,
            "repeat_count": count,
            **fields,
        }

    @staticmethod
    def _failure_signature(fields: dict, failed: list[dict]) -> str:
        # #M01/#C1: '진전 없음(동일 실패 반복)' 판정 시그니처는 LLM 이 매번 바꾸는 자유 텍스트
        # (repair_instruction·notes·blockers)와 변동성 큰 stderr_tail(타임스탬프·줄번호·임시경로)을
        # 제외하고, 실제 장애를 식별하는 안정 필드(failure_kind·repair_owner·command)만 쓴다.
        # 그래야 반복 카운트가 헛리셋되지 않아 에스컬레이션이 정상 발화한다.
        # #audit14: 검증 단계(source: test-engineer vs qa)도 안정 필드로 포함한다. 서로 다른
        # 검증 주체의 실패가 같은 failure_kind/command 를 우연히 공유해도 하나의 '반복 실패'로
        # 과합산되지 않게 한다(Codex 교차검증). dev 경로는 fields 에 source 가 없어 영향 없음.
        stable = {
            k: fields[k]
            for k in ("failure_kind", "repair_owner", "command", "source")
            if k in fields
        }
        return json.dumps(
            {"fields": stable, "statuses": [str(o.get("status", "")) for o in failed]},
            ensure_ascii=False,
            sort_keys=True,
        )

    def _remember_verify_failure(self, uid: str, outcome: dict, source: str = "") -> int:
        """QA/test 검증 실패의 진행 시그니처를 기록하고 반복 횟수를 반환한다(#H04).

        dev 실패(_remember_dev_failure)와 대칭. dev 는 성공하나 QA 가 계속 실패하는 경우에도
        '진전 없음'을 추적해 에스컬레이션·외부장애 분류에 쓴다.

        source(test-engineer/qa)를 시그니처에 포함해(#audit14) 서로 다른 검증 주체의 실패를
        같은 반복으로 합치지 않는다(같은 source 가 같은 방식으로 반복될 때만 count 증가).
        """
        fields = {
            k: outcome[k] for k in ("failure_kind", "repair_owner", "command") if outcome.get(k)
        }
        if source:
            fields["source"] = source
        signature = self._failure_signature(fields, [outcome])
        prev_sig, prev_count = self._verify_failure_signatures.get(uid, ("", 0))
        count = prev_count + 1 if prev_sig == signature else 1
        self._verify_failure_signatures[uid] = (signature, count)
        return count

    def _dev_failure_repeat_count(self, uid: str) -> int:
        """동일 dev 실패가 연속으로 몇 번 반복됐는지(진전 없음 횟수)."""
        return self._dev_failure_signatures.get(uid, ("", 0))[1]

    def _clear_failure_state(self, uid: str) -> None:
        """unit 이 terminal(DONE/FAILED/BLOCKED)에 도달하면 누적 실패 시그니처를 정리한다(#RA5).

        dev/verify 실패 추적 dict 들이 종료된 unit 의 키를 계속 들고 있으면 메모리 누수이고,
        같은 uid 가 재진입할 경우 이전 카운트가 남아 에스컬레이션 게이트를 오염시킨다.
        """
        self._verify_failure_signatures.pop(uid, None)
        self._dev_failure_signatures.pop(uid, None)
        self._last_dev_failure.pop(uid, None)
        self._external_repeat.pop(uid, None)  # #audit15: verify 외부장애 카운터
        self._dev_external_repeat.pop(uid, None)  # #audit18(A1): dev 외부장애 카운터도 함께 정리

    @staticmethod
    def _escalation_text(count: int, label: str) -> str | None:
        """동일 실패가 반복되면(진전 없음) 포기하지 않고 수리 전략을 점진적으로 강화한다(#C1/#H04).

        제품 완주 모드(max_attempts=0)는 완료까지 무한히 수리하되, '같은 시도를 그대로 반복하는'
        토큰 낭비를 막는다. 반복될수록 더 강한 지시를 주입해 매 시도가 실질적으로 달라지게 한다.
        """
        if count < 2:
            return None
        note = (
            f"\n[수리 에스컬레이션] 동일한 {label} 실패가 {count}회 반복되었습니다. "
            "표면적 수정이 듣지 않으니 '직전과 같은 시도를 반복하지 말고' 접근을 바꾸세요:\n"
            "1) 에러/스택 트레이스를 처음부터 다시 읽고 근본 원인을 특정하세요.\n"
            "2) 관련 모듈·인터페이스·계약·설정 파일을 전체적으로 다시 확인하세요.\n"
            "3) 빌드/의존성/환경/버전 설정을 점검하세요.\n"
            "4) 이전과 '다른' 구현 전략을 시도하세요."
        )
        if count >= 4:
            note += (
                "\n5) 현재 접근이 막혀 있습니다 — 가정을 의심하고, 필요하면 라이브러리·패턴·"
                "구현 방식을 교체하거나 더 단순하고 견고한 대안 구현으로 전환하세요."
            )
        return note

    def _escalation_note(self, uid: str) -> str | None:
        """동일 dev 실패 반복 시 수리 전략 강화 노트(#C1)."""
        return self._escalation_text(self._dev_failure_repeat_count(uid), "dev")

    def _external_blocker_reason(self, outcomes: list[dict]) -> str | None:
        """코드 수리로 풀 수 없는 고신뢰 외부/환경 장애(Tier A)면 사유 문자열, 아니면 None (#H09).

        보수적으로 — auth/key·tool/backend 부재·권한 거부 같은 고신뢰 영구 신호만 본다. 구조화된
        failure_kind 를 우선 보고, 보조로 제한된 텍스트 패턴을 본다. network/DNS/registry/403(Tier
        B)은 transient 가능성이 있어 여기서 external 로 단정하지 않는다(계속 수리).
        """
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            kind = str(o.get("failure_kind") or "").strip().lower().replace("-", "_")
            if kind in _EXTERNAL_TIER_A_KINDS:
                return kind
            text = " ".join(
                str(x)
                for x in [
                    o.get("repair_instruction", ""),
                    o.get("command", ""),
                    o.get("stderr_tail", ""),
                    *self._text_list(o.get("blockers")),
                    *self._text_list(o.get("notes")),
                ]
            ).lower()
            for pat in _EXTERNAL_TIER_A_PATTERNS:
                if pat in text:
                    return pat
        return None

    def _attempt_label(self, attempt: int) -> str:
        if self.cfg.max_attempts == 0:
            return f"{attempt}/∞"
        return f"{attempt}/{self.cfg.max_attempts}"

    def _can_repair(self, attempt: int) -> bool:
        return self.cfg.max_attempts == 0 or attempt < self.cfg.max_attempts

    def _budget_exhausted(self) -> bool:
        if self.cfg.budget is None:
            return False
        try:
            spent = float(self.board.snapshot().get("total_cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            spent = 0.0
        return spent >= self.cfg.budget

    async def _repair_failed_dev(self, unit: dict, sem: asyncio.Semaphore, attempt: int) -> None:
        """초기 dev 실패(pipeline 진입) 후 dev 재작업 루프 → 성공 시 검증(_test_unit).

        재귀 없이 _dev_repair_loop(while)로 dev 재작업을 반복하고, 성공한 repair_unit 으로 검증한다.
        """
        uid = unit["id"]
        if self._budget_exhausted():
            await self.board.set_status(uid, BLOCKED, "repair stopped: budget exhausted")
            await self.board.add_warning(f"{uid}: 예산 소진으로 자동 수리 중단")
            return
        outcome = self._last_dev_failure.get(
            uid, {"_ok": False, "status": "failed", "blockers": ["dev failed"], "notes": []}
        )
        repair_unit = self._with_repair_context(
            unit, outcome, "dev", escalation=self._escalation_note(uid)
        )
        developed = await self._dev_repair_loop(unit, sem, attempt + 1, repair_unit)
        if developed is not None:
            await self._test_unit(developed, sem, attempt + 1)

    async def _dev_repair_loop(
        self, unit: dict, sem: asyncio.Semaphore, next_attempt: int, repair_unit: dict
    ) -> dict | None:
        """dev 재작업을 while 로 반복한다(#H03: 재귀 제거).

        dev 성공 시 그 repair_unit 을 반환(호출자가 _test_unit 으로 재검증). 종료 조건(예산 소진·
        deps 붕괴·고신뢰 외부 장애·stop·유한 attempts 소진) 시 terminal 상태를 세팅하고 None.
        동일 dev 실패 반복 시 포기 대신 에스컬레이션(#C1); 고신뢰 외부 장애 반복은 분류·중단(#H09).
        """
        uid = unit["id"]
        while self._can_repair(next_attempt - 1) and not self._stop.is_set():
            if self._budget_exhausted():
                self._clear_failure_state(uid)  # #RA5: terminal(BLOCKED) → 시그니처 정리
                await self.board.set_status(uid, BLOCKED, "repair stopped: budget exhausted")
                await self.board.add_warning(f"{uid}: 예산 소진으로 자동 수리 중단")
                return None
            broken = self._broken_deps(unit)
            if broken:
                self._clear_failure_state(uid)  # #RA5: terminal(BLOCKED) → 시그니처 정리
                await self.board.set_status(uid, BLOCKED, f"rework aborted: deps broken {broken}")
                await self.board.add_warning(f"{uid}: 재작업 직전 의존성 붕괴 → blocked: {broken}")
                return None
            # #H09/#audit16: 고신뢰 외부/영구 장애가 반복되면(코드 수리 불가) external 로 분류·중단.
            # #audit18(A1): verify 와 분리된 dev 전용 카운터(_dev_external_repeat)로 센다. 그래야
            # dev 재작업의 외부장애가 verify 카운터로 새지 않는다(조기 BLOCK 방지). failure_kind/
            # command 진동에도 source 무관 카운터라 자동중단(>=2)은 정상 발화한다.
            ext = self._external_blocker_reason([self._last_dev_failure.get(uid, {})])
            if ext:
                ext_repeat = self._dev_external_repeat.get(uid, 0) + 1
                self._dev_external_repeat[uid] = ext_repeat
            else:
                self._dev_external_repeat.pop(uid, None)
                ext_repeat = 0
            if ext and ext_repeat >= 2:
                self._clear_failure_state(uid)  # #RA5: terminal(BLOCKED) → 시그니처 정리
                await self.board.set_status(uid, BLOCKED, f"external blocker: {ext}")
                await self.board.add_warning(
                    f"{uid}: 외부/환경 장애로 자동 수리 중단 (external: {ext})"
                )
                return None
            await self.board.log_event(uid, f"dev 재작업 → attempt {next_attempt}")
            if await self._develop_unit(repair_unit, sem, next_attempt):
                # #audit18(A1): dev 성공 = 진전 → dev 외부장애 카운터를 비워 다음 dev 수리(또는
                # verify 루프 복귀)로 누수되지 않게 한다. verify 카운터(_external_repeat)는 별도라
                # 영향 없음(te↔qa 누적·중단은 audit15 그대로 유지).
                self._dev_external_repeat.pop(uid, None)
                return repair_unit
            escalation = self._escalation_note(uid)
            if escalation:
                await self.board.log_event(
                    uid,
                    f"동일 dev 실패 {self._dev_failure_repeat_count(uid)}회 반복 → "
                    "수리 전략 에스컬레이션 후 계속",
                )
            outcome = self._last_dev_failure.get(
                uid, {"_ok": False, "status": "failed", "blockers": ["dev failed"], "notes": []}
            )
            repair_unit = self._with_repair_context(unit, outcome, "dev", escalation=escalation)
            next_attempt += 1
        if self._stop.is_set():
            self._clear_failure_state(uid)
            await self.board.set_status(uid, BLOCKED, "stop requested during dev repair")
            return None
        self._clear_failure_state(uid)  # #RA5: terminal(FAILED) → 시그니처 정리
        await self.board.set_status(uid, FAILED, "dev repair failed after retries")
        return None

    async def _test_unit(self, unit: dict, sem: asyncio.Semaphore, attempt: int) -> None:
        """dev 완료 후 test-engineer → qa 검증. 실패 시 재작업→재검증을 단일 while 루프로 반복한다.

        #H03: 예전엔 _test_unit↔_rework_after_verification_failure↔_qa_after_test_repair 가 상호
        재귀라, dev 는 성공하나 QA 가 계속 실패하면 max_attempts=0 에서 스택이 무한히 깊어져
        RecursionError 로 죽었다. 이를 평탄화한다(스택 증가 없음).
        #H04: QA/test 반복 실패에도 진행 시그니처·에스컬레이션을 적용(동일 시도 반복 낭비 방지).
        #H09: 고신뢰 외부/영구 장애 반복은 분류·중단.
        제품 완주 모드(max_attempts=0)는 통과/예산/deps/stop/외부장애 전까지 계속한다.

        test/qa 호출은 _test_sem 으로 캡한다(dev 슬롯과 별개). dev 재작업은 반드시 _test_sem 블록
        밖에서 수행해 두 세마포어를 중첩 점유하지 않는다.
        """
        uid = unit["id"]
        # #audit17(R1)/#audit18(A1): dev→test 경계에서 외부장애 카운터를 리셋한다. verify 카운터
        # (_external_repeat)는 진입 시 0 에서 시작하고, dev 카운터(_dev_external_repeat)도 비워
        # 직전 dev 단계의 잔여가 남지 않게 한다. audit18 에서 두 카운터를 분리했으므로 검증 루프
        # 내부의 dev 재작업(_dev_repair_loop)은 verify 카운터를 건드리지 않는다 → te↔qa 교대
        # 외부장애 누적·중단(audit15)은 유지하면서, dev→verify 누수로 인한 조기 BLOCK 은 사라진다.
        self._external_repeat.pop(uid, None)
        self._dev_external_repeat.pop(uid, None)
        target = unit  # 검증 대상 (실패 시 repair_context 가 붙은 unit 으로 갱신)
        skip_te_once = False  # test/config 재작업 직후엔 te 를 생략하고 바로 qa
        while True:
            if self._stop.is_set():  # #H5: stop 중 새 test/qa 세션 시작 금지
                await self.board.log_event(uid, "stop requested → skip test/qa")
                return
            await self.board.set_status(uid, TESTING)
            outcome: dict | None = None
            source = ""
            test_paths: list[str] = []
            async with self._test_sem:
                if not skip_te_once:
                    te = await self.runner.run_role("test-engineer", target)
                    test_paths = list(te.get("artifacts", []))
                    await self.board.add_artifacts(uid, test_paths)
                    # #18: test-engineer 실패 시 qa 비용을 쓰지 않는다. #H3: _ok 기본 False.
                    if not (te.get("_ok", False) and te.get("status") != "failed"):
                        await self.board.log_event(uid, "test-engineer 실패 → qa 건너뜀(비용 절감)")
                        await self.board.set_test_status(uid, "fail")
                        outcome, source = te, "test-engineer"
                skip_te_once = False
                if outcome is None:
                    qa = await self.runner.run_role("qa", target)
                    qa_paths = list(qa.get("artifacts", []))
                    await self.board.add_artifacts(uid, qa_paths)
                    passed = qa.get("_ok", False) and qa.get("status") != "failed"  # #H3
                    await self.board.set_test_status(uid, "pass" if passed else "fail")
                    if passed:
                        self._clear_failure_state(uid)  # #RA5: terminal(DONE) → 시그니처 정리
                        await self.board.set_status(uid, DONE)
                        await self._checkpoint(
                            f"orchestrator: {uid} verified", [*test_paths, *qa_paths]
                        )
                        return
                    outcome, source = qa, "qa"
            # ---- 검증 실패 (_test_sem 해제됨) ----
            if not self._can_repair(attempt):
                self._clear_failure_state(uid)  # #RA5: terminal(FAILED) → 시그니처 정리
                await self.board.set_status(uid, FAILED, f"{source} failed after retries")
                return
            count = self._remember_verify_failure(uid, outcome, source)  # #H04 escalation 추적
            # #H09/#audit15: 고신뢰 외부/영구 장애가 반복되면 분류·중단. escalation 카운터(source
            # 포함)와 분리된 별도 카운터로 외부장애 반복을 센다 → te↔qa 교대에도 정상 누적.
            ext = self._external_blocker_reason([outcome])
            if ext:
                ext_repeat = self._external_repeat.get(uid, 0) + 1
                self._external_repeat[uid] = ext_repeat
            else:
                self._external_repeat.pop(uid, None)
                ext_repeat = 0
            if ext and ext_repeat >= 2:
                self._clear_failure_state(uid)  # #RA5: terminal(BLOCKED) → 시그니처 정리
                await self.board.set_status(uid, BLOCKED, f"external blocker: {ext}")
                await self.board.add_warning(
                    f"{uid}: 외부/환경 장애로 자동 수리 중단 (external: {ext})"
                )
                return
            if self._stop.is_set():  # #H5
                await self.board.log_event(uid, "stop requested → skip rework")
                return
            if self._budget_exhausted():
                self._clear_failure_state(uid)  # #RA5: terminal(BLOCKED) → 시그니처 정리
                await self.board.set_status(uid, BLOCKED, "repair stopped: budget exhausted")
                await self.board.add_warning(f"{uid}: 예산 소진으로 자동 수리 중단")
                return
            broken = self._broken_deps(unit)
            if broken:
                self._clear_failure_state(uid)  # #RA5: terminal(BLOCKED) → 시그니처 정리
                await self.board.set_status(uid, BLOCKED, f"rework aborted: deps broken {broken}")
                await self.board.add_warning(f"{uid}: 재작업 직전 의존성 붕괴 → blocked: {broken}")
                return
            # ---- 재작업 (sem 밖) ----
            attempt += 1
            escalation = self._escalation_text(count, "검증(QA/test)")  # #H04
            target = self._with_repair_context(unit, outcome, source, escalation=escalation)
            if escalation:
                await self.board.log_event(
                    uid, f"동일 검증 실패 {count}회 반복 → 에스컬레이션 후 계속"
                )
            test_repair_first = self._needs_test_repair(outcome)
            await self.board.log_event(
                uid,
                f"재검증 실패 → {'test/config' if test_repair_first else 'dev'} 재작업 "
                f"(attempt {attempt})",
            )
            if test_repair_first:
                await self.board.set_status(
                    uid, TESTING, f"test/config repair attempt {self._attempt_label(attempt)}"
                )
                # #audit19(P1): test/config 재작업의 test-engineer 호출도 _test_sem 으로 캡한다.
                # 예전엔 sem 밖이라 검증 반복(production max_attempts=0) 중 동시 te 세션이 cap 을
                # 우회해 폭증할 수 있었다. (dev 재작업은 아래에서 sem 밖 — 두 세마포어 중첩 금지.)
                async with self._test_sem:
                    te = await self.runner.run_role("test-engineer", target)
                await self.board.add_artifacts(uid, list(te.get("artifacts", [])))
                if te.get("_ok", False) and te.get("status") != "failed":
                    skip_te_once = True  # 다음 루프: te 생략하고 바로 qa
                    continue
                await self.board.log_event(uid, "test/config repair failed → dev 재작업 fallback")
            # dev 재작업: 먼저 QA 컨텍스트(target)로 1회, 실패하면 dev 재작업 루프(dev 컨텍스트).
            if await self._develop_unit(target, sem, attempt):
                continue  # 재검증
            dev_outcome = self._last_dev_failure.get(uid, outcome)
            dev_repair_unit = self._with_repair_context(
                unit, dev_outcome, "dev", escalation=self._escalation_note(uid)
            )
            developed = await self._dev_repair_loop(unit, sem, attempt + 1, dev_repair_unit)
            if developed is None:
                return  # terminal 상태 세팅됨
            target = developed  # 재검증 (continue)

    async def _wait_for_deps(
        self, unit: dict, timeout: float | None = None, skipped_unit_ids: set[str] | None = None
    ) -> bool:
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
        # #audit17(N2): dep 가 --max-units 로 스킵되어 영구 DESIGNED 로 남는 경우, 완료(성공)도
        # 실패(FAILED/BLOCKED)도 아니라 stall 윈도까지 헛대기하다 blocked 됐다. 스킵된 dep 는
        # 만족될 수 없으므로 즉시 blocked 로 표면화한다(성공으로 치지 않음).
        skipped_dep = [d for d in deps if skipped_unit_ids and d in skipped_unit_ids]
        if skipped_dep:
            await self.board.add_warning(
                f"{uid}: 의존성이 --max-units 로 스킵됨 → blocked: {skipped_dep}"
            )
            await self.board.log_event(uid, f"deps skipped by --max-units → blocked: {skipped_dep}")
            return False
        # 단순 시간초과가 아니라 '진행이 멈췄을 때'만 포기한다(stall). dep 가 아직 작업 중이면
        # (상태가 바뀌거나 에이전트가 활동 중이면) 계속 기다린다 — 한 role 호출이 상태를 붙잡는
        # 최대 시간(session_timeout)보다 넉넉한 윈도. timeout 인자를 주면 그 값을 stall 로 쓴다.
        if timeout is not None:
            stall = timeout
        elif self.cfg.session_timeout is None:
            # 역할 호출 자체는 무제한이어도 dependency wait 는 영구 대기하면 안 된다. 특히
            # --max-units/stop 등으로 dep 가 DESIGNED 상태에 머물면 후속 unit 이 사람 개입 전까지
            # 멈춘다. "무제한 세션"에는 보수적인 stall 상한을 둔다.
            stall = 3600.0
        else:
            stall = max(1800.0, self.cfg.session_timeout * 2)
        prev_sig = None
        idle = 0.0
        last_check = time.monotonic()
        while True:
            if self._stop.is_set():  # #M02: graceful shutdown 중엔 dep 대기를 즉시 중단(응답성).
                await self.board.log_event(uid, "stop requested → dep wait aborted")
                return False
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
        scoped = [
            a
            for a in agents.values()
            if a.get("status", "running") == "running" and a.get("current_unit") in pending_set
        ]
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
