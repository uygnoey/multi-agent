"""Git checkpoint commits for generated target projects."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from pathlib import Path

_FALLBACK_NAME = "dev-crew-orchestrator"
_FALLBACK_EMAIL = "dev-crew-orchestrator@brillianttiger.io"
_DEFAULT_COMMIT_MESSAGE = "orchestrator: checkpoint"

_log = logging.getLogger(__name__)


class GitCheckpointer:
    """Create best-effort checkpoint commits in the target project.

    The orchestrator should not fail a build just because git is unavailable or
    a commit cannot be created. Callers receive ``(committed, detail)`` and can
    decide whether to log or warn.
    """

    def __init__(self, project_dir: Path, *, enabled: bool = True):
        self.project_dir = Path(project_dir)
        self.enabled = enabled
        self._lock = asyncio.Lock()
        self._baseline_paths = self._status_paths_best_effort()

    async def checkpoint(self, message: str, paths: list[str] | None = None) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        async with self._lock:
            try:
                return await asyncio.to_thread(self._checkpoint_sync, message, paths)
            except Exception as e:
                return False, str(e)

    def _run(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        cmd = ["git", "-C", str(self.project_dir), *args]
        try:
            return subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            # (#2) timeout 을 호출자마다 다르게(set() vs 전파) 처리하지 않도록 여기서 일관된
            # "실패한 CompletedProcess" 로 변환한다. returncode != 0 이므로 모든 호출자가 동일하게
            # 실패로 인식한다(RuntimeError 또는 best-effort set() 둘 다 정상 동작).
            _log.warning("git %s timed out after %.1fs", " ".join(args), timeout)
            return subprocess.CompletedProcess(
                cmd,
                returncode=124,  # GNU timeout 관례: 124 = timed out
                stdout=e.stdout or "" if isinstance(e.stdout, str) else "",
                stderr=f"git {' '.join(args)} timed out after {timeout}s",
            )

    def _status_paths_best_effort(self) -> set[str]:
        if not self.enabled or not self.project_dir.exists():
            return set()
        try:
            # (#1) -z 로 NUL 구분 출력을 받아 공백/한글/이스케이프 경로를 정확히 파싱한다.
            status = self._run("status", "--porcelain", "-z", "--untracked-files=all")
        except Exception as e:
            # (#5) baseline 캡처 실패를 완전히 삼키지 않고 사유를 기록한다.
            _log.warning("git status (baseline) failed: %s", e)
            return set()
        if status.returncode != 0:
            # (#5) 실패 사유를 남긴다(예: timeout/권한). baseline 이 비어도 조용하진 않게.
            _log.warning(
                "git status (baseline) returned %s: %s",
                status.returncode,
                (status.stderr or status.stdout or "").strip(),
            )
            return set()
        return _parse_status_paths_z(status.stdout)

    def _ensure_repo(self) -> None:
        if not shutil.which("git"):
            raise RuntimeError("git executable not found")
        self.project_dir.mkdir(parents=True, exist_ok=True)
        top = self._run("rev-parse", "--show-toplevel")
        if top.returncode == 0:
            try:
                if Path(top.stdout.strip()).resolve() == self.project_dir.resolve():
                    self._ensure_identity()
                    return
            except Exception:
                pass
        init = self._run("init")
        if init.returncode != 0:
            raise RuntimeError((init.stderr or init.stdout or "git init failed").strip())
        self._ensure_identity()

    def _ensure_identity(self) -> None:
        if _identity_from_env():
            return
        name = self._run("config", "--get", "user.name")
        email = self._run("config", "--get", "user.email")
        if (
            name.returncode == 0
            and name.stdout.strip()
            and email.returncode == 0
            and email.stdout.strip()
        ):
            return
        self._run("config", "--local", "user.name", _FALLBACK_NAME)
        self._run("config", "--local", "user.email", _FALLBACK_EMAIL)

    def _changed_paths(self, paths: list[str] | None = None) -> list[str]:
        # (#1) -z 로 NUL 구분 출력 → 공백/한글/이스케이프 경로도 정확히 파싱(한글 파일명 흔함).
        status = self._run("status", "--porcelain", "-z", "--untracked-files=all")
        if status.returncode != 0:
            raise RuntimeError((status.stderr or status.stdout or "git status failed").strip())
        all_changed = _parse_status_paths_z(status.stdout) - self._baseline_paths
        # #RA-git: rename 짝(R/C) 과 tracked-삭제 경로를 미리 뽑아 둔다(필터링 시 짝을 함께 끌어옴).
        rename_pairs = [
            (o, n)
            for (o, n) in _parse_rename_pairs_z(status.stdout)
            if o not in self._baseline_paths or n not in self._baseline_paths
        ]
        deleted = _parse_deleted_paths_z(status.stdout) - self._baseline_paths
        selected = _normalize_paths(paths)
        if selected is None:
            return sorted(all_changed)

        def _matches(p: str) -> bool:
            return any(p == s or p.startswith(s + "/") for s in selected)

        changed = {p for p in all_changed if _matches(p)}
        # #RA-git (a): staged rename(R/C) 은 origin·new 가 한 변경이다. 한쪽만 필터에 잡혀도
        # 양쪽을 함께 포함해야 체크포인트 커밋이 일관된다(old 가 tracked 인 채 남지 않게).
        for orig, new in rename_pairs:
            if _matches(orig) or _matches(new):
                changed.update({orig, new})
        # #RA-git (b): filesystem-rename(git mv 아닌 삭제+생성)은 ' D old'+'?? new' 로 따로 나온다.
        # paths=[new] 만 선택되면 old 삭제가 staging 누락되어 커밋이 일관성을 잃는다. 선택된 경로와
        # 같은 디렉터리에 있는 tracked-삭제 파일을 함께 포함해 삭제도 stage 되게 한다(한계: 디렉터리
        # 단위 휴리스틱 — 다른 디렉터리로의 fs-rename 은 git mv 사용 시에만 R 짝으로 정확히 잡힌다).
        # 루트 파일 선택(paths=["new.txt"])에서 dirname 이 ""가 되면 루트의 모든 tracked
        # deletion 이 같은 checkpoint 로 끌려오는 over-staging 이 된다. 디렉터리 휴리스틱은
        # "같은 비루트 디렉터리"에 한정하고, 루트-level fs rename 은 git mv/rename-pair 또는
        # 명시 selected deletion 으로만 처리한다(무관한 루트 삭제를 커밋하지 않는 쪽이 안전).
        sel_dirs = {s.rsplit("/", 1)[0] for s in selected if "/" in s}
        for d in deleted:
            ddir = d.rsplit("/", 1)[0] if "/" in d else ""
            if _matches(d) or ddir in sel_dirs:
                changed.add(d)
        return sorted(changed)

    def _has_staged_changes(self) -> bool:
        diff = self._run("diff", "--cached", "--quiet")
        if diff.returncode == 0:
            return False
        if diff.returncode == 1:
            return True
        raise RuntimeError((diff.stderr or diff.stdout or "git diff --cached failed").strip())

    def _reset_paths(self, paths: list[str]) -> None:
        # (#3) commit 실패 후 staging 롤백. reset 자체가 실패하면 staging 이 남아 다음
        # 체크포인트를 오염시킬 수 있으므로, 조용히 무시하지 않고 사유를 기록한다.
        for chunk in _chunks(paths, 100):
            reset = self._run("reset", "-q", "--", *chunk)
            if reset.returncode != 0:
                _log.warning(
                    "git reset (rollback) failed for %d path(s): %s",
                    len(chunk),
                    (reset.stderr or reset.stdout or "").strip(),
                )

    def _stageable_paths(self, paths: list[str]) -> list[str]:
        out: list[str] = []
        for p in paths:
            full = self.project_dir / p
            if full.exists():
                out.append(p)
                continue
            # 파일이 status 이후 사라졌더라도 tracked 파일이면 deletion stage 가 필요하다.
            # untracked 파일이 사라진 경우만 git add pathspec 에러를 피하려고 제외한다.
            tracked = self._run("ls-files", "--error-unmatch", "--", p)
            if tracked.returncode == 0:
                out.append(p)
        return out

    def _checkpoint_sync(self, message: str, paths: list[str] | None = None) -> tuple[bool, str]:
        self._ensure_repo()
        paths = self._stageable_paths(self._changed_paths(paths))
        if not paths:
            return False, "no changes"
        for chunk in _chunks(paths, 100):
            add = self._run("add", "-A", "--", *chunk)
            if add.returncode != 0:
                raise RuntimeError((add.stderr or add.stdout or "git add failed").strip())
        if not self._has_staged_changes():
            return False, "no changes"
        # (#4) 빈/공백뿐인 메시지는 git 에서 nondeterministic(에디터 호출/거부) → 기본값으로 대체.
        commit_message = message if (message and message.strip()) else _DEFAULT_COMMIT_MESSAGE
        commit = self._run("commit", "-m", commit_message, timeout=60.0)
        if commit.returncode != 0:
            self._reset_paths(paths)
            raise RuntimeError((commit.stderr or commit.stdout or "git commit failed").strip())
        rev = self._run("rev-parse", "--short", "HEAD")
        sha = rev.stdout.strip() if rev.returncode == 0 else "committed"
        return True, sha


def _identity_from_env() -> bool:
    keys = (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    )
    return all(os.environ.get(k) for k in keys)


def _parse_status_paths_z(text: str) -> set[str]:
    """`git status --porcelain -z` 의 NUL 구분 출력을 파싱한다 (#1).

    -z 모드는 각 엔트리를 NUL(\\0) 로 구분하고 경로를 인용/이스케이프하지 않으므로(원문 바이트
    그대로), 공백·한글·따옴표가 든 파일명도 손실 없이 처리된다. 일반 엔트리는
    'XY <path>\\0' 한 레코드지만, rename/copy(R/C) 는 'XY <new>\\0<orig>\\0' 처럼 *두 개*의 NUL
    레코드로 나뉜다(둘째 레코드 앞에 status 코드가 없다). 따라서 토큰을 순차 소비하며 R/C 일
    때만 다음 토큰을 origin 경로로 함께 추가한다.
    """
    paths: set[str] = set()
    # 마지막 NUL 뒤의 빈 토큰은 버린다. (#N04: 무의미한 리스트 컴프리헨션 제거)
    tokens = text.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    i = 0
    n = len(tokens)
    while i < n:
        rec = tokens[i]
        i += 1
        if len(rec) < 4:
            # 형식: 'XY <path>' — 'XY ' (3자) + 경로(>=1자). 너무 짧으면 손상 레코드로 보고 skip.
            continue
        xy = rec[:2]
        path = rec[3:]  # 'XY ' 다음부터가 경로(공백 포함 가능)
        if path:
            paths.add(path)
        # rename/copy 는 새 경로 레코드 다음 NUL 토큰에 원본 경로가 별도로 온다.
        if ("R" in xy or "C" in xy) and i < n:
            orig = tokens[i]
            i += 1
            if orig:
                paths.add(orig)
    return paths


def _iter_status_records_z(text: str):
    """`git status --porcelain -z` 레코드를 (xy, path, orig) 로 순차 산출 (#RA-git).

    _parse_status_paths_z 와 같은 토큰 소비 규칙을 쓰되, rename/copy(R/C) 의 origin 경로까지
    함께 돌려준다(일반 레코드는 orig=None). 손상/짧은 레코드는 skip.
    """
    tokens = text.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    i = 0
    n = len(tokens)
    while i < n:
        rec = tokens[i]
        i += 1
        if len(rec) < 4:
            continue
        xy = rec[:2]
        path = rec[3:]
        orig = None
        if ("R" in xy or "C" in xy) and i < n:
            orig = tokens[i]
            i += 1
        yield xy, path, orig


def _parse_rename_pairs_z(text: str) -> list[tuple[str, str]]:
    """rename/copy(R/C) 레코드의 (origin, new) 짝 목록 (#RA-git). 빈 경로는 제외."""
    pairs: list[tuple[str, str]] = []
    for _xy, path, orig in _iter_status_records_z(text):
        if orig and path:
            pairs.append((orig, path))
    return pairs


def _parse_deleted_paths_z(text: str) -> set[str]:
    """삭제된 tracked 경로 집합 (#RA-git). XY 어느 한쪽이 'D'(' D'/'D '/'AD' 등)면 삭제로 본다."""
    out: set[str] = set()
    for xy, path, _orig in _iter_status_records_z(text):
        if path and "D" in xy:
            out.add(path)
    return out


def _normalize_paths(paths: list[str] | None) -> set[str] | None:
    if paths is None:
        return None
    out: set[str] = set()
    for raw in paths:
        s = str(raw).strip().replace("\\", "/")
        if not s or s.startswith("/") or s.startswith("../") or "/../" in s or s == "..":
            continue
        out.add(s.strip("/"))
    return out


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]
