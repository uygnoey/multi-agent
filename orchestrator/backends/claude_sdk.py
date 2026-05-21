"""① Claude Agent SDK 백엔드 (API 키 방식).

pip install claude-agent-sdk / 인증 ANTHROPIC_API_KEY.
import 는 lazy — 패키지가 없어도 모듈 로드/--check 는 동작한다.
"""

from __future__ import annotations

import asyncio
import os

from .base import Backend, RoleRequest, RoleResult


def _make_options(cls, **kwargs):
    """SDK 버전에 따라 지원되는 인자만 골라 옵션 생성 (시그니처 기반).

    예전의 에러문자열 부분매칭 방식은 지원되는 인자를 잘못 제거할 수 있어 폐기.
    """
    import inspect

    try:
        params = inspect.signature(cls).parameters
        accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in params.values())
        if not accepts_kwargs:
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (ValueError, TypeError):
        pass
    try:
        return cls(**kwargs)
    except TypeError:
        # 최후 방어: 선택 인자를 제거하며 재시도
        for k in ("agents", "max_budget_usd", "setting_sources", "model", "max_turns"):
            kwargs.pop(k, None)
            try:
                return cls(**kwargs)
            except TypeError:
                continue
        raise


def _build_agents(teammates: list[dict]):
    """Build {name: AgentDefinition} for SDK subagent delegation (resilient to signature)."""
    try:
        from claude_agent_sdk import AgentDefinition
    except Exception:
        return None
    out = {}
    for t in teammates:
        kwargs = {
            "description": t.get("description", t["name"]),
            "prompt": t.get("prompt", ""),
            "tools": t.get("tools", []),
        }
        if t.get("model"):
            kwargs["model"] = t["model"]
        try:
            out[t["name"]] = AgentDefinition(**kwargs)
        except TypeError:
            kwargs.pop("model", None)
            try:
                out[t["name"]] = AgentDefinition(**kwargs)
            except Exception:
                return None
    return out or None


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
        if req.delegate and req.teammates:
            agents = _build_agents(req.teammates)
            if agents:
                kwargs["agents"] = agents
                if "Task" not in kwargs["allowed_tools"]:
                    kwargs["allowed_tools"].append("Task")
        options = _make_options(ClaudeAgentOptions, **kwargs)

        state = {"final": "", "cost": None}

        async def _consume():
            async for msg in query(prompt=req.prompt, options=options):
                text = _extract_text(msg)
                if text:
                    state["final"] = text
                c = getattr(msg, "total_cost_usd", None)
                if c is not None:
                    state["cost"] = c

        try:
            await asyncio.wait_for(_consume(), timeout=req.timeout)
        except asyncio.TimeoutError:
            return RoleResult(
                ok=False,
                error=f"claude-sdk timed out after {req.timeout}s",
                final_message=state["final"],
                cost_usd=state["cost"],
            )
        except Exception as e:
            return RoleResult(
                ok=False, error=str(e), final_message=state["final"], cost_usd=state["cost"]
            )
        return RoleResult(ok=True, final_message=state["final"], cost_usd=state["cost"])
