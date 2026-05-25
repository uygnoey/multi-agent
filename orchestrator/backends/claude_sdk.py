"""① Claude Agent SDK 백엔드 (API 키 방식).

pip install claude-agent-sdk / 인증 ANTHROPIC_API_KEY.
import 는 lazy — 패키지가 없어도 모듈 로드/--check 는 동작한다.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from pathlib import Path

from .base import Backend, RoleRequest, RoleResult

# #3(audit9): SDK 가 total_cost_usd 를 노출하지 않는 구독 모드에서는 cost 가 None 으로 남아
# codex/openai 처럼 토큰×단가 추정치를 내지 못했다. 다른 백엔드와 일관되게, in/out 토큰과
# 모델 단가가 있으면 추정 비용을 내고 cost_estimated=True 로 표기한다(허위 비용 날조 금지:
# 모델/토큰을 알 수 없으면 None 으로 둔다). 단가는 [input, output] (1M 토큰당 USD).
# 가격 변동 대응: 환경변수 ANTHROPIC_PRICING_FILE 로 외부 JSON 을 지정할 수 있다.
_ANTHROPIC_FALLBACK_PRICING = {
    "claude-opus-4": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "claude-3-haiku": (0.25, 1.25),
}

# #M05: base 키 뒤에 붙는 접미사를 base 단가로 매핑한다. 현행 모델 ID 는 패밀리와 날짜 사이에
# 포인트 버전이 들어가므로(claude-opus-4-1-20250805, claude-sonnet-4-5) 포인트 버전 세그먼트
# `(-\d+)*` 를 허용해야 한다. 예전 정규식 `^-(?:\d{6,8}|latest)$` 는 순수 날짜/latest 만 매칭해
# 현행 모델이 단가표에 닿지 못하고 구독모드 비용 추정이 None 으로 떨어졌다.
_ANTHROPIC_DATE_SUFFIX = re.compile(r"^(?:-\d+)*(?:-(?:\d{6,8}|latest))?$")


def _anthropic_pricing() -> dict:
    """ANTHROPIC_PRICING_FILE(있으면) → 코드 fallback. 값은 [input, output] (1M 토큰당 USD)."""
    path = os.environ.get("ANTHROPIC_PRICING_FILE")
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            out = {}
            for k, v in data.items():
                if k.startswith("_") or not isinstance(v, (list, tuple)) or len(v) < 2:
                    continue
                prices = (float(v[0]), float(v[1]))
                if any((not math.isfinite(x)) or x < 0 for x in prices):
                    continue
                out[k] = prices
            if out:
                return out
        except Exception:
            pass
    return dict(_ANTHROPIC_FALLBACK_PRICING)


def _anthropic_price_for(model: str | None):
    """모델명에 맞는 단가 (input, output). 정확 매칭 → 날짜/latest 접미사 base 폴백 → None.

    codex_cli/_price_for, openai_agents/_openai_price_for 와 동일한 보수적 규칙
    (긴 키 우선, 알 수 없는 변형은 None — 허위 비용 날조 금지).
    """
    m = (model or "").lower().strip()
    if not m:
        return None
    pricing = _anthropic_pricing()
    if m in pricing:
        return pricing[m]
    for key in sorted(pricing, key=len, reverse=True):
        if m.startswith(key) and _ANTHROPIC_DATE_SUFFIX.match(m[len(key) :]):
            return pricing[key]
    return None


def _estimate_anthropic_cost(model: str | None, in_tokens: int, out_tokens: int):
    """모델 단가 × 토큰으로 추정 비용(USD). 모델 미지정/단가표 미등록이면 None."""
    p = _anthropic_price_for(model)
    if not p:
        return None
    in_price, out_price = p
    cost = (in_tokens or 0) / 1e6 * in_price + (out_tokens or 0) / 1e6 * out_price
    return round(cost, 6)


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

        # #3(audit9): in/out 토큰을 별도로 누적해 USD 미보고(구독) 시 토큰×단가 추정에 쓴다.
        state = {
            "final": "",
            "cost": None,
            "model": req.model,
            "tokens": None,
            "in_tokens": 0,
            "out_tokens": 0,
        }

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
                # #3(audit9): 분리 in/out 토큰도 누적 (USD 미보고 시 비용 추정용).
                state["in_tokens"] = int(it or 0)
                state["out_tokens"] = int(ot or 0)

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

        def _resolve_cost() -> tuple[float | None, bool]:
            # #3(audit9): SDK 가 total_cost_usd 를 보고하면 그 실청구액을 그대로 쓴다(추정 아님).
            # 미보고(구독 모드)면 in/out 토큰×모델 단가로 추정하고 cost_estimated=True 로 표기 —
            # codex/openai 백엔드와 일관. 모델/토큰을 알 수 없으면 None(허위 비용 날조 금지).
            if state["cost"] is not None:
                return state["cost"], False
            est = _estimate_anthropic_cost(state["model"], state["in_tokens"], state["out_tokens"])
            if est is not None:
                return est, True
            # 비용은 못 냈지만 USD 미보고(구독)임은 다른 백엔드처럼 표면화한다.
            return None, True

        try:
            await asyncio.wait_for(_consume(), timeout=req.timeout)
        except asyncio.TimeoutError:
            cost, cost_estimated = _resolve_cost()
            return RoleResult(
                ok=False,
                error=_note(f"claude-sdk timed out after {req.timeout}s"),
                final_message=state["final"],
                cost_usd=cost,
                cost_estimated=cost_estimated,
                model=state["model"],
                tokens=state["tokens"],
                warning=budget_warning,
            )
        except Exception as e:
            cost, cost_estimated = _resolve_cost()
            return RoleResult(
                ok=False,
                error=_note(str(e)),
                final_message=state["final"],
                cost_usd=cost,
                cost_estimated=cost_estimated,
                model=state["model"],
                tokens=state["tokens"],
                warning=budget_warning,
            )
        cost, cost_estimated = _resolve_cost()
        return RoleResult(
            ok=True,
            final_message=_note(state["final"]) if budget_dropped else state["final"],
            cost_usd=cost,
            cost_estimated=cost_estimated,
            model=state["model"],
            tokens=state["tokens"],
            warning=budget_warning,
        )
