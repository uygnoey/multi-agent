"""claude-team backend: native Team Agents lead-dispatch.

Runs a single Claude Code "lead" session that dispatches the role as a native
subagent via the Task tool. The role definition is loaded from the target's
`.claude/agents/<role>.md` (exposed at scaffold time). Subscription or API key.
"""

from __future__ import annotations

import json
import shutil

from .base import Backend, RoleRequest, RoleResult, run_subprocess


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
            "json",
            "--allowedTools",
            "Task,Read,Write,Edit,Bash",
            "--permission-mode",
            "acceptEdits",
        ]
        if req.model:
            cmd += ["--model", req.model]
        try:
            rc, out, err, timed_out = await run_subprocess(
                cmd, str(req.cwd), req.timeout, req.live_log_path
            )
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        if timed_out:
            return RoleResult(ok=False, error=f"claude-team timed out after {req.timeout}s")
        if rc != 0:
            return RoleResult(ok=False, error=err.decode(errors="replace")[:500] or f"exit {rc}")

        text = out.decode(errors="replace")
        final, cost = text, None
        try:
            data = json.loads(text)
            final = data.get("result", text)
            cost = data.get("total_cost_usd")
        except Exception:
            pass
        return RoleResult(ok=True, final_message=final, cost_usd=cost, raw=text[:2000])
