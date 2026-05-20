"""① Claude Agent SDK 백엔드 (API 키 방식).

pip install claude-agent-sdk / 인증 ANTHROPIC_API_KEY.
import 는 lazy — 패키지가 없어도 모듈 로드/--check 는 동작한다.
"""
from __future__ import annotations

import os

from .base import Backend, RoleRequest, RoleResult


def _make_options(cls, **kwargs):
    """버전에 따라 지원 안 되는 kwarg 를 점진적으로 제거하며 옵션 생성."""
    optional = ["max_budget_usd", "model", "setting_sources", "max_turns", "permission_mode"]
    while True:
        try:
            return cls(**kwargs)
        except TypeError as e:
            removed = False
            for k in list(kwargs):
                if k in str(e):
                    kwargs.pop(k)
                    removed = True
                    break
            if not removed:
                for k in optional:
                    if k in kwargs:
                        kwargs.pop(k)
                        removed = True
                        break
            if not removed:
                raise


def _extract_text(msg) -> str:
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            t = getattr(b, "text", None)
            if t:
                out.append(t)
            elif isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
        return "\n".join(out)
    return ""


class ClaudeSDKBackend(Backend):
    name = "claude-sdk"

    def available(self) -> tuple[bool, str]:
        try:
            import claude_agent_sdk  # noqa: F401
        except Exception:
            return False, "claude-agent-sdk 미설치 (pip install claude-agent-sdk)"
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False, "ANTHROPIC_API_KEY 미설정 (SDK 는 구독 자동 폴백 안 함)"
        return True, "ready"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, query
        except Exception as e:  # pragma: no cover
            return RoleResult(ok=False, error=f"import 실패: {e}")

        kwargs = dict(
            system_prompt=req.system_prompt,
            allowed_tools=list(req.allowed_tools),
            permission_mode="acceptEdits",
            cwd=str(req.cwd),
            max_turns=req.max_turns,
            setting_sources=["project"],
        )
        if req.model:
            kwargs["model"] = req.model
        if req.budget is not None:
            kwargs["max_budget_usd"] = req.budget
        options = _make_options(ClaudeAgentOptions, **kwargs)

        final, cost = "", None
        try:
            async for msg in query(prompt=req.prompt, options=options):
                text = _extract_text(msg)
                if text:
                    final = text
                c = getattr(msg, "total_cost_usd", None)
                if c is not None:
                    cost = c
        except Exception as e:
            return RoleResult(ok=False, error=str(e), final_message=final, cost_usd=cost)
        return RoleResult(ok=True, final_message=final, cost_usd=cost)
