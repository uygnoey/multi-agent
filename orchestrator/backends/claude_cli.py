"""③ Claude Code 구독(CLI) 백엔드.

claude -p ... --output-format json (cwd=타깃에서 실행).
ANTHROPIC_API_KEY 미설정 시 로그인된 구독을 사용한다. cwd 의 CLAUDE.md 자동 로드.
"""

from __future__ import annotations

import json
import shutil

from .base import Backend, RoleRequest, RoleResult, run_subprocess


class ClaudeCLIBackend(Backend):
    name = "claude-cli"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("claude"):
            return False, "claude CLI 미설치 (npm i -g @anthropic-ai/claude-code)"
        return True, "ready (로그인 구독 또는 ANTHROPIC_API_KEY)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        cmd = [
            "claude",
            "-p",
            req.prompt,
            "--output-format",
            "json",
            "--append-system-prompt",
            req.system_prompt,
            "--allowedTools",
            ",".join(req.allowed_tools),
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
            return RoleResult(ok=False, error=f"claude-cli timed out after {req.timeout}s")
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
