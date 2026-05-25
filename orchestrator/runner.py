"""결과파일 계약 실행기: 역할 프롬프트 합성 → 백엔드 호출 → 결과 파싱.

보드 전이 자체는 스케줄러가 담당한다. 여기서는 한 역할 세션을 돌리고
표준화된 outcome dict 를 돌려준다.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re

from .agents import load_agent
from .backends import get_backend
from .backends.base import RoleRequest, RoleResult
from .board import Board, _safe_unit_id
from .config import (
    DELEGATES,
    DELEGATION_CAPABLE,
    DELEGATION_TOOL,
    MAX_TURNS,
    PHASE_SUPERVISOR,
    ROLES,
    RunConfig,
)
from .prompts import compose_prompt

# #39: 동시 호출이 시작 시점에 모두 "예산 충분" 으로 보고 N-way 로 초과하는 것을 막기 위한
# in-flight 예약 추정치(USD). 실제 비용은 호출 후에야 알 수 있으므로 보수적인 한 호출 추정으로
# 자리를 미리 점유한다. 정확한 값이 아니어도 시작 시점 동시 통과를 막는 것이 목적이다.
_INFLIGHT_RESERVE_USD = 0.50

# #22: 역할 세션이 쓰는 결과 JSON 의 최대 크기. 폭주/악성 에이전트가 거대한 결과 파일을 쓰면
# orchestrator 가 read_text() 로 통째 메모리에 올리다 죽을 수 있다. 5MB 초과는 읽지 않고 계약
# 위반(실패)으로 처리한다 — 정상 결과(아티팩트/노트/units 목록)는 이 한참 아래다.
_MAX_RESULT_BYTES = 5 * 1024 * 1024


def _failure_outcome(status: str, blocker: str) -> dict:
    return {
        "status": status,
        "artifacts": [],
        "notes": [],
        "blockers": [blocker],
        "units": [],
        "_ok": False,
    }


def _truthy_env(name: str, default: bool) -> bool:
    """ORCH_* 불리언 환경변수 파싱. 미설정이면 default. 0/false/no/off → False, 그 외 → True."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _log_prompt_bodies() -> bool:
    """전체 프롬프트 본문을 per-agent 로그에 남길지 여부 (#3).

    기본값은 True(디버깅 편의 유지·하위호환). ORCH_LOG_PROMPTS=0(또는 false/no/off)로 끄면
    spec/directives 등 민감 내용이 .orchestrator/agents 로그에 남지 않도록 짧은 메모만 기록한다.
    """
    return _truthy_env("ORCH_LOG_PROMPTS", True)


