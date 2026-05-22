"""② OpenAI Agents SDK 백엔드 (API 키 방식).

pip install openai-agents / 인증 OPENAI_API_KEY.
내장 파일/배시 툴이 없으므로 타깃 cwd 로 스코프 한정한 function_tool 을 직접 제공한다.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
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


def _extract_io_tokens(result) -> tuple[int, int] | None:
    """#8: input/output 토큰을 분리 추출 (비용 추정용). 합산만 가능하면 None.

    가격이 input/output 단가가 다르므로, 정직한 추정을 위해 분리값이 있을 때만 추정한다.
    """
    # ① context_wrapper.usage
    try:
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        it = getattr(usage, "input_tokens", None)
        ot = getattr(usage, "output_tokens", None)
        if it is not None or ot is not None:
            return int(it or 0), int(ot or 0)
    except Exception:
        pass
    # ② raw_responses[*].usage 합산 (input/output 별도 누적)
    try:
        in_t = 0
        out_t = 0
        found = False
        for resp in getattr(result, "raw_responses", None) or []:
            u = getattr(resp, "usage", None)
            if u is None:
                continue
            i = getattr(u, "input_tokens", None)
            o = getattr(u, "output_tokens", None)
            if i is None and o is None:
                continue
            found = True
            in_t += int(i or 0)
            out_t += int(o or 0)
        if found:
            return in_t, out_t
    except Exception:
        pass
    return None


# #8: OpenAI 모델 단가(1M 토큰당 USD): [input, output]. CLI 백엔드처럼 토큰×단가로
# 비용을 추정해 대시보드/리포트가 OpenAI 사용량을 0 으로 과소표시하지 않게 한다.
# 가격 변동 대응: 환경변수 OPENAI_PRICING_FILE 로 외부 JSON 을 지정할 수 있다.
# 모델이 표에 없으면 추정하지 않고 cost=None 으로 둔다(허위 비용 날조 금지).
_OPENAI_FALLBACK_PRICING = {
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o4-mini": (1.1, 4.4),
}


def _openai_pricing() -> dict:
    """OPENAI_PRICING_FILE(있으면) → 코드 fallback. 값은 [input, output] (1M 토큰당 USD)."""
    import json

    path = os.environ.get("OPENAI_PRICING_FILE")
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            out = {}
            for k, v in data.items():
                if k.startswith("_") or not isinstance(v, (list, tuple)) or len(v) < 2:
                    continue
                out[k] = (float(v[0]), float(v[1]))
            if out:
                return out
        except Exception:
            pass
    return dict(_OPENAI_FALLBACK_PRICING)


def _estimate_openai_cost(model: str | None, in_tokens: int, out_tokens: int):
    """#8: 모델 단가 × 토큰으로 추정 비용(USD). 모델 미지정/단가표 미등록이면 None.

    정확매칭만 인정한다(임의 prefix 매칭은 알 수 없는 변형을 오과금할 수 있음).
    """
    m = (model or "").lower().strip()
    if not m:
        return None
    p = _openai_pricing().get(m)
    if not p:
        return None
    in_price, out_price = p
    cost = (in_tokens or 0) / 1e6 * in_price + (out_tokens or 0) / 1e6 * out_price
    return round(cost, 6)


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

    @staticmethod
    def _kill_proc(proc) -> None:
        # #36: 타임아웃/예외 시 자식 셸을 확실히 정리한다 (좀비/고아 방지).
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except Exception:
            pass

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
            # #35: 거대 파일을 통째로 메모리에 올린 뒤 자르면 200KB 상한이 메모리를 못 막는다.
            # 바이트 단위로 max_read_bytes+1 까지만 읽어, 초과 여부를 비싸지 않게 판정한다.
            try:
                with open(p, "rb") as fh:
                    raw = fh.read(max_read_bytes + 1)
            except Exception as e:
                return f"<read error: {path}: {e}>"
            truncated = len(raw) > max_read_bytes
            data = raw[:max_read_bytes].decode("utf-8", errors="replace")
            if truncated:
                return data + f"\n<... truncated: {path} exceeds {max_read_bytes} bytes>"
            return data

        @function_tool
        def write_file(path: str, content: str) -> str:
            """파일을 생성/'전체 덮어쓰기'한다 (타깃 디렉터리 한정, 5MB 초과 거부).

            주의(#16/#95): 이 백엔드의 Edit 툴도 이 함수에 매핑되어 부분 패치가 아닌 전체
            파일 덮어쓰기로 동작한다 — in-process function_tool 만으로는 true patch tool 을
            안전히 구현하기 어려운 근본 제약(KEEP-DOCUMENTED). 에이전트는 Edit 도 전체
            덮어쓰기로 인지하고 항상 전체 내용을 보내야 한다.
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
        # #36: 반환 텍스트는 어차피 4000자로 자르므로, 파이프에서 읽어 보관하는 양도 상한선으로
        # 묶는다(메모리 폭주 방지). capture_output=True 는 출력을 통째로 버퍼링하므로 쓰지 않고,
        # PIPE 에서 상한까지만 읽고 초과분은 흘려보낸 뒤 프로세스를 정리한다.
        max_bash_capture = 64 * 1024  # 보관 상한(~64KB) — 4000자 절단보다 넉넉

        @function_tool
        def run_bash(command: str) -> str:
            """셸 명령 실행 (cwd=타깃).

            주의(#1/#16): shell=True 이며 cwd 만 설정한다. 셸 자체는 FS 경계를 강제하지 않으므로
            절대경로/상위참조로 타깃 밖 파일 접근이 가능하다 — 진짜 격리는 in-process 로 불가능한
            근본 제약(KEEP-DOCUMENTED; 진짜 샌드박스는 컨테이너/OS 레벨에서만 가능).
            노출 자체는 역할의 allowed_tools(Bash 포함 여부)로만 제어된다.
            """
            proc = None
            try:
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    cwd=str(root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # stdout+stderr 를 한 스트림으로 합쳐 순서 보존
                    text=True,
                )
                buf: list[str] = []
                size = 0
                truncated = False
                deadline = time.monotonic() + bash_timeout
                # #36: 상한(max_bash_capture)까지만 보관하고, 초과분은 읽어서 버린다
                # (파이프 버퍼가 가득 차 자식이 블록되지 않도록 계속 소비한다).
                assert proc.stdout is not None
                for line in proc.stdout:
                    if time.monotonic() > deadline:
                        raise subprocess.TimeoutExpired(command, bash_timeout)
                    if size < max_bash_capture:
                        take = line[: max_bash_capture - size]
                        buf.append(take)
                        size += len(take)
                        if len(take) < len(line):
                            truncated = True
                    else:
                        truncated = True
                # #36: 예전엔 subprocess.run() 의 r.returncode 를 썼으나, 출력 버퍼링을 피하려
                # Popen 스트리밍으로 바꿨다. exit code 는 proc.wait() 의 rc 로 동일하게 얻는다.
                rc = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
                # #124: exit code 를 출력에 포함해 에이전트가 실패를 성공으로 오인하지 않게 한다.
                body = "".join(buf)[:4000]
                note = "\n<... output truncated>" if truncated and len(body) >= 4000 else ""
                return f"[exit {rc}]\n{body}{note}"
            except subprocess.TimeoutExpired:
                self._kill_proc(proc)
                return f"<bash error: timed out after {bash_timeout}s>"
            except Exception as e:
                self._kill_proc(proc)
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
        # #22(#114): OpenAI Agents SDK 에는 per-run 예산 캡 옵션이 없어 req.budget 강제는 미지원
        # — 검증 결과 SDK 에 해당 인자가 없는 근본 제약(KEEP-DOCUMENTED). max_turns 만 호출
        # 안전장치로 전달한다(SDK 가 지원). 누적 예산은 상위 runner 에서 사전 체크로 처리한다.
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
        # #8: input/output 토큰이 분리되고 모델 단가를 알면 비용을 추정한다(추정치 표시).
        cost = None
        cost_estimated = False
        io = _extract_io_tokens(result)
        if io is not None:
            est = _estimate_openai_cost(model, io[0], io[1])
            if est is not None:
                cost = est
                cost_estimated = True  # 토큰×단가 추정치 — 실청구액 아님
        return RoleResult(
            ok=True,
            final_message=str(getattr(result, "final_output", "")),
            model=model,
            tokens=tokens,
            cost_usd=cost,
            cost_estimated=cost_estimated,
        )
