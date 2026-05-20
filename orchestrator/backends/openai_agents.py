"""② OpenAI Agents SDK 백엔드 (API 키 방식).

pip install openai-agents / 인증 OPENAI_API_KEY.
내장 파일/배시 툴이 없으므로 타깃 cwd 로 스코프 한정한 function_tool 을 직접 제공한다.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .base import Backend, RoleRequest, RoleResult


class OpenAIAgentsBackend(Backend):
    name = "openai-agents"

    def available(self) -> tuple[bool, str]:
        try:
            import agents  # noqa: F401
        except Exception:
            return False, "openai-agents 미설치 (pip install openai-agents)"
        if not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY 미설정"
        return True, "ready"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        try:
            from agents import Agent, Runner, function_tool
        except Exception as e:  # pragma: no cover
            return RoleResult(ok=False, error=f"import 실패: {e}")

        root = req.cwd.resolve()

        def _safe(rel: str) -> Path:
            p = (root / rel).resolve()
            if root != p and root not in p.parents:
                raise ValueError(f"path escapes project dir: {rel}")
            return p

        @function_tool
        def read_file(path: str) -> str:
            """파일 내용을 읽는다 (타깃 디렉터리 한정)."""
            p = _safe(path)
            return p.read_text(encoding="utf-8") if p.exists() else f"<no file: {path}>"

        @function_tool
        def write_file(path: str, content: str) -> str:
            """파일을 생성/덮어쓴다 (타깃 디렉터리 한정)."""
            p = _safe(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        @function_tool
        def list_dir(path: str = ".") -> str:
            """디렉터리 목록 (타깃 디렉터리 한정)."""
            p = _safe(path)
            if not p.exists():
                return f"<no dir: {path}>"
            return "\n".join(sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()))

        @function_tool
        def run_bash(command: str) -> str:
            """타깃 cwd 에서 셸 명령을 실행한다 (120s 타임아웃)."""
            try:
                r = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                return (r.stdout + r.stderr)[:4000]
            except Exception as e:
                return f"<bash error: {e}>"

        kwargs = dict(
            name=req.role,
            instructions=req.system_prompt,
            tools=[read_file, write_file, list_dir, run_bash],
        )
        if req.model:
            kwargs["model"] = req.model
        agent = Agent(**kwargs)
        try:
            result = await Runner.run(agent, req.prompt, max_turns=req.max_turns)
        except Exception as e:
            return RoleResult(ok=False, error=str(e))
        return RoleResult(ok=True, final_message=str(getattr(result, "final_output", "")))
