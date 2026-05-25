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


# (#audit9-11) 루트 바로 아래의 시스템 디렉터리 블랙리스트(이름 단위). 절대경로가 정확히
# <anchor>/<name> 형태(루트 한 단계 아래)일 때만 거부한다 → /etc/myproj 같은 하위는 정상 허용.
# POSIX·Windows 모두 포함하는 합리적 교집합. ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 로 우회 가능.
_SYSTEM_DIR_NAMES = {
    "etc",
    "usr",
    "bin",
    "sbin",
    "lib",
    "lib64",
    "var",
    "opt",
    "boot",
    "dev",
    "proc",
    "sys",
    "run",
    "root",
    "system",  # macOS /System
    "library",  # macOS /Library
    "windows",  # Windows C:\Windows
    "program files",
    "program files (x86)",
    "programdata",
}


def _under_root(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _guard_managed_path(project_dir: Path, path: Path, *, allow_unsafe: bool, label: str) -> None:
    """Reject symlink escapes before writing orchestrator-managed project files."""

    if allow_unsafe:
        return
    root = project_dir.resolve()
    try:
        rel = path.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError(
            f"위험한 project_dir 거부: {label} 경로가 project_dir 밖입니다 ({path}). "
            "정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
        ) from exc

    cur = project_dir
    for part in rel.parts:
        cur = cur / part
        try:
            is_link = cur.is_symlink()
        except OSError as exc:
            raise ValueError(
                f"위험한 project_dir 거부: {cur} 의 심링크 여부를 확인할 수 없습니다 "
                f"({exc}). 안전을 확인할 수 없어 거부합니다. "
                "정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
            ) from exc
        if is_link:
            raise ValueError(
                f"위험한 project_dir 거부: {cur} 이(가) 심볼릭 링크입니다 "
                f"({label} 외부 쓰기 방지). "
                "정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
            )
        if cur.exists() and not _under_root(cur, root):
            raise ValueError(
                f"위험한 project_dir 거부: {label} 실경로가 project_dir 밖을 가리킵니다 "
                f"({cur.resolve()}). 정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
            )


def _fmt_stack(stack: dict) -> str:
    # (#audit9-13) stack 이 None/비-dict 여도 죽지 않게 방어.
    if not isinstance(stack, dict):
        return ""
    return ", ".join(f"{k}={v}" for k, v in stack.items())


def _render_template_once(template: str, values: dict[str, str]) -> str:
    """Replace known {{PLACEHOLDER}} tokens in one pass so replacements are not re-expanded."""

    def repl(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    rendered = re.sub(r"\{\{([A-Z_]+)\}\}", repl, template)
    # (#audit9-14) 치환되지 않고 남은 {{...}} 토큰을 조용히 흘리지 않고 경고로 표면화한다.
    # (대문자+언더스코어가 아닌 토큰이나 values 에 없는 키 등) 깨진 placeholder 를 알린다.
    leftovers = sorted(set(re.findall(r"\{\{([^}]+)\}\}", rendered)))
    if leftovers:
        print(
            "[scaffold] 미치환 템플릿 토큰이 남아 있습니다: "
            + ", ".join("{{" + name + "}}" for name in leftovers)
        )
    return rendered


def expose_team_agents(project_dir: Path) -> int:
    """Copy the framework's role definitions into the target as native subagents.

    This lets Claude-family backends (claude-cli / claude-sdk / claude-team) load them
    via the project's `.claude/agents/` and dispatch them with the Task tool.
    Returns the number of role files exposed.
    """
    if not AGENTS_DIR.exists():
        return 0
    project_dir = Path(project_dir).expanduser().resolve()
    allow_unsafe = os.environ.get("ORCH_ALLOW_UNSAFE_PROJECT_DIR") == "1"
    dest_dir = project_dir / ".claude" / "agents"
    _guard_managed_path(project_dir, dest_dir, allow_unsafe=allow_unsafe, label=".claude/agents")
    dest_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for md in sorted(AGENTS_DIR.glob("*.md")):
        # (#audit9-15) 인코딩 처리를 다른 곳(errors="replace")과 일치시키고, 개별 파일이
        # 읽히지 않으면 전체 스캐폴딩을 중단하지 않고 그 파일만 건너뛴다.
        try:
            bundled = md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"[scaffold] 역할 정의 읽기 실패로 건너뜀: {md.name} ({exc})")
            continue
        dest = dest_dir / md.name
        _guard_managed_path(project_dir, dest, allow_unsafe=allow_unsafe, label=".claude/agents")
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
    # 심링크 해소 전의 절대경로도 따로 둔다(#audit9-11): macOS 에서 /etc → /private/etc 처럼
    # resolve() 가 심링크를 따라가면 "루트 한 단계 아래" 패턴이 어긋나 시스템 디렉터리 탐지를
    # 놓친다. abspath(=정규화하되 심링크 미해소)로 사용자가 *지정한* 경로 형태를 보존해 검사한다.
    abs_unresolved = Path(os.path.abspath(Path(project_dir).expanduser()))
    project_dir = Path(project_dir).expanduser().resolve()
    allow_unsafe = os.environ.get("ORCH_ALLOW_UNSAFE_PROJECT_DIR") == "1"
    if not allow_unsafe:
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
        # (#audit9-11) 루트 바로 아래의 시스템 디렉터리(/etc, /usr, /System, /Library, C:\Windows)
        # 자체를 타깃으로 삼는 것도 거부한다. 정확히 <anchor>/<name>(루트 한 단계 아래)일 때만
        # 막아 /etc/myproj 같은 하위 디렉터리는 정상 타깃으로 허용한다. 심링크 미해소 경로
        # (abs_unresolved)로 검사해 macOS 의 /etc → /private/etc 재작성에도 안정적으로 잡는다.
        for candidate in {abs_unresolved, project_dir}:
            anchor = Path(candidate.anchor).resolve() if candidate.anchor else None
            if (
                anchor is not None
                and candidate.parent == Path(candidate.anchor)
                and candidate.name.lower() in _SYSTEM_DIR_NAMES
            ):
                raise ValueError(
                    f"위험한 project_dir 거부: {candidate} (시스템 디렉터리에는 스캐폴딩하지 "
                    "않습니다). 정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
                )

    project_dir.mkdir(parents=True, exist_ok=True)

    # (#audit9-12) 심볼릭 링크를 통한 탈출 방어: 스캐폴드 쓰기는 .orchestrator/ 및 그 하위로
    # 들어간다. 만약 project_dir/.orchestrator(또는 그 부모인 project_dir)가 project_dir 밖을
    # 가리키는 기존 심볼릭 링크라면, mkdir/write_text 가 링크를 따라가 외부(예: /etc)에 쓰게 된다.
    # 따라서 .orchestrator 와 project_dir 가 (a) 심링크가 아닌지, (b) 실경로가 여전히 project_dir
    # 안에 있는지 확인하고, 어긋나면 거부한다. allow_unsafe 면 이 방어도 건너뛴다(명시적 우회).
    if not allow_unsafe:
        orch_path = project_dir / ".orchestrator"
        for comp in (project_dir, orch_path):
            try:
                is_link = comp.is_symlink()
            except OSError as e:
                # #M12: is_symlink 자체가 실패하면(권한/경합) 심링크 여부를 확인할 수 없다.
                # fail-closed: 안전을 보장할 수 없으므로 조용히 통과시키지 않고 거부한다.
                raise ValueError(
                    f"위험한 project_dir 거부: {comp} 의 심링크 여부를 확인할 수 없습니다 "
                    f"({e}). 안전을 확인할 수 없어 거부합니다. "
                    "정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
                ) from e
            if is_link:
                raise ValueError(
                    f"위험한 project_dir 거부: {comp} 이(가) 심볼릭 링크입니다 "
                    "(심링크를 통한 외부 쓰기 방지). "
                    "정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
                )
        # .orchestrator 가 이미 존재하면 실경로가 project_dir 밖으로 새지 않는지 한 번 더 확인.
        if orch_path.exists():
            resolved_orch = orch_path.resolve()
            expected = project_dir / ".orchestrator"
            inside = resolved_orch == expected or project_dir in resolved_orch.parents
            if not inside:
                raise ValueError(
                    f"위험한 project_dir 거부: .orchestrator 실경로가 project_dir 밖을 "
                    f"가리킵니다 ({resolved_orch}). "
                    "정말 의도했다면 ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 을 설정하세요."
                )

    orch = project_dir / ".orchestrator"
    _guard_managed_path(project_dir, orch, allow_unsafe=allow_unsafe, label=".orchestrator")
    _guard_managed_path(
        project_dir, orch / "results", allow_unsafe=allow_unsafe, label=".orchestrator/results"
    )
    _guard_managed_path(
        project_dir, orch / "qa", allow_unsafe=allow_unsafe, label=".orchestrator/qa"
    )
    (orch / "results").mkdir(parents=True, exist_ok=True)
    (orch / "qa").mkdir(parents=True, exist_ok=True)
    # .orchestrator/spec.md 는 사용자 파일이 아니라 오케스트레이터 내부 상태다. 재사용 디렉터리에
    # 새 spec 을 돌리면 이전 값이 stale 해지므로 현재 내용으로 (재)기록한다 (#140).
    # 단, spec_text 가 비어있거나 공백뿐이면 기록을 건너뛴다: 빈 값으로 덮어쓰면 이전에 있던
    # 정상 spec 을 파괴하기 때문이다(재사용 디렉터리 보호). 기존 파일이 있으면 그대로 보존한다.
    if spec_text and spec_text.strip():
        _guard_managed_path(
            project_dir, orch / "spec.md", allow_unsafe=allow_unsafe, label=".orchestrator/spec.md"
        )
        (orch / "spec.md").write_text(spec_text, encoding="utf-8")
    elif not (orch / "spec.md").exists():
        # #audit13: 빈/공백 spec 이고 기존 spec.md 도 없으면, 프롬프트가 지시하는
        # "전체 spec 은 .orchestrator/spec.md 에 있다" 포인터가 dangling 되지 않게
        # 플레이스홀더를 기록한다. 기존 spec.md 가 있으면 보존(재사용 디렉터리 보호, #140).
        _guard_managed_path(
            project_dir, orch / "spec.md", allow_unsafe=allow_unsafe, label=".orchestrator/spec.md"
        )
        (orch / "spec.md").write_text(
            "(이 run 에는 spec 본문이 제공되지 않았습니다.)\n", encoding="utf-8"
        )

    stack_str = _fmt_stack(stack)
    for fname in ("CLAUDE.md", "AGENTS.md"):
        target = project_dir / fname
        _guard_managed_path(project_dir, target, allow_unsafe=allow_unsafe, label=fname)
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
                    # (#audit9-13) spec_text 가 None 여도 죽지 않게 안전화.
                    "SPEC_EXCERPT": (spec_text or "")[:1200],
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
    _guard_managed_path(project_dir, gi, allow_unsafe=allow_unsafe, label=".gitignore")
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
