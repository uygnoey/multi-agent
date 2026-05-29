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
# 를 허용해야 한다. 예전 정규식 `^-(?:\d{6,8}|latest)$` 는 순수 날짜/latest 만 매칭해 현행
# 모델이 단가표에 닿지 못하고 구독모드 비용 추정이 None 으로 떨어졌다.
# #RA-sdkre: 단, `(?:-\d+)*` 는 임의 개수의 `-digit` 세그먼트를 허용해, 미래의 가격이 다른
# 변형(가령 단가가 다른 claude-opus-4-2 류)까지 base 단가로 조용히 매핑할 위험이 있다. 포인트
# 버전 세그먼트를 최대 2개(`{0,2}`)로 제한해, 현행 ID(메이저·마이너 2단계)는 폴백하되 그 이상의
# 미지 변형은 매칭에서 빠져 None(허위 비용 날조 금지)으로 떨어지게 한다.
_ANTHROPIC_DATE_SUFFIX = re.compile(
    # #audit18(A7): 포인트 릴리스를 `-\d+` 로 확장한다. 예전엔 `-(?:1|5)` 만 허용해 현행 모델
    # ID(claude-opus-4-7 / claude-sonnet-4-7 등)가 매칭에서 빠져 비용 추정이 None 으로 떨어졌다.
    # 매칭 형태: exact base ("") / dated base ("-20250805"|"-latest") / 포인트 릴리스("-7",
    # "-7-20250805"). 위험: 단가가 다른 미래 SKU 까지 base 패밀리 단가로 매핑할 수 있으나, 같은
    # 패밀리 베이스 단가는 합리적 근사이고 None(추정 불가)보다 유용하다. 정확한 단가가 필요한
    # 포인트 릴리스는 _ANTHROPIC_PRICES 에 명시 키를 추가하면 그 키가 우선한다.
    r"^(?:-(?:\d{6,8}|latest)|-\d+(?:-(?:\d{6,8}|latest))?)?$"
)


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
    """모델 단가 × 토큰으로 추정 비용(USD). 모델 미지정/단가표 미등록이면 None.

    #RA-0tok: usage 가 있어도 토큰이 전부 0 이면(예: reasoning_tokens 만 잡힌 경우 등) 추정치가
    0.0 으로 나온다. "$0.00 (estimated)" 라는 오해를 막기 위해 정확히 0.0 인 추정치는 None 으로
    접는다(추정 비용 없음 → cost_estimated 도 False 로 표기되게).
    """
    p = _anthropic_price_for(model)
    if not p:
        return None
    in_price, out_price = p
    cost = (in_tokens or 0) / 1e6 * in_price + (out_tokens or 0) / 1e6 * out_price
    cost = round(cost, 6)
    return cost or None


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
                # #audit16: SDK 는 usage 를 ResultMessage 에 '누적(cumulative)' 값으로 싣는 형태가
                # 일반적이다. 메시지마다 max 로 취해 최종 누적값을 유지한다 → (a) 여러 메시지가
                # usage 를 실어도 중복 합산(double-count) 안 되고, (b) 뒤에 usage 없는/부분 메시지가
                # 와도 이전 값을 0/축소로 덮지 않는다. (이전엔 주석은 '누적'인데 코드는 단순 = 할당
                # 이라, 멀티-usage 메시지에서 마지막 값만 남아 과소계상 위험이 있었다.)
                # #audit21: malformed usage(문자열/inf/None 등)에서 int() 가 던지는
                # TypeError/ValueError/OverflowError 를 흡수한다. 이전엔 성공 stream 도
                # 비정상 usage 한 번에 _consume 전체가 실패로 바뀔 수 있었다.
                def _to_nonneg_int(v) -> int:
                    if v is None:
                        return 0
                    try:
                        iv = int(v)
                    except (TypeError, ValueError, OverflowError):
                        return 0
                    return iv if iv > 0 else 0

                it_i, ot_i = _to_nonneg_int(it), _to_nonneg_int(ot)
                state["in_tokens"] = max(state["in_tokens"], it_i)
                state["out_tokens"] = max(state["out_tokens"], ot_i)
                total = _to_nonneg_int(tt) if tt else it_i + ot_i
                if total:
                    state["tokens"] = max(state["tokens"] or 0, total)

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
            # #RA-0tok: _estimate_anthropic_cost 가 0.0(토큰 전부 0)을 None 으로 접으므로,
            # 여기 est is not None 분기는 0.0 추정치를 보고하지 않는다(아래 None,True 로 떨어짐).
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
