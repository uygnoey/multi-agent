"""`.claude/agents/*.md` 파서 → 역할 시스템 프롬프트/툴/모델.

frontmatter 가 단순(name/description/tools/model)하므로 pyyaml 없이도 동작하는
경량 파서를 쓴다. pyyaml 이 설치돼 있으면 그것을 우선 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import AGENTS_DIR, DEV_TOOLS, ROLES, normalize_role


@dataclass
class AgentDef:
    name: str
    description: str
    tools: list[str]
    model: str | None
    system_prompt: str


def _split_frontmatter(text: str) -> tuple[str, str]:
    # 첫 줄이 정확히 '---' 여야 frontmatter (----- 같은 구분선/수평선 오인 방지)
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return "", text
    # 닫는 펜스는 그 줄 자체가 정확히 '---'(뒤 공백 허용)인 줄이어야 한다 (#136).
    # '---extra' / '----' 같은 본문 줄을 닫는 마커로 오인하지 않도록 줄 단위로 찾는다.
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            fm = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :]).lstrip("\n")
            return fm, body
    return "", text


def _strip_quotes(s: str) -> str:
    """값 양끝의 짝맞는 따옴표(" 또는 ')를 한 겹 벗긴다 (#audit9-8)."""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _parse_scalar_value(v: str):
    """경량 폴백 값 파서: 인라인 리스트([a, b])는 list, 그 외는 따옴표 벗긴 문자열 (#audit9-8)."""
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        # 최소한의 인라인 리스트 처리: 콤마 분리 + 각 항목 따옴표 제거.
        inner = v[1:-1]
        return [_strip_quotes(item) for item in inner.split(",") if item.strip()]
    return _strip_quotes(v)


def _parse_meta(fm: str) -> dict:
    try:
        import yaml  # type: ignore
    except Exception:
        pass
    else:
        # (#audit9-7) yaml 파싱이 성공했으면 그 결과를 신뢰한다. dict 면 그대로, dict 가 아니면
        # (frontmatter 가 list/scalar 등) 깨끗한 빈 dict 를 돌려준다 — 경량 폴백으로 떨어지면
        # 같은 텍스트를 ':' 분리로 다시 긁어 garbage 메타를 만들기 때문이다.
        try:
            data = yaml.safe_load(fm)
        except Exception:
            data = None  # yaml 파싱 자체 실패 → 아래 경량 폴백 시도
        else:
            if data is None:
                return {}
            return {k: v for k, v in data.items()} if isinstance(data, dict) else {}
    # 경량 폴백 (pyyaml 미설치 또는 yaml 파싱 예외 시에만 도달)
    meta: dict = {}
    for line in fm.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = _parse_scalar_value(v)
    return meta


def _as_tools(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def load_agent(role: str) -> AgentDef:
    # (#audit9-6) 권한 상승 방지: 정규화 전 역할명(예: "pm")은 ROLES/AGENTS_DIR 어디에도
    # 매칭되지 않아 .md 가 없는 경로로 빠지고, 그러면 supervisor(PM/PL=RO_TOOLS)인데도
    # DEV_TOOLS(Write/Bash)가 폴백으로 부여되는 권한 상승이 된다. 시작 시 정식 역할명으로
    # 정규화해 supervisor 가 절대 쓰기/실행 권한을 얻지 못하게 한다.
    role = normalize_role(role)
    path = AGENTS_DIR / f"{role}.md"
    if not path.exists():
        # fail-open 방지: 정의 파일이 없을 때 무조건 DEV_TOOLS(Read·Write·Edit·Bash)를 주면
        # read-only 여야 할 supervisor(PM/PL = RO_TOOLS)가 쓰기/실행 권한을 얻는 권한 상승이 된다
        # (패키징 누락 등으로 .md 가 없을 때). 역할이 ROLES 에 정의돼 있으면 그 역할이 선언한
        # tools 를 폴백으로 쓰고(= supervisor 는 RO_TOOLS), 모르는 역할만 DEV_TOOLS 로 둔다.
        default_tools = list(ROLES[role].tools) if role in ROLES else list(DEV_TOOLS)
        return AgentDef(role, role, default_tools, None, f"You are the {role} agent.")
    # #RA-agread: 손상/비-UTF8 .md 가 UnicodeDecodeError 로 죽지 않게, workspace.expose_team_agents
    # 와 동일하게 errors="replace" 로 견고하게 읽는다.
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    meta = _parse_meta(fm)
    tools = _as_tools(meta.get("tools"))
    # #RA-tools: supervisor(PM/PL=RO_TOOLS) 같이 ROLES 가 선언한 역할은 .md frontmatter 가
    # 권한을 *추가* 하지 못하게 한다(변조/과대 .md 로 RO 역할이 Write/Bash 를 얻는 권한 상승 방지).
    # 효과적 tools = .md tools 와 ROLES[role].tools 의 교집합(순서/중복은 .md 기준). dev 역할은
    # 자신의 전체 tool 셋이 그대로 유지된다(교집합이 동일). 알 수 없는 역할만 .md 를 그대로 신뢰.
    if role in ROLES:
        allowed = set(ROLES[role].tools)
        tools = [t for t in tools if t in allowed]
    return AgentDef(
        name=str(meta.get("name", role)),
        description=str(meta.get("description", "")),
        tools=tools,
        model=_norm_model(meta.get("model")),
        system_prompt=body.strip(),
    )


def _norm_model(value) -> str | None:
    """frontmatter 의 model 값 정규화. 'inherit'(대소문자 무관)/빈값은 미지정(None) 처리 (#94).

    번들 에이전트가 model: inherit 를 쓰면 실제 모델명이 아니라 '미지정' 의미이므로
    백엔드/teammate 에 'inherit' 가 모델명으로 새어 들어가지 않게 한다.
    """
    if not value:
        return None
    # #L16: YAML 이 model 값을 숫자/불리언으로 파싱하면(model: 5 → int 5, model: true → bool)
    # str() 로 강제하면 "5"/"True" 같은 가짜 모델명이 새어 들어간다. 진짜 문자열만 수용하고
    # 비-문자열은 미지정(None) 처리한다.
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s or s.lower() == "inherit":
        return None
    return s
