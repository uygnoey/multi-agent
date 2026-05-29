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
from .fsutil import atomic_write_bytes, atomic_write_text

_GITIGNORE_SEED = ".orchestrator/\n__pycache__/\nnode_modules/\n.venv/\n*.db\n"

# 우리가 생성한 CLAUDE.md/AGENTS.md 임을 표시하는 마커 (#40).
# 이 마커가 있는 파일만 재실행 시 안전하게 (재)기록한다. 사용자가 직접 쓴 파일(마커 없음)은
# 덮어쓰지 않는다. HTML 주석이라 Markdown 렌더링에 보이지 않는다.
_GEN_MARKER = "<!-- orchestrator-generated -->"

# #audit19(C2): 타깃 .claude/agents/*.md 복사본에 붙이는 생성 마커. 이 마커가 있는 파일만
# 이후 스캐폴딩에서 최신 프레임워크 역할정의로 갱신한다(없으면 사용자 작성본으로 보고 보존).
_AGENT_GEN_MARKER = "<!-- orchestrator-generated role definition; regenerated on scaffold -->"


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
    """Reject symlink escapes before writing orchestrator-managed project files.

    위협 모델(#audit13): 이미 존재하는 symlink 컴포넌트로의 탈출은 거부한다. 다만 이 검사와
    이어지는 mkdir/write 사이에 *로컬* 공격자가 컴포넌트를 symlink 로 바꿔치기하는 TOCTOU
    레이스는 닫지 못한다. 본 도구는 단일 사용자 로컬 실행을 전제로 하므로(공격자가 이미 같은
    사용자 권한으로 로컬에 있어야 성립) 이 잔존 레이스를 수용한다. CI/멀티테넌트/불신 spec 을
    공유 환경에서 실행한다면 openat(O_NOFOLLOW) 기반 incremental mkdir 로 재작성해야 한다.
    """

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
        # #audit19(C2): 생성 마커 기반 갱신. 복사 시 본문 끝에 마커를 붙여 둔다. 이후 스캐폴딩에서
        #   - 없으면 새로 기록,
        #   - 마커가 있으면(=우리가 만든 복사본) 최신 프레임워크 정의로 *덮어써 갱신*
        #     (예: docs-writer 4언어 변경이 기존 프로젝트에도 전파),
        #   - 마커가 없으면(사용자 작성/편집본) 보존.
        # (마커는 본문 끝 HTML 주석이라 frontmatter/subagent 동작에 영향 없음. 프레임워크 원본
        #  AGENTS_DIR 파일에는 마커를 넣지 않으므로 load_agent 등 내부 사용엔 영향 없음.)
        content = bundled.rstrip("\n") + "\n\n" + _AGENT_GEN_MARKER + "\n"
        if dest.exists():
            try:
                cur = dest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _AGENT_GEN_MARKER not in cur:
                continue  # 사용자 작성/편집본 → 보존
            if cur == content:
                continue  # 이미 최신 → 재기록 불필요
        # #audit23: 비원자 write_text 는 크래시/ENOSPC 시 agent prompt 가 부분 파일로 남아
        # 다음 run 의 백엔드 호출 시 깨진 시스템 프롬프트로 실패할 수 있다. fsutil 원자 쓰기로 통일.
        atomic_write_text(dest, content)
        count += 1
    return count


# #feature: 증분 모드에서 기존 프로젝트 컨텍스트를 모을 때 건너뛸 디렉터리/핵심 파일.
_REPO_SKIP_DIRS = {
    ".git",
    ".orchestrator",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    ".turbo",
    ".mypy_cache",
}
_REPO_KEY_FILES = (
    "README.md",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "docker-compose.yml",
    "tsconfig.json",
    "go.mod",
    "Cargo.toml",
)
_REPO_MAX_FILES = 600
_REPO_MAX_KEY_BYTES = 4000


