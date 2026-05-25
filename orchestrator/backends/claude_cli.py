"""③ Claude Code 구독(CLI) 백엔드.

claude -p ... --output-format stream-json (cwd=타깃에서 실행) → 생각/툴/응답이 실시간 스트리밍.
ANTHROPIC_API_KEY 미설정 시 로그인된 구독을 사용한다. cwd 의 CLAUDE.md 자동 로드.
"""

from __future__ import annotations

import json
import shutil

from .base import Backend, RoleRequest, RoleResult, run_subprocess

_MAX_LOG_FIELD_CHARS = 4000


def _clip_log_field(value: str) -> str:
    if len(value) <= _MAX_LOG_FIELD_CHARS:
        return value
    return (
        value[:_MAX_LOG_FIELD_CHARS] + f"... [truncated {len(value) - _MAX_LOG_FIELD_CHARS} chars]"
    )


def claude_stream_line(line_bytes: bytes) -> str | None:
    """stream-json 한 줄을 사람이 읽을 텍스트로 렌더 (실시간 로그용). 노이즈는 None."""
    try:
        o = json.loads(line_bytes)
    except Exception:
        return None
    t = o.get("type")
    if t == "system" and o.get("subtype") == "init":
        return f"· model: {o.get('model', '?')}  tools: {len(o.get('tools', []))}"
    if t == "assistant":
        out = []
        for c in o.get("message", {}).get("content") or []:
            ct = c.get("type")
            if ct == "text" and c.get("text", "").strip():
                out.append(c["text"])
            elif ct == "tool_use":
                inp = json.dumps(c.get("input", {}), ensure_ascii=False)
                out.append(f"🔧 {c.get('name')} {_clip_log_field(inp)}")
            elif ct == "thinking" and c.get("thinking"):
                out.append("💭 [thinking redacted]")
        return "\n".join(out) or None
    if t == "result":
        # #10(audit9): total_cost_usd 가 없으면(구독 모드) '$0' 처럼 보여 '0달러 썼다'로 오인된다.
        # 보고되지 않은 경우 '(n/a)' 로 표기해 '비용 미보고'와 '실제 0달러'를 구분한다.
        c = o.get("total_cost_usd")
        return f"✓ result (${c})" if c is not None else "✓ result (cost n/a)"
    return None


def parse_stream_result(out_bytes: bytes):
    """stream-json 전체에서 (final_text, cost, model, tokens) 추출."""
    final, cost, model, tokens = "", None, None, None
    for line in out_bytes.splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") == "system" and o.get("model"):
            model = o.get("model")
        if o.get("type") == "result":
            if o.get("is_error") or o.get("subtype") in ("error", "failure"):
                final = str(o.get("result") or o.get("error") or final)
                cost = o.get("total_cost_usd", cost)
                continue
            candidate = o.get("result", final)
            final = candidate if isinstance(candidate, str) else final
            cost = o.get("total_cost_usd", cost)
            u = o.get("usage") or {}
            if u:
                tokens = (
                    (u.get("input_tokens") or 0)
                    + (u.get("cache_creation_input_tokens") or 0)
                    + (u.get("cache_read_input_tokens") or 0)
                    + (u.get("output_tokens") or 0)
                )
    return final, cost, model, tokens


def stream_result_has_error(out_bytes: bytes) -> bool:
    for line in out_bytes.splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") == "result" and (
            o.get("is_error") or o.get("subtype") in ("error", "failure")
        ):
            return True
    return False


def budget_arg(value) -> str:
    s = f"{float(value):.12f}".rstrip("0").rstrip(".")
    if "." not in s:
        s += ".0"
    return s


# #1(audit9): 설치된 claude CLI 가 '--max-budget-usd' 플래그를 모르면 rc!=0 으로 깨진다
# (구버전/배포판 차이). stderr 에서 '알 수 없는 옵션' 신호를 감지해, 그 플래그만 빼고 한 번
# 재시도할 수 있게 한다. SDK 백엔드의 'dropped flag' graceful 메커니즘과 같은 취지.
_UNKNOWN_OPTION_HINTS = (
    "unknown option",
    "unrecognized option",
    "unrecognized argument",
    "no such option",
    "unexpected option",
    "unknown argument",
    "did you mean",
)


