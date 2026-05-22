"""타깃 프로젝트 스캐폴딩.

- 디렉터리 생성, .orchestrator/ 초기화
- .orchestrator/spec.md 는 오케스트레이터 내부 상태 → 항상 (재)기록 (#140)
- CLAUDE.md·AGENTS.md 는 생성 마커가 있으면(=우리가 만든 것) 새로 갱신, 없으면
  (=사용자가 직접 쓴 것) 보존한다 (#40 + #141)
- 사용자 작성 파일(.claude/agents/*.md)은 보존 (#12)
- 타깃 .gitignore 에 .orchestrator/ 시드
"""

from __future__ import annotations

from pathlib import Path

from .config import AGENTS_DIR, TEMPLATES_DIR

_GITIGNORE_SEED = ".orchestrator/\n__pycache__/\nnode_modules/\n.venv/\n*.db\n"

# 우리가 생성한 CLAUDE.md/AGENTS.md 임을 표시하는 마커 (#40).
# 이 마커가 있는 파일만 재실행 시 안전하게 (재)기록한다. 사용자가 직접 쓴 파일(마커 없음)은
# 덮어쓰지 않는다. HTML 주석이라 Markdown 렌더링에 보이지 않는다.
_GEN_MARKER = "<!-- orchestrator-generated -->"


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
        # 의도된 동작(#20): 기존 .claude/agents/*.md 는 절대 덮어쓰지 않아 사용자 편집을 보존한다.
        # 부작용으로 프레임워크의 역할정의(role-definition) 변경은 *이미 존재하는* 타깃에
        # 자동 전파되지 않는다. 최신 역할정의로 강제 갱신하려면 해당 파일(또는
        # .claude/agents/ 디렉터리)을 지운 뒤 다시 스캐폴딩하면 된다.
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
    # .orchestrator/spec.md 는 사용자 파일이 아니라 오케스트레이터 내부 상태다. 재사용 디렉터리에
    # 새 spec 을 돌리면 이전 값이 stale 해지므로 항상 현재 내용으로 (재)기록한다 (#140).
    (orch / "spec.md").write_text(spec_text, encoding="utf-8")

    stack_str = _fmt_stack(stack)
    for fname in ("CLAUDE.md", "AGENTS.md"):
        target = project_dir / fname
        tpl = TEMPLATES_DIR / fname
        base = tpl.read_text(encoding="utf-8") if tpl.exists() else f"# {fname}\n"
        # 생성 마커를 본문 맨 앞에 박아 두어 우리 생성물임을 표시한다 (#40).
        content = (
            _GEN_MARKER
            + "\n"
            + base.replace("{{STACK}}", stack_str).replace("{{SPEC_EXCERPT}}", spec_text[:1200])
        )
        # 기존 파일이 없거나(처음) 마커를 포함하면(우리가 만든 것) → 현재 내용으로 새로 갱신
        # (#141 refresh). 마커가 없으면(사용자가 직접 쓴 것) → 덮어쓰지 않고 보존 (#40).
        if target.exists():
            existing = target.read_text(encoding="utf-8", errors="replace")
            if _GEN_MARKER not in existing:
                print(f"[scaffold] {fname} 은 사용자 작성으로 판단되어 보존합니다 (덮어쓰지 않음)")
                continue
        target.write_text(content, encoding="utf-8")

    expose_team_agents(project_dir)

    gi = project_dir / ".gitignore"
    if gi.exists():
        cur = gi.read_text(encoding="utf-8")
        # 라인 단위로 실제 ignore 패턴을 확인 (주석/부분일치 오인 방지; #92)
        existing = {ln.strip() for ln in cur.splitlines()}
        # 시드 블록을 통째로 붙이면 이미 있는 패턴(node_modules/, .venv/ 등)이 중복된다 (#21).
        # 따라서 아직 없는 시드 패턴만 골라 추가한다(공백 무시·정확 라인 일치로 dedupe).
        # .orchestrator/ 는 시드에 포함되므로 이 과정에서 누락되지 않고 결국 ignore 된다.
        missing = [
            pat
            for pat in _GITIGNORE_SEED.splitlines()
            if pat.strip() and pat.strip() not in existing
        ]
        if missing:
            gi.write_text(cur.rstrip() + "\n" + "\n".join(missing) + "\n", encoding="utf-8")
    else:
        gi.write_text(_GITIGNORE_SEED, encoding="utf-8")
