"""④ Codex 구독(CLI) 백엔드.

codex exec ... --cd <타깃> --sandbox workspace-write --json -o <out> --skip-git-repo-check.
시스템 프롬프트 플래그가 없으므로 역할 프롬프트를 prompt 에 prepend 한다.
공유 지침은 타깃의 AGENTS.md 가 자동 로드한다. 인증: codex login 또는 CODEX_API_KEY.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from .base import Backend, RoleRequest, RoleResult, run_subprocess

# 가격은 코드에 하드코딩하지 않고 설정 파일에서 로드한다(변동 대응). 파일이 없으면 아래 fallback.
_PRICING_FILE = Path(__file__).resolve().parent.parent / "codex_pricing.json"
_FALLBACK_PRICING = {
    "gpt-5.5-pro": (30.0, 30.0, 180.0),
    "gpt-5.5": (5.0, 0.5, 30.0),
    "gpt-5.4": (2.5, 0.25, 15.0),
    "gpt-5.4-mini": (0.75, 0.075, 4.5),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.3-codex": (1.75, 0.175, 14.0),
}


def load_pricing() -> dict:
    """모델별 단가 로드: ① $CODEX_PRICING_FILE ② orchestrator/codex_pricing.json ③ 코드 fallback.

    값은 [input, cached_input, output] (1M 토큰당 USD). '_' 로 시작하는 키는 주석으로 무시.
    """
    for path in (os.environ.get("CODEX_PRICING_FILE"), _PRICING_FILE):
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out = {}
            for k, v in data.items():
                if k.startswith("_") or not isinstance(v, (list, tuple)) or len(v) < 3:
                    continue
                out[k] = (float(v[0]), float(v[1]), float(v[2]))
            if out:
                return out
        except Exception:
            pass
    return dict(_FALLBACK_PRICING)


def _codex_default_model() -> str:
    """~/.codex/config.toml 의 기본 모델 (없으면 gpt-5.5)."""
    try:
        cfg = Path.home() / ".codex" / "config.toml"
        if cfg.exists():
            for line in cfg.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("model") and "=" in s and not s.startswith("model_"):
                    return s.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return "gpt-5.5"


def _price_for(model: str):
    pricing = load_pricing()
    m = (model or "").lower()
    for key in sorted(pricing, key=len, reverse=True):  # 긴 키 우선 매칭
        if m.startswith(key):
            return pricing[key]
    return None


def codex_cost(model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int):
    """토큰 usage × 모델 단가로 추정 비용(USD). 단가표에 없으면 None.

    uncached_input = input - cached. reasoning_output 은 output 에 포함된 것으로 본다(중복 계산 X).
    """
    p = _price_for(model)
    if not p:
        return None
    in_price, cached_price, out_price = p
    uncached = max(0, (input_tokens or 0) - (cached_input_tokens or 0))
    cost = (
        uncached / 1e6 * in_price
        + (cached_input_tokens or 0) / 1e6 * cached_price
        + (output_tokens or 0) / 1e6 * out_price
    )
    return round(cost, 6)


class CodexCLIBackend(Backend):
    name = "codex"

    def available(self) -> tuple[bool, str]:
        if not shutil.which("codex"):
            return False, "codex CLI 미설치 (npm i -g @openai/codex)"
        return True, "ready (codex login 또는 CODEX_API_KEY)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        prompt = f"[SYSTEM ROLE INSTRUCTIONS]\n{req.system_prompt}\n\n[TASK]\n{req.prompt}"
        key = req.unit["id"] if req.unit else "global"
        # 동시 codex 호출 간 충돌 방지: role+unit+고유 토큰으로 출력 파일 분리
        out_path = (
            req.cwd
            / ".orchestrator"
            / "results"
            / f"{req.role}__{key}__{uuid.uuid4().hex}.codex.txt"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "codex",
            "exec",
            prompt,
            "--cd",
            str(req.cwd),
            "--sandbox",
            "workspace-write",
            "--json",
            "-o",
            str(out_path),
            "--skip-git-repo-check",
        ]
        if req.model:
            cmd += ["--model", req.model]
        try:
            rc, out, err, timed_out = await run_subprocess(
                cmd, str(req.cwd), req.timeout, req.live_log_path
            )
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        if timed_out:
            return RoleResult(ok=False, error=f"codex timed out after {req.timeout}s")
        if rc != 0:
            return RoleResult(ok=False, error=err.decode(errors="replace")[:500] or f"exit {rc}")

        final = ""
        if out_path.exists():
            try:
                final = out_path.read_text(encoding="utf-8")[:2000]
            except Exception:
                pass
        # codex 는 USD 를 안 주므로 turn.completed 의 usage(토큰)로 비용을 추정 계산.
        usage = {}
        for line in out.splitlines():
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "turn.completed" and isinstance(o.get("usage"), dict):
                usage = o["usage"]
        model = req.model or _codex_default_model()
        inp = usage.get("input_tokens") or 0
        cached = usage.get("cached_input_tokens") or 0
        out_t = usage.get("output_tokens") or 0
        tokens = (inp + out_t) or None
        cost = codex_cost(model, inp, cached, out_t) if usage else None
        return RoleResult(
            ok=True,
            final_message=final or "codex exec ok",
            model=model,
            tokens=tokens,
            cost_usd=cost,
            cost_estimated=cost is not None,  # 가격표 기반 추정치 → est.
        )