class Runner:
    def __init__(self, cfg: RunConfig, board: Board):
        self.cfg = cfg
        self.board = board
        # #39: 예산 사전점검+예약을 직렬화하는 락. 동시에 시작한 여러 역할이 모두 예산을
        # "충분" 으로 보지 못하게 한다(check+reserve 를 원자적으로 처리).
        self._budget_lock = asyncio.Lock()
        # 현재 진행 중인(아직 add_cost 로 커밋되지 않은) 호출들의 예약 추정 합계.
        self._inflight_reserved = 0.0

    def _candidates(self, role: str) -> tuple[list[str], list[str]]:
        """(가용 후보, skip된 후보) 반환. 가용이 없으면 첫 후보로 명확히 실패시킨다."""
        cands = self.cfg.backends_for(role) or [self.cfg.default_backend or "mock"]

        def _ok(name: str) -> bool:
            try:
                return bool(get_backend(name).available()[0])
            except Exception:
                return False

        avail = [c for c in cands if _ok(c)]
        skipped = [c for c in cands if c not in avail]
        return (avail or cands[:1]), skipped

    def _build_teammates(self, role: str) -> list[dict]:
        out = []
        for mate in DELEGATES.get(role, ()):
            td = load_agent(mate)
            out.append(
                {
                    "name": mate,
                    "description": td.description,
                    "prompt": td.system_prompt,
                    "tools": td.tools or list(ROLES[mate].tools),
                    "model": td.model,
                }
            )
        return out

    def _build_req(self, role, spec, unit, agent, prompt, result_path, result_rel, backend_name):
        allowed_tools = agent.tools or list(spec.tools)
        delegate = self.cfg.delegate and (
            backend_name in DELEGATION_CAPABLE or backend_name == "codex"
        )
        teammates = self._build_teammates(role) if delegate else []
        if delegate and backend_name in DELEGATION_CAPABLE and DELEGATION_TOOL not in allowed_tools:
            allowed_tools = [*allowed_tools, DELEGATION_TOOL]
        # cfg 모델 미지정 시 frontmatter 의 per-agent model 을 fallback 으로 사용 (#93/#94).
        # agent.model 은 load_agent 에서 이미 'inherit'→None 정규화됨.
        model = self.cfg.model_for(backend_name) or agent.model
        return RoleRequest(
            role=role,
            phase=spec.phase,
            unit=unit,
            system_prompt=agent.system_prompt,
            prompt=prompt,
            cwd=self.board.project_dir,
            allowed_tools=allowed_tools,
            model=model,
            max_turns=MAX_TURNS.get(spec.phase, 20),
            budget=self.cfg.budget,
            result_path=result_path,
            result_rel=result_rel,
            spec_text=self.board.spec_text,
            timeout=self.cfg.session_timeout,
            # #L10: board._log_path(role) 를 써서 라이브 스트림 경로와 보드의 agent-log
            # 경로가 항상 일치하게 한다(raw role 직접 조합은 _safe_unit_id 살균과 어긋남).
            live_log_path=self.board._log_path(role),
            delegate=delegate,
            full_access=self.cfg.full_access,
            teammates=teammates,
        )

    async def run_role(self, role: str, unit: dict | None = None) -> dict:
        key = "global"
        reserved = 0.0

        try:
            spec = ROLES[role]
            agent = load_agent(role)
            # #9: raw unit id 를 result 파일 경로에 그대로 끼우면 '/'·'..' 등이 결과 파일을
            # results 디렉터리 밖으로 빼낼 수 있다. 보드와 동일한 _safe_unit_id 로 방어적으로
            # 살균(경로 구분자/특수문자 → '-')한다. 살균 후 빈 값이면 "unknown" 으로 폴백.
            if isinstance(unit, dict):
                key = _safe_unit_id(unit.get("id", "unknown")) or "unknown"
            else:
                key = "global"
            result_rel = f".orchestrator/results/{role}__{key}.json"
            result_path = self.board.project_dir / result_rel

            # 누적 예산 enforcement: 백엔드 무관하게 runner 에서 강제 (#112). 보드 누적 비용이 이미
            # 예산 이상이면 백엔드를 호출하지 않고 blocked 반환 → --budget 가 모든 백엔드에 의미.
            #
            # #39: concurrency>1 일 때 여러 역할이 add_cost 전에 모두 사전점검을 통과해 N-way 로
            # 초과하던 문제를 줄인다. check+reserve 를 _budget_lock 으로 직렬화하고, "커밋된 보드
            # 누적 + 진행 중 예약 추정" 을 함께 보아 동시 시작이 모두 예산을 충분으로 보지
            # 못하게 한다.
            # 잔여 한계: 이미 시작된 한 호출은 실제 비용을 끝나봐야 알 수 있어 한 호출 비용만큼은
            # 여전히 초과할 수 있다(완벽 방지는 불가능). reserved 는 호출 종료 시 finally 에서
            # 해제한다.
            if self.cfg.budget is not None:
                async with self._budget_lock:
                    committed = self.board.snapshot().get("total_cost_usd", 0.0) or 0.0
                    projected = committed + self._inflight_reserved
                    if projected >= self.cfg.budget:
                        blocker = (
                            f"budget exceeded: spent ${committed:.4f} "
                            f"(+${self._inflight_reserved:.4f} in-flight) >= "
                            f"budget ${self.cfg.budget:.4f}"
                        )
                        await self.board.log_event(role, f"skip [budget] {blocker}")
                        await self.board.agent_update(
                            role,
                            status="idle",
                            activity=f"⏸ skipped (budget) {key if unit else ''}",
                        )
                        return _failure_outcome("blocked", blocker)
                    # 통과 → 한 호출분 추정치를 예약(동시 시작자가 같은 잔액을 중복 사용 못 하게).
                    reserved = _INFLIGHT_RESERVE_USD
                    self._inflight_reserved += reserved

            return await self._run_role_inner(role, spec, agent, unit, key, result_path, result_rel)
        except Exception as e:
            return _failure_outcome(
                "failed", f"runner setup/preflight failed for {role}:{key}: {e}"
            )
        finally:
            if reserved:
                async with self._budget_lock:
                    # 음수 방지: 부동소수 오차 등으로 0 미만이 되지 않게 클램프.
                    self._inflight_reserved = max(0.0, self._inflight_reserved - reserved)

    async def _run_role_inner(self, role, spec, agent, unit, key, result_path, result_rel) -> dict:
        prompt = compose_prompt(
            role=role,
            phase=spec.phase,
            unit=unit,
            directives=self.board.directives(),
            result_rel=result_rel,
            spec_excerpt=self.board.snapshot().get("spec_excerpt", ""),
            recent_events=self.board.recent_events(12) if unit is None else "",
            completion_level=self.cfg.completion_level,
        )

        candidates, skipped = self._candidates(role)
        if skipped:  # 왜 특정 provider 가 빠졌는지 run artifact 에서 추적 가능하게
            await self.board.log_event(role, f"backend skipped (unavailable): {skipped}")
        res: RoleResult | None = None
        chosen = candidates[0]
        role_cost = 0.0
        # 바깥 try 는 마지막 안전망(safety net)일 뿐이다. 후보 처리는 후보별 안쪽 try/except 로
        # 감싸므로, 한 후보를 처리하다 발생한 예기치 못한 예외(백엔드 호출이 아니라
        # write_agent_block/add_cost/agent_update/결과 로깅 등에서 난 것)는 그 후보만 실패로 보고
        # 다음 후보로 폴오버한다. (백엔드 호출 자체의 예외는 이미 _run_with_retries 안에서
        # RoleResult 로 흡수된다.) 예전에는 전체 for 가 하나의 try 안에 있어, 백엔드가 아닌
        # 처리 단계에서 한 번 예외가 나면 남은 폴오버 후보를 건너뛰고 role 전체가 죽었다.
        try:
            for i, name in enumerate(candidates):
                chosen = name
                try:
                    req = self._build_req(
                        role, spec, unit, agent, prompt, result_path, result_rel, name
                    )
                    team = f" +team={[m['name'] for m in req.teammates]}" if req.teammates else ""
                    fo = f" (failover {i + 1}/{len(candidates)})" if i else ""
                    await self.board.log_event(
                        role, f"start [{name}]" + (f" unit={key}" if unit else "") + team + fo
                    )
                    await self.board.agent_update(
                        role,
                        status="running",
                        unit=key,
                        backend=name,
                        call=True,
                        activity="▶ " + (f"unit={key} " if unit else "") + f"[{name}]{team}{fo}",
                    )
                    if result_path.exists() and _under_results_dir(result_path, self.board):
                        # missing_ok: .exists() 와 unlink 사이의 TOCTOU(다른 곳에서 먼저 삭제)
                        # 로 FileNotFoundError 가 나지 않게 한다 — 후보마다 직전 결과 제거(#25)
                        result_path.unlink(missing_ok=True)

                    # 상세 로그: 보낸 프롬프트 (시스템 + 작업).
                    # #3: 전체 프롬프트 본문에는 spec excerpt·PM/PL directives 등 민감 내용이 들어가
                    # .orchestrator/agents 로그에 영구 저장될 수 있다. ORCH_LOG_PROMPTS=0
                    # 으로 끄면 본문 대신 짧은 메모만 남긴다. 기본값은 디버깅 편의로 전체 기록.
                    title = f"PROMPT → [{name}]" + (f" unit={key}" if unit else "")
                    if _log_prompt_bodies():
                        body = "[SYSTEM]\n" + agent.system_prompt + "\n\n[TASK]\n" + prompt
                    else:
                        body = (
                            "[prompt body suppressed: ORCH_LOG_PROMPTS=0] "
                            f"system={len(agent.system_prompt)} chars, task={len(prompt)} chars"
                        )
                    self.board.write_agent_block(role, title, body)
                    # 후보가 예외를 던져도 다음 후보로 폴오버 (전체 role 을 죽이지 않는다)
                    res = await self._run_with_retries(get_backend(name), req, role, key)
                    # 상세 로그: 받은 결과. board.write_agent_block 가 본문을 ~20000자로 절단하므로
                    # spec/directives/대용량 출력이 로그를 무한히 키우지 않음 (#31/#32). 단, spec 내
                    # 민감정보는 여전히 .orchestrator/agents 로그에 (절단된 형태로) 남을 수 있음.
                    self.board.write_agent_block(
                        role,
                        f"RESULT ← [{name}] ok={res.ok}",
                        res.final_message or res.error or "(no output)",
                    )
                    if res.cost_usd:
                        role_cost += res.cost_usd
                        await self.board.add_cost(res.cost_usd)
                        await self.board.agent_update(
                            role, cost_add=res.cost_usd, cost_est=res.cost_estimated
                        )
                    if res.tokens:
                        await self.board.agent_update(role, tokens_add=res.tokens)
                    if res.warning:  # 백엔드 경고(예: SDK 예산 캡 미적용)를 보드/리포트에 표면화
                        await self.board.add_warning(f"{role} [{name}]: {res.warning}")
                    if res.ok:
                        break
                    if i < len(candidates) - 1:
                        nxt = candidates[i + 1]
                        await self.board.log_event(role, f"failover [{name}]→[{nxt}]: {res.error}")
                        await self.board.agent_update(role, activity=f"↪ failover [{name}]→[{nxt}]")
                except Exception as e:
                    # 이 후보를 처리하는 도중(백엔드 호출이 아닌 단계 — 로깅/비용/agent_update 등)에
                    # 예기치 못한 예외가 났다. 그 후보만 실패로 보고 다음 후보로 폴오버한다
                    # (남은 후보를 건너뛰고 role 전체를 죽이지 않는다).
                    res = RoleResult(ok=False, error=f"runner error: {e}")
                    try:
                        await self.board.log_event(role, f"error [{name}]: {e}")
                    except Exception:
                        pass  # 로깅 실패도 폴오버 진행을 막지 않는다
                    if i < len(candidates) - 1:
                        try:
                            await self.board.agent_update(
                                role, activity=f"↪ failover [{name}] (error: {e})"
                            )
                        except Exception:
                            pass
                    continue  # 다음 후보 시도
        # 바깥 안전망: 예기치 못한 오류도 절대 전파 금지(형제 gather 취소 방지).
        except Exception as e:
            res = RoleResult(ok=False, error=f"runner error: {e}")
            try:
                await self.board.log_event(role, f"error [{chosen}]: {e}")
            except Exception:
                pass

        if res is None:
            res = RoleResult(ok=False, error="no backend candidate")

        # 감독(PM/PL)은 결과파일을 안 남겨도 자연스럽다. 그 외 역할은 결과 JSON 이 계약.
        result_required = spec.phase != PHASE_SUPERVISOR
        # 페이즈/역할을 넘겨 _ok 판정을 페이즈별 계약에 맞춘다 (#97).
        outcome = self._read_result(result_path, res, result_required, phase=spec.phase, role=role)
        # #10: done 스테이지(로그/agent_update)는 후보 try/except 밖이라, 여기서 예외가 나면
        # 바깥 run_role 의 setup-실패 처리로 빠져 '성공한 역할'이 'failed' 로 오보된다. 이미 결과는
        # outcome 으로 확정됐으므로, 부수적 로깅/상태 갱신 실패는 흡수하고 outcome 을 보존한다.
        try:
            cost = f" ${role_cost:.4f}" if role_cost else ""
            await self.board.log_event(
                role,
                f"done [{chosen}] ok={res.ok}{cost}" + ("" if res.ok else f" err={res.error}"),
            )
            summary = (outcome.get("status") or "?") + (
                f" · {len(outcome.get('artifacts', []))} files" if outcome.get("artifacts") else ""
            )
            await self.board.agent_update(
                role,
                status="idle",
                backend=chosen,
                model=res.model or self.cfg.model_for(chosen),
                message=res.final_message or (res.error or ""),
                activity=f"✓ done [{chosen}]{cost} → {summary}"
                if res.ok
                else f"✗ failed [{chosen}]{cost}: {res.error}",
            )
        except Exception as e:
            # 성공한 백엔드 결과를 setup 실패로 오보하지 않도록 오류만 best-effort 로 남기고 흡수.
            try:
                await self.board.log_event(role, f"done-stage logging error [{chosen}]: {e}")
            except Exception:
                pass
        return outcome

    async def _run_with_retries(self, backend, req, role, key):
        attempts = max(1, self.cfg.retries + 1)
        last = None
        total_cost = 0.0
        total_tokens = 0
        estimated = False
        for i in range(attempts):
            try:
                res = await backend.run_role(req)
            except Exception as e:
                res = RoleResult(ok=False, error=f"backend raised: {e}")
            if res.cost_usd:
                total_cost += res.cost_usd
                estimated = estimated or res.cost_estimated
            if res.tokens:
                total_tokens += res.tokens
            if res.ok:
                if total_cost:
                    res.cost_usd = total_cost
                    res.cost_estimated = estimated
                if total_tokens:
                    res.tokens = total_tokens
                return res
            last = res
            if i < attempts - 1:
                # #11: 지수 백오프에 지터를 더한다(0.5~1.0배). 레이트리밋 백엔드에 여러 역할이
                # 동시에 재시도하며 동시 폭주(thundering herd)하는 것을 방지한다. 캡(60s)을 먼저
                # 적용한 뒤 지터를 곱해 항상 캡 이하가 되게 한다.
                base = min(self.cfg.retry_backoff * (2**i), 60.0)
                delay = base * (0.5 + random.random() / 2)
                await self.board.log_event(
                    role, f"retry {i + 1}/{attempts - 1} after err: {res.error} (in {delay:.0f}s)"
                )
                await asyncio.sleep(delay)
        if last is not None:
            if total_cost:
                last.cost_usd = total_cost
                last.cost_estimated = estimated
            if total_tokens:
                last.tokens = total_tokens
        return last

    @staticmethod
    def _read_result(
        result_path,
        res,
        result_required: bool = True,
        phase: str | None = None,
        role: str | None = None,
    ) -> dict:
        # 백엔드 호출이 성공한 경우에만 결과파일을 신뢰한다. 실패 시 남아있는
        # 부분/이전 결과파일을 성공으로 오탐하지 않도록 합성 실패 결과를 쓴다.
        # phase/role 을 _coerce_result 로 전달해 페이즈별 계약(아키텍트=units 필수 등)을 반영 (#97).
        if res.ok and result_path.exists():
            # #audit13: 결과 파일이 symlink 면 신뢰하지 않는다. sandbox 백엔드가 결과 경로를
            #           외부(예: /etc/passwd)를 가리키는 symlink 로 바꿔치기해 임의 파일을
            #           '결과 JSON'으로 읽히는 것을 차단(결과 무결성). 명시적 검사 + 아래
            #           os.open 의 O_NOFOLLOW 로 TOCTOU 레이스까지 원자적으로 막는다.
            if result_path.is_symlink():
                return {
                    "status": "failed",
                    "artifacts": [],
                    "notes": [],
                    "blockers": ["result file is a symlink (rejected; contract violation)"],
                    "units": [],
                    "_ok": False,
                }
            # #22: read_text() 로 통째 올리기 전에 크기를 검사 — 거대 결과 파일은 메모리를
            #      폭주시키므로 읽지 않고 계약 위반(실패)으로 처리한다.
            try:
                rsize = result_path.stat().st_size
            except OSError:
                rsize = 0
            if rsize > _MAX_RESULT_BYTES:
                return {
                    "status": "failed",
                    "artifacts": [],
                    "notes": [],
                    "blockers": [
                        f"result file too large ({rsize} > {_MAX_RESULT_BYTES} bytes; "
                        "contract violation)"
                    ],
                    "units": [],
                    "_ok": False,
                }
            try:
                # O_NOFOLLOW: 최종 컴포넌트가 symlink 면 open 이 ELOOP 로 실패 → 아래 except 가
                # 계약 위반으로 처리(검사~open 사이 symlink 바꿔치기 레이스까지 원자적 차단).
                # Windows 에는 O_NOFOLLOW 가 없으므로 getattr 폴백(상단 is_symlink 검사로 보완).
                fd = os.open(result_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                # #audit15: os.fdopen 이 raise 하면 raw fd 가 누수되므로 close 후 재전파.
                try:
                    fh = os.fdopen(fd, "rb")
                except BaseException:
                    os.close(fd)
                    raise
                with fh:
                    raw = fh.read(_MAX_RESULT_BYTES + 1)
                if len(raw) > _MAX_RESULT_BYTES:
                    return {
                        "status": "failed",
                        "artifacts": [],
                        "notes": [],
                        "blockers": [
                            "result file too large "
                            f"(> {_MAX_RESULT_BYTES} bytes; contract violation)"
                        ],
                        "units": [],
                        "_ok": False,
                    }
                # utf-8-sig: 선행 UTF-8 BOM 이 있어도 유효 JSON 으로 디코드(BOM 거부 방지)
                data = json.loads(raw.decode("utf-8-sig"))
                return _coerce_result(data, res, phase=phase, role=role)
            except Exception:
                # 결과파일이 있는데 깨졌으면 계약 위반 → 성공으로 오탐하지 않는다
                return {
                    "status": "failed",
                    "artifacts": [],
                    "notes": [],
                    "blockers": ["result file unparseable (contract violation)"],
                    "units": [],
                    "_ok": False,
                }
        if res.ok and result_required:
            # 결과파일이 필수인 역할(dev/test/cicd/docs/architect 등)이 안 썼으면 계약 위반 → 실패
            return {
                "status": "failed",
                "artifacts": [],
                # #12: final_message 가 str 이 아닐 수도 있으니 방어적으로 str() 후 슬라이스
                "notes": [str(res.final_message)[:300]] if res.final_message else [],
                "blockers": ["no result file written (contract violation)"],
                "units": [],
                "_ok": False,
            }
        # 결과파일이 불필요한 역할(감독) 또는 백엔드 실패 → 백엔드 결과로 합성
        return {
            "status": "done" if res.ok else "failed",
            "artifacts": [],
            # #12: final_message 가 str 이 아닐 수도 있으니 방어적으로 str() 후 슬라이스
            "notes": [str(res.final_message)[:300]] if res.final_message else [],
            "blockers": [] if res.ok else [res.error or "unknown error"],
            "units": [],
            "_ok": res.ok,
        }


# 성공으로 인정하는 status 의 기준선(baseline). 페이즈 무관하게 우선 이 화이트리스트로 거른다
# — 여기 없는 값(fail/failure/error/incomplete 등)이거나 blocker 가 있으면 _ok=False (#97).
_SUCCESS_STATUSES = frozenset(
    {
        "done",
        "designed",
        "dev_done",
        "tested",
        "pass",
        "passed",
        "ok",
        "success",
        "complete",
        "completed",
    }
)

# 페이즈별 성공 status (#97). baseline 통과 후 추가로 페이즈 계약에 맞는지 확인한다.
# 보수적으로 — mock 백엔드(designed/dev_done/tested/done) 와 기존 테스트를 깨지 않도록
# 각 페이즈의 '정상' 값 + 범용 완료 표현(done/complete/completed/ok/success)을 함께 허용한다.
# 명백히 잘못된 조합(예: 설계 역할이 dev_done, 개발 역할이 designed)만 추가로 _ok=False 로 잡는다.
_GENERIC_DONE = frozenset({"done", "ok", "success", "complete", "completed"})
_PHASE_SUCCESS_STATUSES: dict[str, frozenset[str]] = {
    "design": _GENERIC_DONE | {"designed", "pass", "passed", "tested"},
    "dev": _GENERIC_DONE | {"dev_done", "pass", "passed"},
    "test": _GENERIC_DONE | {"tested", "pass", "passed"},
    "cicd": _GENERIC_DONE | {"pass", "passed"},
    "docs": _GENERIC_DONE | {"pass", "passed"},
}


def _as_list(v) -> list:
    if isinstance(v, list):
        return v
    if v in (None, ""):
        return []
    return [v]


def _under_results_dir(path, board) -> bool:
    """result_path 가 board.results_dir 하위에 있는지 검증 (디렉터리 밖 삭제 방지; #25).

    unit id 는 보드에서 슬러그되어 안전하지만, 방어적으로 resolve 후 results_dir 경계를 확인한다.
    """
    try:
        results_dir = board.results_dir.resolve()
        return board.results_dir.exists() and results_dir in path.resolve().parents
    except Exception:
        return False


def _safe_rel_artifact(raw) -> str | None:
    """아티팩트 경로 경량 검증 (보드 add_artifacts 와 동일 정책; #11).

    str 만 허용하고 strip 후 절대경로(POSIX '/' · Windows 드라이브/역슬래시) 와 '..' traversal
    토큰을 drop. 타깃 프로젝트 기준 상대경로만 유지. 비-str/빈값/안전하지 않은 값은 None.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.startswith("/") or s.startswith("\\"):
        return None
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():  # 예: C:\... 드라이브 절대경로
        return None
    parts = re.split(r"[\\/]+", s)
    if ".." in parts:
        return None
    return s


def _coerce_result(data: dict, res, phase: str | None = None, role: str | None = None) -> dict:
    """Normalize an agent-written result file to the expected schema.

    status 를 소문자 정규화하고, 에이전트가 status=failed/blocked/error 또는 blockers 를 보고하면
    (백엔드 호출이 성공했더라도) _ok=False 로 내린다. 그래야 스케줄러 판정이 제대로 잡힌다.

    phase/role 이 주어지면(#97) baseline 화이트리스트 통과 후 추가로 페이즈별 계약을 검사한다:
    - status 가 해당 페이즈의 성공 집합에 들어야 한다(명백히 어긋난 조합만 추가로 거른다).
    - 아키텍트(설계 페이즈, 핵심 역할)는 units 배열이 비어있지 않아야 한다(#98 과 동일한 설계 계약).
      같은 design 페이즈라도 testsheet-creator 는 units 를 만들지 않으므로 units 검사 대상이 아니다.
    phase 미지정 시 기존 동작(baseline 화이트리스트만) 그대로 — 기존 테스트 호환.
    """
    if not isinstance(data, dict):
        # [] / "done" / 숫자 같은 비-객체 JSON 은 계약 위반 → 성공으로 보지 않는다
        return {
            "status": "failed",
            "artifacts": [],
            "notes": [],
            "blockers": ["result JSON is not an object (contract violation)"],
            "units": [],
            "_ok": False,
        }
    raw_status = data.get("status")
    if res.ok and phase is not None and (raw_status is None or str(raw_status).strip() == ""):
        return {
            "status": "failed",
            "artifacts": [],
            "notes": [str(n) for n in _as_list(data.get("notes"))],
            "blockers": ["result status missing (contract violation)"],
            "units": [u for u in _as_list(data.get("units")) if isinstance(u, dict)],
            "_ok": False,
        }
    status = str(raw_status or ("done" if res.ok else "failed")).strip().lower()
    # 빈/공백 blocker 슬롯은 무시 (LLM 이 빈 칸을 남겨도 불필요한 실패가 나지 않게)
    blockers = [s for b in _as_list(data.get("blockers")) if (s := str(b).strip())]
    units = [u for u in _as_list(data.get("units")) if isinstance(u, dict)]
    # 1) baseline: 실패 변형을 다 나열하기보다 성공 status 만 허용(whitelist) — 기존 동작 유지
    ok = res.ok and status in _SUCCESS_STATUSES and not blockers
    # 2) 페이즈별 계약 추가 검사(#97). baseline 을 통과한 경우에만 좁힌다(over-tighten 방지).
    if ok and phase is not None:
        allowed = _PHASE_SUCCESS_STATUSES.get(phase)
        if allowed is not None and status not in allowed:
            # 예: 개발 역할이 'designed', 설계 역할이 'dev_done' 같은 어긋난 조합
            ok = False
            blockers = [*blockers, f"status '{status}' not valid for phase '{phase}'"]
        elif role == "architecture-engineer" and not units:
            # 아키텍트 계약: spec 을 units 로 분해해야 성공 (#97/#98).
            # testsheet-creator 는 같은 design 페이즈라도 units 가 없어 정상이므로 제외.
            ok = False
            blockers = [*blockers, "architect result has no units (contract violation)"]
    # 아티팩트: str 만, 절대경로/'..' 등 안전하지 않은 값 drop (보드 정책과 일치; #11)
    artifacts = [s for a in _as_list(data.get("artifacts")) if (s := _safe_rel_artifact(a))]
    out = {
        "status": status,
        "artifacts": artifacts,
        "notes": [str(n) for n in _as_list(data.get("notes"))],
        "blockers": blockers,
        "units": units,
        "_ok": ok,
    }
    for key in ("failure_kind", "repair_owner", "repair_instruction", "command", "stderr_tail"):
        value = data.get(key)
        if value not in (None, ""):
            out[key] = str(value)
    return out