def gather_repo_context(project_dir: Path, max_files: int = _REPO_MAX_FILES) -> str:
    """기존 프로젝트의 파일 트리 + 핵심 파일 발췌를 bounded 문자열로 모은다 (#feature 증분 모드).

    아키텍트/개발자가 '무엇이 이미 있는지' 이해해 기존 코드를 재사용·편집하도록 프롬프트(스펙)에
    주입한다. .git/.orchestrator/node_modules 등 노이즈 디렉터리는 제외하고, 파일 수 상한으로
    캡한다(거대 트리 방어).
    """
    root = Path(project_dir)
    if not root.is_dir():
        return "(no existing project files)"
    paths: list[str] = []
    truncated = False
    try:
        walker = os.walk(root)
        for dirpath, dirs, files in walker:
            dirs[:] = sorted(d for d in dirs if d not in _REPO_SKIP_DIRS)
            for fn in sorted(files):
                if fn == ".DS_Store" or fn.endswith((".pyc", ".pyo")):
                    continue
                full = Path(dirpath) / fn
                try:
                    rel = full.relative_to(root).as_posix()
                except ValueError:
                    continue
                paths.append(rel)
                if len(paths) >= max_files:
                    truncated = True
                    break
            if truncated:
                break
    except OSError:
        pass
    tree = "\n".join(paths) if paths else "(empty project)"
    excerpts: list[str] = []
    for kf in _REPO_KEY_FILES:
        p = root / kf
        try:
            if not (p.is_file() and not p.is_symlink()):
                continue
            # #audit19(F3): 거대 key 파일(비정상 package.json/lockfile)을 read_text() 로 통째로
            # 올린 뒤 자르면 OOM 위험. 필요한 만큼만 읽는다.
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                txt = fh.read(_REPO_MAX_KEY_BYTES)
            excerpts.append(f"--- {kf} ---\n{txt}")
        except OSError:
            continue
    out = [f"## Existing project files ({len(paths)}{'+' if truncated else ''} paths)", tree]
    if excerpts:
        out += ["", "## Key file excerpts", *excerpts]
    return "\n".join(out)


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
    # #audit23: spec.md 의 비원자 write_text 는 크래시/ENOSPC 시 부분 spec 이 남아
    # 다음 run/리포트가 절단된 spec 을 신뢰하게 된다. fsutil 원자 쓰기로 통일.
    if spec_text and spec_text.strip():
        _guard_managed_path(
            project_dir, orch / "spec.md", allow_unsafe=allow_unsafe, label=".orchestrator/spec.md"
        )
        atomic_write_text(orch / "spec.md", spec_text)
    elif not (orch / "spec.md").exists():
        # #audit13: 빈/공백 spec 이고 기존 spec.md 도 없으면, 프롬프트가 지시하는
        # "전체 spec 은 .orchestrator/spec.md 에 있다" 포인터가 dangling 되지 않게
        # 플레이스홀더를 기록한다. 기존 spec.md 가 있으면 보존(재사용 디렉터리 보호, #140).
        _guard_managed_path(
            project_dir, orch / "spec.md", allow_unsafe=allow_unsafe, label=".orchestrator/spec.md"
        )
        atomic_write_text(
            orch / "spec.md",
            "(이 run 에는 spec 본문이 제공되지 않았습니다.)\n",
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
        # #audit23: CLAUDE.md/AGENTS.md 도 원자 쓰기 — 크래시 시 사용자 작성으로 오인될
        # 수 있는 부분 파일이 남지 않게 한다.
        atomic_write_text(target, content)

    expose_team_agents(project_dir)

    gi = project_dir / ".gitignore"
    _guard_managed_path(project_dir, gi, allow_unsafe=allow_unsafe, label=".gitignore")
    if gi.exists():
        # #audit16/#audit18(A4): 기존 .gitignore 를 *바이트*로 읽는다. 비교용으로만 디코드하고
        # 원본 바이트는 절대 다시 쓰지 않는다. (audit16 의 errors="replace" + write_text 는 crash 는
        # 막았지만 비-UTF8 바이트를 �(U+FFFD)로 치환해 사용자 .gitignore 를 손상시켰다.)
        raw = gi.read_bytes()
        cur = raw.decode("utf-8", errors="replace")
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
            # #audit23: 원본 바이트 보존 + 누락 시드만 추가. 비원자 'ab' append 는 크래시 시
            # 부분 라인이 남을 수 있어 atomic_write_bytes 로 read-modify-write 원자 교체.
            prefix = b"" if (not raw or raw.endswith(b"\n")) else b"\n"
            addition = prefix + ("\n".join(missing) + "\n").encode("utf-8")
            atomic_write_bytes(gi, raw + addition)
    else:
        atomic_write_text(gi, _GITIGNORE_SEED)