def _is_unknown_budget_flag_error(stderr: str) -> bool:
    """stderr 가 '--max-budget-usd' 를 모르는 CLI 의 에러 신호를 담고 있으면 True.

    플래그 이름과 'unknown/unrecognized/no such option' 류 힌트가 함께 보일 때만 True 로 판정해,
    예산 초과 등 무관한 에러를 오인하지 않는다(보수적 매칭).
    """
    low = (stderr or "").lower()
    if "--max-budget-usd" not in low and "max-budget-usd" not in low:
        return False
    return any(hint in low for hint in _UNKNOWN_OPTION_HINTS)


class ClaudeCLIBackend(Backend):
    name = "claude-cli"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("claude"):
            return False, "claude CLI 미설치 (npm i -g @anthropic-ai/claude-code)"
        # #109: '바이너리 존재'만 확인 — 로그인/인증은 검증하지 않는다(네트워크 probe 회피).
        # --check 가 정직하도록 인증 미검증임을 reason 에 명시한다.
        return True, "binary present (auth NOT verified: 로그인 구독 또는 ANTHROPIC_API_KEY 필요)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        base_cmd = [
            "claude",
            "-p",
            req.prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--append-system-prompt",
            req.system_prompt,
            "--allowedTools",
            ",".join(req.allowed_tools),
            "--permission-mode",
            "acceptEdits",
        ]
        if req.model:
            base_cmd += ["--model", req.model]
        # #24(#116): 검증 결과 설치된 claude CLI(v2.1.x)에는 '--max-budget-usd <amount>' 플래그가
        # 실재한다(-p/--print 전용). 따라서 req.budget 이 명시되면 per-call 예산 캡을 실제로
        # 전달해 강제한다. budget 미지정이면 추가하지 않는다(기존 동작 유지).
        budget_flag = ["--max-budget-usd", budget_arg(req.budget)] if req.budget is not None else []
        cmd = base_cmd + budget_flag
        # #25(#117): 그러나 동일 CLI 에 turn-limit 플래그(--max-turns 등)는 존재하지 않는다
        # (claude --help 로 검증: 0건). 없는 플래그를 넘기면 매 호출이 'unknown option'으로
        # 깨지므로 추가하지 않는다 — req.max_turns 강제는 이 백엔드에서 불가(KEEP-DOCUMENTED).
        # 긴/루핑 세션은 timeout 으로만 통제된다.
        try:
            rc, out, err, timed_out = await run_subprocess(
                cmd, str(req.cwd), req.timeout, req.live_log_path, line_render=claude_stream_line
            )
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        # #1(audit9): 설치된 claude CLI 가 '--max-budget-usd' 를 모르면(구버전/배포판 차이) rc!=0 로
        # 깨진다. stderr 가 '알 수 없는 옵션' 신호를 담으면, 그 플래그만 빼고 한 번 재시도한다
        # (SDK 의 dropped-flag graceful 메커니즘과 동일 취지). 재시도해도 실패하면 그 결과를 쓴다.
        if (
            budget_flag
            and not timed_out
            and rc != 0
            and _is_unknown_budget_flag_error(err.decode(errors="replace"))
        ):
            try:
                rc, out, err, timed_out = await run_subprocess(
                    base_cmd,
                    str(req.cwd),
                    req.timeout,
                    req.live_log_path,
                    line_render=claude_stream_line,
                )
            except Exception as e:
                return RoleResult(ok=False, error=str(e))

        if timed_out:
            return RoleResult(ok=False, error=f"claude-cli timed out after {req.timeout}s")
        if rc != 0:
            # #5(#42): auth/permission 진단의 '끝부분'(가장 관련 깊은 마지막 에러)이 살아남도록
            # 예전의 head 절단(err[:4000])이 아니라 tail(마지막 4000자, err[-4000:])을 보존한다.
            return RoleResult(ok=False, error=err.decode(errors="replace")[-4000:] or f"exit {rc}")

        final, cost, model, tokens = parse_stream_result(out)
        # #L14: cost_estimated 는 env var 가 아니라 CLI 가 실제로 total_cost_usd 를 보고했는지로
        # 판정한다. parse_stream_result 가 cost 를 채웠으면(CLI 보고) 추정 아님, None 이면(미보고/
        # 구독 모드 등) 추정으로 표기한다.
        cost_estimated = cost is None
        if stream_result_has_error(out):
            return RoleResult(
                ok=False,
                error=final or "claude-cli stream result reported an error",
                final_message=final,
                cost_usd=cost,
                model=model or req.model,
                tokens=tokens,
                cost_estimated=cost_estimated,
            )
        return RoleResult(
            ok=True,
            final_message=final or "(done)",
            cost_usd=cost,
            model=model or req.model,
            tokens=tokens,
            cost_estimated=cost_estimated,  # CLI 미보고면 추정치
        )
