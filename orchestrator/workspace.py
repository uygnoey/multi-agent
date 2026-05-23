"""타깃 프로젝트 스캐폴딩.

- 디렉터리 생성, .orchestrator/ 초기화
- .orchestrator/spec.md 는 오케스트레이터 내부 상태 → 항상 (재)기록 (#140)
- CLAUDE.md·AGENTS.md 는 생성 마커가 있으면(=우리가 만든 것) 새로 갱신, 없으면
  (=사용자가 직접 쓴 것) 보존한다 (#40 + #141)
- 사용자 작성 파일(.claude/agents/*.md)은 보존 (#12)
- 타깃 .gitignore 에 .orchestrator/ 시드
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import AGENTS_DIR, FRAMEWORK_ROOT, TEMPLATES_DIR

_GITIGNORE_SEED = ".orchestrator/\n__pycache__/\nnode_modules/\n.venv/\n*.db\n"

# 우리가 생성한 CLAUDE.md/AGENTS.md 임을 표시하는 마커 (#40).
# 이 마커가 있는 파일만 재실행 시 안전하게 (재)기록한다. 사용자가 직접 쓴 파일(마커 없음)은
# 덮어쓰지 않는다. HTML 주석이라 Markdown 렌더링에 보이지 않는다.
_GEN_MARKER = "<!-- orchestrator-generated -->"


def _fmt_stack(stack: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in stack.items())


def _render_template_once(template: str, values: dict[str, str]) -> str:
    """Replace known {{PLACEHOLDER}} tokens in one pass so replacements are not re-expanded."""

    def repl(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return re.sub(r"\{\{([A-Z_]+)\}\}", repl, template)


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
    # 위험한 타깃 가드: scaffold 는 project_dir 에 mkdir(parents=True) 한 뒤 .orchestrator/,
    # .gitignore, CLAUDE.md, AGENTS.md, spec.md 를 기록한다. 사용자가 실수로 파일시스템
    # 루트(/), 홈 디렉터리(~) 자체, 또는 오케스트레이터 저장소 루트(FRAMEWORK_ROOT)를
    # 가리키면 기존 파일을 오염시킬 수 있으므로 거부한다. ~의 하위 디렉터리는 정상
    # 타깃이므로 허용한다(홈 자체만 거부). 의도적으로 위 경로에 스캐폴딩하려면 환경변수
    # ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 로 우회할 수 있다.
    project_dir = Path(project_dir).expanduser().resolve()
    if os.environ.get("ORCH_ALLOW_UNSAFE_PROJECT_DIR") != "1":
        unsafe = {
            Path(project_dir.anchor).resolve() if project_dir.anchor else None,
            Path.home().expanduser().resolve(),
            FRAMEWORK_ROOT.resolve(),
        }
        unsafe.discard(None)
        if project_dir in unsafe:
            raise ValueError(
                f"위험한 project_dir 거부: {project_dir} (파일시스템 루트·홈 디렉터리·"
                "프레임워크 저장소 루트에는 스캐폴딩하지 않습니다). "
                "정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
            )

    project_dir.mkdir(parents=True, exist_ok=True)

    orch = project_dir / ".orchestrator"
    (orch / "results").mkdir(parents=True, exist_ok=True)
    (orch / "qa").mkdir(parents=True, exist_ok=True)
    # .orchestrator/spec.md 는 사용자 파일이 아니라 오케스트레이터 내부 상태다. 재사용 디렉터리에
    # 새 spec 을 돌리면 이전 값이 stale 해지므로 현재 내용으로 (재)기록한다 (#140).
    # 단, spec_text 가 비어있거나 공백뿐이면 기록을 건너뛴다: 빈 값으로 덮어쓰면 이전에 있던
    # 정상 spec 을 파괴하기 때문이다(재사용 디렉터리 보호). 기존 파일이 있으면 그대로 보존한다.
    if spec_text and spec_text.strip():
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
            + _render_template_once(
                base,
                {
                    "STACK": stack_str,
                    "SPEC_EXCERPT": spec_text[:1200],
                },
            )
        )
        # 기존 파일이 없거나(처음) 마커를 포함하면(우리가 만든 것) → 현재 내용으로 새로 갱신
        # (#141 refresh). 마커가 없으면(사용자가 직접 쓴 것) → 덮어쓰지 않고 보존 (#40).
        if target.exists():
            existing = target.read_text(encoding="utf-8", errors="replace")
            # 마커가 본문 *맨 앞*에 있어야만 우리 생성물로 인정한다. 단순 부분일치(in)는
            # 마커 문자열을 본문 중간에 우연히 포함한 사용자 파일까지 덮어쓰는 오인을 부른다.
            if not existing.lstrip().startswith(_GEN_MARKER):
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
