"""claude-team backend: native Team Agents lead-dispatch.

Runs a single Claude Code "lead" session that dispatches the role as a native
subagent via the Task tool. The role definition is loaded from the target's
`.claude/agents/<role>.md` (exposed at scaffold time). Subscription or API key.
"""

from __future__ import annotations

import os
import shutil

from .base import Backend, RoleRequest, RoleResult, run_subprocess
from .claude_cli import claude_stream_line, parse_stream_result


class ClaudeTeamBackend(Backend):
    name = "claude-team"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("claude"):
            return False, "claude CLI 미설치 (npm i -g @anthropic-ai/claude-code)"
        return True, "ready (native subagent dispatch)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        lead_prompt = (
            "You are the team lead orchestrating specialists. Use the Task tool to delegate "
            f"the following task to the `{req.role}` subagent, then report its result. "
            f"Do not do the work yourself — the `{req.role}` subagent must do it.\n\n"
            f"{req.prompt}"
        )
        cmd = [
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
            cmd += ["--model", req.model]
        try:
            rc, out, err, timed_out = await run_subprocess(
                cmd, str(req.cwd), req.timeout, req.live_log_path, line_render=claude_stream_line
            )
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        if timed_out:
            return RoleResult(ok=False, error=f"claude-team timed out after {req.timeout}s")
        if rc != 0:
            return RoleResult(ok=False, error=err.decode(errors="replace")[:500] or f"exit {rc}")

        final, cost, model, tokens = parse_stream_result(out)
        return RoleResult(
            ok=True,
            final_message=final or "(done)",
            cost_usd=cost,
            model=model or req.model,
            tokens=tokens,
            cost_estimated=not os.environ.get("ANTHROPIC_API_KEY"),
        )
