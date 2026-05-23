"""`.claude/agents/*.md` 파서 → 역할 시스템 프롬프트/툴/모델.

frontmatter 가 단순(name/description/tools/model)하므로 pyyaml 없이도 동작하는
경량 파서를 쓴다. pyyaml 이 설치돼 있으면 그것을 우선 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import AGENTS_DIR, DEV_TOOLS, ROLES


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


def _parse_meta(fm: str) -> dict:
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(fm) or {}
        if isinstance(data, dict):
            return {k: v for k, v in data.items()}
    except Exception:
        pass
    # 경량 폴백
    meta: dict = {}
    for line in fm.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta


def _as_tools(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return []


def load_agent(role: str) -> AgentDef:
    path = AGENTS_DIR / f"{role}.md"
    if not path.exists():
        # fail-open 방지: 정의 파일이 없을 때 무조건 DEV_TOOLS(Read·Write·Edit·Bash)를 주면
        # read-only 여야 할 supervisor(PM/PL = RO_TOOLS)가 쓰기/실행 권한을 얻는 권한 상승이 된다
        # (패키징 누락 등으로 .md 가 없을 때). 역할이 ROLES 에 정의돼 있으면 그 역할이 선언한
        # tools 를 폴백으로 쓰고(= supervisor 는 RO_TOOLS), 모르는 역할만 DEV_TOOLS 로 둔다.
        default_tools = list(ROLES[role].tools) if role in ROLES else list(DEV_TOOLS)
        return AgentDef(role, role, default_tools, None, f"You are the {role} agent.")
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    meta = _parse_meta(fm)
    tools = _as_tools(meta.get("tools"))
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
    s = str(value).strip()
    if not s or s.lower() == "inherit":
        return None
    return s
