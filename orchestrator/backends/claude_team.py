"""claude-team backend: native Team Agents lead-dispatch.

Runs a single Claude Code "lead" session that dispatches the role as a native
subagent via the Task tool. The role definition is loaded from the target's
`.claude/agents/<role>.md` (exposed at scaffold time). Subscription or API key.
"""

from __future__ import annotations

import shutil

from .base import Backend, RoleRequest, RoleResult, run_subprocess
from .claude_cli import (
    _is_unknown_budget_flag_error,
    budget_arg,
    claude_stream_line,
    parse_stream_result,
    stream_result_has_error,
)


class ClaudeTeamBackend(Backend):
    name = "claude-team"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("claude"):
            return False, "claude CLI 미설치 (npm i -g @anthropic-ai/claude-code)"
        # #110: 바이너리 존재만 확인 — 로그인/인증은 검증하지 않는다(probe 회피).
        return True, "binary present (auth NOT verified: native subagent dispatch)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        lead_prompt = (
            "You are the team lead orchestrating specialists. Use the Task tool to delegate "
            f"the following task to the `{req.role}` subagent, then report its result. "
            f"Do not do the work yourself — the `{req.role}` subagent must do it.\n\n"
            f"{req.prompt}"
        )
        base_cmd = [
            "claude",
            "-p",
            lead_prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            # 역할 allowed_tools + 리드 디스패치용 Task (읽기전용 역할에 쓰기/실행 안 열리게)
            ",".join(dict.fromkeys(["Task", *req.allowed_tools])),
            "--permission-mode",
            "acceptEdits",
        ]
        if req.model:
            base_cmd += ["--model", req.model]
        # #24(#116): 검증 결과 설치된 claude CLI(v2.1.x)에는 '--max-budget-usd' 플래그가 실재한다
        # (-p/--print 전용). req.budget 이 명시되면 per-call 예산 캡을 실제로 전달해 강제한다.
        budget_flag = ["--max-budget-usd", budget_arg(req.budget)] if req.budget is not None else []
        cmd = base_cmd + budget_flag
        # #26(#118): 그러나 turn-limit 플래그(--max-turns 등)는 동일 CLI 에 존재하지 않는다
        # (claude --help 로 검증: 0건). 없는 플래그를 넘기면 매 호출이 깨지므로 추가하지 않는다
        # — req.max_turns 강제는 이 백엔드에서 불가(KEEP-DOCUMENTED). 긴 세션은 timeout 으로 통제.
        try:
            rc, out, err, timed_out = await run_subprocess(
                cmd, str(req.cwd), req.timeout, req.live_log_path, line_render=claude_stream_line
            )
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        # #1(audit9): 설치된 claude CLI 가 '--max-budget-usd' 를 모르면(구버전/배포판 차이) rc!=0 로
        # 깨진다. stderr 가 '알 수 없는 옵션' 신호를 담으면 그 플래그만 빼고 한 번 재시도한다
        # (claude_cli 와 동일 취지). 재시도해도 실패하면 그 결과를 그대로 쓴다.
        if (
            budget_flag
            and not timed_out
            and rc != 0
            and _is_unknown_budget_flag_error(err.decode(errors="replace"))
        ):
            try:
                rc, out, err, timed_out = await run_subprocess(
                    base_cmd,
                    str(req.cwd),
                    req.timeout,
                    req.live_log_path,
                    line_render=claude_stream_line,
                )
            except Exception as e:
                return RoleResult(ok=False, error=str(e))

        if timed_out:
            return RoleResult(ok=False, error=f"claude-team timed out after {req.timeout}s")
        if rc != 0:
            # #7(#44): subagent dispatch/CLI 진단의 '끝부분'(마지막 에러 컨텍스트)이 살아남도록
            # 예전의 head 절단(err[:4000])이 아니라 tail(마지막 4000자, err[-4000:])을 보존한다.
            return RoleResult(ok=False, error=err.decode(errors="replace")[-4000:] or f"exit {rc}")

        final, cost, model, tokens = parse_stream_result(out)
        # #L14: cost_estimated 는 env var 가 아니라 CLI 가 실제로 total_cost_usd 를 보고했는지로
        # 판정한다. cost 가 채워졌으면(CLI 보고) 추정 아님, None 이면(미보고) 추정으로 표기.
        cost_estimated = cost is None
        if stream_result_has_error(out):
            return RoleResult(
                ok=False,
                error=final or "claude-team stream result reported an error",
                final_message=final,
                cost_usd=cost,
                model=model or req.model,
                tokens=tokens,
                cost_estimated=cost_estimated,
            )
        return RoleResult(
            ok=True,
            final_message=final or "(done)",
            cost_usd=cost,
            model=model or req.model,
            tokens=tokens,
            cost_estimated=cost_estimated,
        )
