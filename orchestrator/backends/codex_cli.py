"""④ Codex 구독(CLI) 백엔드.

codex exec ... --cd <타깃> --sandbox workspace-write --json -o <out> --skip-git-repo-check.
시스템 프롬프트 플래그가 없으므로 역할 프롬프트를 prompt 에 prepend 한다.
공유 지침은 타깃의 AGENTS.md 가 자동 로드한다. 인증: codex login 또는 CODEX_API_KEY.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from pathlib import Path

try:  # Python 3.11+ 에는 표준 tomllib 가 있다.
    import tomllib  # type: ignore
except ModuleNotFoundError:  # 3.10 이하 → 정규식 기반 안전 fallback 사용
    tomllib = None  # type: ignore

from .base import Backend, RoleRequest, RoleResult, run_subprocess


def _sanitize_key(key: str) -> str:
    """파일명 컴포넌트로 안전하게: 경로 구분자/.. 등을 제거하고 단어 문자만 남긴다.

    transcript 출력이 .orchestrator/results 밖으로 탈출하지 못하도록 하는 방어층.
    """
    s = str(key).strip()
    # 경로 구분자·상위참조 토큰 → 단어 문자 외 전부 '_' 치환
    s = re.sub(r"[^\w.-]", "_", s)
    # 선행 점/대시는 숨김파일·옵션 오인 방지 위해 제거, 빈 값은 'unit'
    s = s.lstrip(".-")
    return s or "unit"


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


def _root_model_from_text(text: str) -> str | None:
    """#143 fallback: 첫 [section] 헤더 이전(=root table)에 나오는 model 키만 인정한다.

    tomllib 가 없거나 파싱이 실패할 때 사용. 프로필/하위 섹션의 model 을
    전역 기본값으로 오인하지 않도록, 섹션 헤더를 만나면 더 이상 보지 않는다.
    """
    for raw in text.splitlines():
        s = raw.strip()
        if s.startswith("[") and "]" in s:
            # root table 종료 — 이후 등장하는 model 은 하위 섹션 소속이므로 무시
            break
        if s.startswith("model") and "=" in s and not s.startswith("model_"):
            key = s.split("=", 1)[0].strip()
            if key == "model":  # 'model = ...' 의 LHS 가 정확히 model 일 때만
                return s.split("=", 1)[1].strip().strip("\"'") or None
    return None


def _codex_default_model() -> str:
    """~/.codex/config.toml 의 최상위(root table) 기본 모델 (없으면 gpt-5.5).

    #143: 라인 단위 스캔은 [profiles.xxx] 같은 무관한 섹션의 model 키를 전역
    기본값으로 오인할 수 있다. 표준 tomllib(3.11+) 로 root table 의 model 만
    읽고, tomllib 미가용/파싱 실패 시 첫 섹션 이전의 model 만 인정한다.
    """
    try:
        cfg = Path.home() / ".codex" / "config.toml"
        if not cfg.exists():
            return "gpt-5.5"
        text = cfg.read_text(encoding="utf-8")
        if tomllib is not None:
            try:
                data = tomllib.loads(text)
                val = data.get("model")  # root table 의 model 키만
                if isinstance(val, str) and val.strip():
                    return val.strip()
                return "gpt-5.5"
            except Exception:
                pass  # 파싱 실패 → fallback 으로 진행
        val = _root_model_from_text(text)
        if val:
            return val
    except Exception:
        pass
    return "gpt-5.5"


# model 키 뒤에 붙는 날짜 스냅샷 접미사: '-2026', '-2026-05', '-2026-05-21' 등.
_DATE_SUFFIX = re.compile(r"^-\d{4}(?:-\d{2}){0,2}$")


def _price_for(model: str):
    """#144: 단순 startswith prefix 매칭은 알 수 없는 변형(예: gpt-5.5-pro)을
    더 짧은 키(gpt-5.5)로 오인 과금할 수 있다. ① 정확 매칭 우선, ② 날짜 접미사만
    붙은 dated 스냅샷(gpt-5.5-2026...)은 base 모델로 매핑, ③ 그 외는 None(미지정).
    """
    pricing = load_pricing()
    m = (model or "").lower().strip()
    if not m:
        return None
    # ① 정확 매칭 — 'gpt-5.5-pro' 는 'gpt-5.5' 가 아니라 'gpt-5.5-pro' 로만 매칭
    if m in pricing:
        return pricing[m]
    # ② 날짜 접미사 dated 스냅샷만 base 모델로 인정 (긴 키 우선: pro 가 base 보다 먼저)
    for key in sorted(pricing, key=len, reverse=True):
        if m.startswith(key) and _DATE_SUFFIX.match(m[len(key) :]):
            return pricing[key]
    # ③ 알 수 없는 변형(gpt-5.5-turbo, gpt-5.6 등) → 추정치 없음
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
        # #111: 바이너리 존재만 확인 — 로그인/인증은 검증하지 않는다(probe 회피).
        return True, "binary present (auth NOT verified: codex login 또는 CODEX_API_KEY)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        prompt = f"[SYSTEM ROLE INSTRUCTIONS]\n{req.system_prompt}\n\n[TASK]\n{req.prompt}"
        key = req.unit["id"] if req.unit else "global"
        # #108: unit id 는 board 에서 slug 처리되지만, 여기서도 방어적으로 경로 구분자를 제거해
        # codex transcript 가 .orchestrator/results 밖으로 탈출하지 못하도록 한다.
        safe_key = _sanitize_key(key)
        # 동시 codex 호출 간 충돌 방지: role+unit+고유 토큰으로 출력 파일 분리
        out_path = (
            req.cwd
            / ".orchestrator"
            / "results"
            / f"{req.role}__{safe_key}__{uuid.uuid4().hex}.codex.txt"
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
        # #23(#115)/#27(#119): codex exec 에는 per-call budget 플래그도 turn-limit 플래그도 없다
        # (근본 제약, KEEP-DOCUMENTED). req.budget / req.max_turns 강제는 이 백엔드에서 불가 —
        # 누적 예산은 상위 runner 가 사전 체크로 처리하고, 긴 세션은 timeout 으로만 통제된다.
        try:
            rc, out, err, timed_out = await run_subprocess(
                cmd, str(req.cwd), req.timeout, req.live_log_path
            )
        except Exception as e:
            return RoleResult(ok=False, error=str(e))

        if timed_out:
            return RoleResult(ok=False, error=f"codex timed out after {req.timeout}s")
        if rc != 0:
            # #6(#43): sandbox/login/runtime 진단의 '끝부분'(마지막 에러 컨텍스트)이 살아남도록
            # 예전의 head 절단(err[:4000])이 아니라 tail(마지막 4000자, err[-4000:])을 보존한다.
            return RoleResult(ok=False, error=err.decode(errors="replace")[-4000:] or f"exit {rc}")

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
