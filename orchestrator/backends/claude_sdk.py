"""① Claude Agent SDK 백엔드 (API 키 방식).

pip install claude-agent-sdk / 인증 ANTHROPIC_API_KEY.
import 는 lazy — 패키지가 없어도 모듈 로드/--check 는 동작한다.
"""

from __future__ import annotations

import asyncio
import os

from .base import Backend, RoleRequest, RoleResult


def _make_options(cls, dropped: list | None = None, **kwargs):
    """SDK 버전에 따라 지원되는 인자만 골라 옵션 생성 (시그니처 기반).

    예전의 에러문자열 부분매칭 방식은 지원되는 인자를 잘못 제거할 수 있어 폐기.
    dropped 리스트를 주면, 호환성 때문에 떨어뜨린 인자 이름을 거기에 기록한다(#113).
    """
    import inspect

    try:
        params = inspect.signature(cls).parameters
        accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in params.values())
        if not accepts_kwargs:
            removed = [k for k in kwargs if k not in params]
            if dropped is not None:
                dropped.extend(removed)
            kwargs = {k: v for k, v in kwargs.items() if k in params}
    except (ValueError, TypeError):
        pass
    try:
        return cls(**kwargs)
    except TypeError:
        # 최후 방어: 선택 인자를 제거하며 재시도
        for k in ("agents", "max_budget_usd", "setting_sources", "model", "max_turns"):
            if k in kwargs and dropped is not None:
                dropped.append(k)
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
        dropped: list[str] = []
        options = _make_options(ClaudeAgentOptions, dropped=dropped, **kwargs)

        # #21(#113): 사용자가 명시적으로 req.budget 을 지정했는데 설치된 SDK 가 max_budget_usd
        # 인자를 받지 못해 떨어졌으면, 조용히 진행하지 않는다. 텍스트 노트 + 구조화된
        # RoleResult.warning 양쪽으로 표면화해 호출자/로그/리포트에서 반드시 보이게 한다.
        # (budget is None 인 정상 경로는 영향 없음 — budget_dropped 가 항상 False)
        budget_dropped = req.budget is not None and "max_budget_usd" in dropped
        budget_warning = (
            f"requested per-call budget cap ${req.budget} was NOT enforced: installed "
            f"claude-agent-sdk rejected max_budget_usd; role ran WITHOUT a per-call cap "
            f"(cumulative budget is still enforced by the runner)"
            if budget_dropped
            else None
        )

        state = {"final": "", "cost": None, "model": req.model, "tokens": None}

        def _capture_meta(msg) -> None:
            # #45: SDK 결과가 usage/model 을 노출하면 best-effort 로 캡처 (getattr 가드).
            c = getattr(msg, "total_cost_usd", None)
            if c is not None:
                state["cost"] = c
            m = getattr(msg, "model", None)
            if m:
                state["model"] = m
            usage = getattr(msg, "usage", None)
            if usage is not None:
                if isinstance(usage, dict):
                    it = usage.get("input_tokens") or 0
                    ot = usage.get("output_tokens") or 0
                    tt = usage.get("total_tokens")
                else:
                    it = getattr(usage, "input_tokens", 0) or 0
                    ot = getattr(usage, "output_tokens", 0) or 0
                    tt = getattr(usage, "total_tokens", None)
                total = int(tt) if tt else int(it + ot)
                if total:
                    state["tokens"] = total

        async def _consume():
            async for msg in query(prompt=req.prompt, options=options):
                text = _extract_text(msg)
                if text:
                    state["final"] = text
                _capture_meta(msg)

        def _note(base: str) -> str:
            if budget_dropped:
                return (
                    f"{base} [warning: installed SDK rejected max_budget_usd "
                    f"(${req.budget}); ran WITHOUT budget cap]"
                ).strip()
            return base

        try:
            await asyncio.wait_for(_consume(), timeout=req.timeout)
        except asyncio.TimeoutError:
            return RoleResult(
                ok=False,
                error=_note(f"claude-sdk timed out after {req.timeout}s"),
                final_message=state["final"],
                cost_usd=state["cost"],
                model=state["model"],
                tokens=state["tokens"],
                warning=budget_warning,
            )
        except Exception as e:
            return RoleResult(
                ok=False,
                error=_note(str(e)),
                final_message=state["final"],
                cost_usd=state["cost"],
                model=state["model"],
                tokens=state["tokens"],
                warning=budget_warning,
            )
        return RoleResult(
            ok=True,
            final_message=_note(state["final"]) if budget_dropped else state["final"],
            cost_usd=state["cost"],
            model=state["model"],
            tokens=state["tokens"],
            warning=budget_warning,
        )
