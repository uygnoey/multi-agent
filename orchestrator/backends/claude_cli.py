"""③ Claude Code 구독(CLI) 백엔드.

claude -p ... --output-format stream-json (cwd=타깃에서 실행) → 생각/툴/응답이 실시간 스트리밍.
ANTHROPIC_API_KEY 미설정 시 로그인된 구독을 사용한다. cwd 의 CLAUDE.md 자동 로드.
"""

from __future__ import annotations

import json
import shutil

from .base import Backend, RoleRequest, RoleResult, run_subprocess


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
                # 전문 저장 (절단 없음)
                inp = json.dumps(c.get("input", {}), ensure_ascii=False)
                out.append(f"🔧 {c.get('name')} {inp}")
            elif ct == "thinking" and c.get("thinking"):
                out.append("💭 " + c["thinking"])
        return "\n".join(out) or None
    if t == "result":
        return f"✓ result (${o.get('total_cost_usd', 0)})"
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
            final = o.get("result", final)
            cost = o.get("total_cost_usd", cost)
            u = o.get("usage") or {}
            if u:
                tokens = (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
    return final, cost, model, tokens


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
            cmd += ["--model", req.model]
        try:
            rc, out, err, timed_out = await run_subprocess(
                cmd, str(req.cwd), req.timeout, req.live_log_path, line_render=claude_stream_line
            )
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        if timed_out:
            return RoleResult(ok=False, error=f"claude-cli timed out after {req.timeout}s")
        if rc != 0:
            return RoleResult(ok=False, error=err.decode(errors="replace")[:500] or f"exit {rc}")

        final, cost, model, tokens = parse_stream_result(out)
        return RoleResult(
            ok=True,
            final_message=final or "(done)",
            cost_usd=cost,
            model=model or req.model,
            tokens=tokens,
        )
