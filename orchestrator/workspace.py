"""타깃 프로젝트 스캐폴딩 (비파괴적).

- 디렉터리 생성, .orchestrator/ 초기화
- templates/CLAUDE.md·AGENTS.md 를 타깃 루트에 기록 (이미 있으면 건드리지 않음)
- 타깃 .gitignore 에 .orchestrator/ 시드
"""
from __future__ import annotations

from pathlib import Path

from .config import TEMPLATES_DIR

_GITIGNORE_SEED = ".orchestrator/\n__pycache__/\nnode_modules/\n.venv/\n*.db\n"


def _fmt_stack(stack: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in stack.items())


def scaffold(project_dir: Path, spec_text: str, stack: dict) -> None:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    orch = project_dir / ".orchestrator"
    (orch / "results").mkdir(parents=True, exist_ok=True)
    (orch / "qa").mkdir(parents=True, exist_ok=True)
    (orch / "spec.md").write_text(spec_text, encoding="utf-8")

    stack_str = _fmt_stack(stack)
    for fname in ("CLAUDE.md", "AGENTS.md"):
        target = project_dir / fname
        if target.exists():
            continue  # 비파괴적
        tpl = TEMPLATES_DIR / fname
        base = tpl.read_text(encoding="utf-8") if tpl.exists() else f"# {fname}\n"
        target.write_text(
            base.replace("{{STACK}}", stack_str).replace("{{SPEC_EXCERPT}}", spec_text[:1200]),
            encoding="utf-8",
        )

    gi = project_dir / ".gitignore"
    if gi.exists():
        cur = gi.read_text(encoding="utf-8")
        if ".orchestrator/" not in cur:
            gi.write_text(cur.rstrip() + "\n" + _GITIGNORE_SEED, encoding="utf-8")
    else:
        gi.write_text(_GITIGNORE_SEED, encoding="utf-8")
