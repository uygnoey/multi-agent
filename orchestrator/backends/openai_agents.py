"""② OpenAI Agents SDK 백엔드 (API 키 방식).

pip install openai-agents / 인증 OPENAI_API_KEY.
내장 파일/배시 툴이 없으므로 타깃 cwd 로 스코프 한정한 function_tool 을 직접 제공한다.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from .base import Backend, RoleRequest, RoleResult


def _extract_tokens(result) -> int | None:
    """Runner 결과에서 total token 사용량을 best-effort 로 뽑는다 (SDK 버전 차이에 견고).

    구버전/신버전 모두 대응: ① context_wrapper.usage.total_tokens
    ② raw_responses[*].usage(input/output) 합산. 없으면 None.
    """
    # ① context_wrapper.usage.total_tokens (최신 SDK)
    try:
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        total = getattr(usage, "total_tokens", None)
        if total:
            return int(total)
        it = getattr(usage, "input_tokens", None)
        ot = getattr(usage, "output_tokens", None)
        if it is not None or ot is not None:
            return int((it or 0) + (ot or 0))
    except Exception:
        pass
    # ② raw_responses[*].usage 합산
    try:
        total = 0
        found = False
        for resp in getattr(result, "raw_responses", None) or []:
            u = getattr(resp, "usage", None)
            if u is None:
                continue
            found = True
            tt = getattr(u, "total_tokens", None)
            if tt:
                total += int(tt)
            else:
                total += int(getattr(u, "input_tokens", 0) or 0)
                total += int(getattr(u, "output_tokens", 0) or 0)
        if found:
            return total or None
    except Exception:
        pass
    return None


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

        # #122/#123: 툴 출력/파일 쓰기 크기 상한 (컨텍스트 폭주·거대 파일 생성 방지).
        max_read_bytes = 200 * 1024  # read 는 ~200KB 까지 (초과 시 절단 안내)
        max_write_bytes = 5 * 1024 * 1024  # write 는 5MB 초과를 거부

        @function_tool
        def read_file(path: str) -> str:
            """파일 내용을 읽는다 (타깃 디렉터리 한정, 최대 ~200KB — 초과분은 절단)."""
            p = _safe(path)
            if not p.exists():
                return f"<no file: {path}>"
            # #122: 거대 파일을 컨텍스트에 통째로 싣지 않도록 상한선까지만 읽는다.
            data = p.read_text(encoding="utf-8", errors="replace")
            if len(data.encode("utf-8")) > max_read_bytes:
                clipped = data.encode("utf-8")[:max_read_bytes].decode("utf-8", errors="ignore")
                return clipped + f"\n<... truncated: {path} exceeds {max_read_bytes} bytes>"
            return data

        @function_tool
        def write_file(path: str, content: str) -> str:
            """파일을 생성/'전체 덮어쓰기'한다 (타깃 디렉터리 한정, 5MB 초과 거부).

            주의(#95): 이 백엔드의 Edit 툴도 이 함수에 매핑되어 부분 패치가 아닌
            전체 파일 덮어쓰기로 동작한다 (true patch tool 은 미지원).
            """
            p = _safe(path)
            # #123: 비정상적으로 거대한 content 는 거부한다.
            if len(content.encode("utf-8")) > max_write_bytes:
                return f"<write rejected: content exceeds {max_write_bytes} bytes>"
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

        bash_timeout = req.timeout if req.timeout else 120  # config 의 세션 타임아웃을 따른다

        @function_tool
        def run_bash(command: str) -> str:
            """셸 명령 실행 (cwd=타깃).

            주의(#16): shell=True 이며 cwd 만 설정한다. 셸 자체는 FS 경계를 강제하지 않으므로
            절대경로/상위참조로 타깃 밖 파일 접근이 가능하다 — 진짜 격리는 in-process 로 불가.
            노출 자체는 역할의 allowed_tools(Bash 포함 여부)로만 제어된다.
            """
            try:
                r = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    timeout=bash_timeout,
                )
                # #124: exit code 를 출력에 포함해 에이전트가 실패를 성공으로 오인하지 않게 한다.
                body = (r.stdout + r.stderr)[:4000]
                return f"[exit {r.returncode}]\n{body}"
            except Exception as e:
                return f"<bash error: {e}>"

        # 역할의 allowed_tools 만 노출 (다른 백엔드의 --allowedTools 와 동일한 격리).
        tool_map = {
            "Read": [read_file, list_dir],
            "Write": [write_file],
            "Edit": [write_file],
            "Bash": [run_bash],
        }
        tools: list = []
        for t in req.allowed_tools or []:
            for fn in tool_map.get(t, []):
                if fn not in tools:
                    tools.append(fn)
        if not tools:  # 안전 폴백: 읽기/목록만 (bash 제외)
            tools = [read_file, list_dir]

        kwargs = dict(name=req.role, instructions=req.system_prompt, tools=tools)
        if req.model:
            kwargs["model"] = req.model
        # #114: OpenAI Agents SDK 에는 per-run 예산 캡 옵션이 없어 req.budget 강제는 미지원.
        # max_turns 만 호출 안전장치로 전달한다. 누적 예산은 상위에서 처리한다.
        agent = Agent(**kwargs)
        try:
            result = await asyncio.wait_for(
                Runner.run(agent, req.prompt, max_turns=req.max_turns), timeout=req.timeout
            )
        except asyncio.TimeoutError:
            return RoleResult(ok=False, error=f"openai-agents timed out after {req.timeout}s")
        except Exception as e:
            return RoleResult(ok=False, error=str(e))
        # #46: model/tokens/cost 를 best-effort 로 캡처 (Runner 결과 형태에 따라 guard).
        tokens = _extract_tokens(result)
        model = req.model
        return RoleResult(
            ok=True,
            final_message=str(getattr(result, "final_output", "")),
            model=model,
            tokens=tokens,
        )
