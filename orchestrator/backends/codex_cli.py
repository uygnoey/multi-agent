"""④ Codex 구독(CLI) 백엔드.

codex exec ... --cd <타깃> --sandbox workspace-write --json -o <out> --skip-git-repo-check.
시스템 프롬프트 플래그가 없으므로 역할 프롬프트를 prompt 에 prepend 한다.
공유 지침은 타깃의 AGENTS.md 가 자동 로드한다. 인증: codex login 또는 CODEX_API_KEY.
"""

from __future__ import annotations

import asyncio
import shutil

from .base import Backend, RoleRequest, RoleResult


class CodexCLIBackend(Backend):
    name = "codex"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("codex"):
            return False, "codex CLI 미설치 (npm i -g @openai/codex)"
        return True, "ready (codex login 또는 CODEX_API_KEY)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        prompt = f"[SYSTEM ROLE INSTRUCTIONS]\n{req.system_prompt}\n\n[TASK]\n{req.prompt}"
        out_path = req.cwd / ".orchestrator" / "results" / f"{req.role}__codex_last.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "codex",
            "exec",
            prompt,
            "--cd",
            str(req.cwd),
            "--sandbox",
            "workspace-write",
            "--json",
            "-o",
            str(out_path),
            "--skip-git-repo-check",
        ]
        if req.model:
            cmd += ["--model", req.model]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(req.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate()
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        if proc.returncode != 0:
            return RoleResult(
                ok=False, error=err.decode(errors="replace")[:500] or f"exit {proc.returncode}"
            )

        final = ""
        if out_path.exists():
            try:
                final = out_path.read_text(encoding="utf-8")[:2000]
            except Exception:
                pass
        return RoleResult(ok=True, final_message=final or "codex exec ok")
