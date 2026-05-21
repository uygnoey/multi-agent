"""`.claude/agents/*.md` 파서 → 역할 시스템 프롬프트/툴/모델.

frontmatter 가 단순(name/description/tools/model)하므로 pyyaml 없이도 동작하는
경량 파서를 쓴다. pyyaml 이 설치돼 있으면 그것을 우선 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import AGENTS_DIR, DEV_TOOLS


@dataclass
class AgentDef:
    name: str
    description: str
    tools: list[str]
    model: str | None
    system_prompt: str


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    fm = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    return fm, body


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
        return AgentDef(role, role, list(DEV_TOOLS), None, f"You are the {role} agent.")
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    meta = _parse_meta(fm)
    tools = _as_tools(meta.get("tools"))
    model = meta.get("model") or None
    return AgentDef(
        name=str(meta.get("name", role)),
        description=str(meta.get("description", "")),
        tools=tools,
        model=str(model) if model else None,
        system_prompt=body.strip(),
    )
