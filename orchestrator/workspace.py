"""타깃 프로젝트 스캐폴딩.

- 디렉터리 생성, .orchestrator/ 초기화
- spec.md·CLAUDE.md·AGENTS.md 를 현재 run 의 spec/stack 으로 항상 (재)기록 (#140/#141)
- 사용자 작성 파일(.claude/agents/*.md)은 보존 (#12)
- 타깃 .gitignore 에 .orchestrator/ 시드
"""

from __future__ import annotations

from pathlib import Path

from .config import AGENTS_DIR, TEMPLATES_DIR

_GITIGNORE_SEED = ".orchestrator/\n__pycache__/\nnode_modules/\n.venv/\n*.db\n"


def _fmt_stack(stack: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in stack.items())


def expose_team_agents(project_dir: Path) -> int:
    """Copy the framework's role definitions into the target as native subagents.

    This lets Claude-family backends (claude-cli / claude-sdk / claude-team) load them
    via the project's `.claude/agents/` and dispatch them with the Task tool.
    Returns the number of role files exposed.
    """
    if not AGENTS_DIR.exists():
        return 0
    dest_dir = Path(project_dir) / ".claude" / "agents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for md in sorted(AGENTS_DIR.glob("*.md")):
        bundled = md.read_text(encoding="utf-8")
        dest = dest_dir / md.name
        # 기존 파일은 건드리지 않음 (#12): 동일하면 재기록 불필요, 다르면 사용자 편집본 보존.
        # 없을 때만 새로 기록한다.
        if dest.exists():
            continue
        dest.write_text(bundled, encoding="utf-8")
        count += 1
    return count


def scaffold(project_dir: Path, spec_text: str, stack: dict) -> None:
    project_dir = Path(project_dir)
    project_dir.mkdir(parents=True, exist_ok=True)

    orch = project_dir / ".orchestrator"
    (orch / "results").mkdir(parents=True, exist_ok=True)
    (orch / "qa").mkdir(parents=True, exist_ok=True)
    # spec.md / CLAUDE.md / AGENTS.md 는 오케스트레이터가 현재 run 의 spec·stack 을 박아 넣는
    # 생성물이다. 재사용 디렉터리에 새 spec 을 돌리면 이전 값이 stale 해지므로 항상 현재
    # 내용으로 (재)기록한다 (#140/#141). 사용자가 직접 쓴 .claude/agents/*.md 만 보존(#12).
    (orch / "spec.md").write_text(spec_text, encoding="utf-8")

    stack_str = _fmt_stack(stack)
    for fname in ("CLAUDE.md", "AGENTS.md"):
        target = project_dir / fname
        tpl = TEMPLATES_DIR / fname
        base = tpl.read_text(encoding="utf-8") if tpl.exists() else f"# {fname}\n"
        target.write_text(
            base.replace("{{STACK}}", stack_str).replace("{{SPEC_EXCERPT}}", spec_text[:1200]),
            encoding="utf-8",
        )

    expose_team_agents(project_dir)

    gi = project_dir / ".gitignore"
    if gi.exists():
        cur = gi.read_text(encoding="utf-8")
        # 라인 단위로 실제 ignore 패턴을 확인 (주석/부분일치 오인 방지; #92)
        lines = {ln.strip() for ln in cur.splitlines()}
        if ".orchestrator/" not in lines:
            gi.write_text(cur.rstrip() + "\n" + _GITIGNORE_SEED, encoding="utf-8")
    else:
        gi.write_text(_GITIGNORE_SEED, encoding="utf-8")
