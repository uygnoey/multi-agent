"""Git checkpoint commits for generated target projects."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

_FALLBACK_NAME = "dev-crew-orchestrator"
_FALLBACK_EMAIL = "dev-crew-orchestrator@brillianttiger.io"
_DEFAULT_COMMIT_MESSAGE = "orchestrator: checkpoint"

# baseline 캡처 상한 (#audit16): 재사용 디렉터리가 거대해도 init 이 과도하게 느려지지 않게
# 파일 수/크기를 캡한다. 상한을 넘기면 best-effort 로 중단하며, 미기록 경로는 '신규'로 간주돼
# 체크포인트에 포함될 수 있다(이전 빈-baseline 동작과 동일한 보수적 폴백).
_BASELINE_MAX_FILES = 5000
_BASELINE_MAX_FILE_BYTES = 5 * 1024 * 1024
# git/orchestrator 메타·의존성 디렉터리는 baseline 해시에서 제외(walk 가속). 이들은 시드
# .gitignore 로 git status 에도 노출되지 않으므로 제외해도 체크포인트 정확도에 영향 없음.
_BASELINE_SKIP_DIRS = {".git", ".orchestrator", "node_modules", ".venv", "venv", "__pycache__"}

_log = logging.getLogger(__name__)


class _NestedRepoError(RuntimeError):
    """project_dir 가 부모 git repo 하위 → nested repo 회피·체크포인트 비활성화 (#audit16)."""


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
        # #audit16: 경로명 집합 대신 (상대경로 → content hash) baseline 을 캡처한다. 이래야
        # (a) 비-git 기존 디렉터리에서도 사용자 파일을 첫 체크포인트에 끌어들이지 않고,
        # (b) 시작 시 dirty 였던 파일을 orchestrator 가 다시 수정하면 그 변경분이 유실되지 않는다.
        self._baseline_hashes = self._capture_baseline_hashes()
        # #audit16: project_dir 가 부모 git repo 하위로 판명되면 nested repo 를 피하려고
        # 체크포인트를 끈 사유를 캐시한다(반복 rev-parse/로그 방지).
        self._checkpoints_disabled_reason: str | None = None

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
        # #audit18(A2): GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE 가 호출 환경(git hook·래퍼 등)에서
        # 설정돼 있으면 `git -C project_dir` 가 그 env 의 *다른* repo 에서 동작해 무관 저장소를
        # 오염시키고 nested-repo 가드(rev-parse --show-toplevel)까지 무력화한다. -C 는 이 env 들을
        # 덮지 못하므로 명시적으로 제거한다. GIT_CONFIG_NOSYSTEM=1 로 시스템 config 영향도 차단.
        for _var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
            env.pop(_var, None)
        env.setdefault("GIT_CONFIG_NOSYSTEM", "1")
        # #audit19(F4): 사용자 전역 ~/.gitconfig 의 commit.gpgsign=true(키 없음)/core.hooksPath/
        # include 등이 체크포인트 커밋을 깨거나 변형하지 않도록 전역 config 도 차단한다. 신원은
        # _ensure_identity 가 env(GIT_AUTHOR_*)나 --local 로 명시 주입하므로 영향 없음.
        env["GIT_CONFIG_GLOBAL"] = os.devnull
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

    def _capture_baseline_hashes(self) -> dict[str, str]:
        """run 시작 전(scaffold 이전) 존재하던 파일들의 (상대경로 → content hash) (#audit16).

        체크포인트에서 'orchestrator 가 만들거나 수정한 변경'만 커밋하기 위한 기준이다. 파일 수/
        크기 상한을 둬 거대한 재사용 디렉터리에서 init 이 느려지지 않게 한다(best-effort).
        """
        out: dict[str, str] = {}
        if not self.enabled or not self.project_dir.exists():
            return out
        count = 0
        try:
            walker = os.walk(self.project_dir, onerror=None)
            for root, dirs, files in walker:
                dirs[:] = [d for d in dirs if d not in _BASELINE_SKIP_DIRS]
                for fn in files:
                    full = Path(root) / fn
                    try:
                        rel = full.relative_to(self.project_dir).as_posix()
                    except ValueError:
                        continue
                    h = self._hash_file(full)
                    if h is None:
                        continue
                    out[rel] = h
                    count += 1
                    if count >= _BASELINE_MAX_FILES:
                        _log.warning(
                            "git checkpoint baseline truncated at %d files; some pre-existing "
                            "files may be included in checkpoints",
                            _BASELINE_MAX_FILES,
                        )
                        return out
        except OSError as e:
            _log.warning("git checkpoint baseline capture failed: %s", e)
        return out

    @staticmethod
    def _hash_file(full: Path) -> str | None:
        """파일 내용 서명. symlink/비정규파일/접근불가는 None. 과대 파일은 (크기,mtime) 서명."""
        try:
            if full.is_symlink() or not full.is_file():
                return None
            st = full.stat()
        except OSError:
            return None
        if st.st_size > _BASELINE_MAX_FILE_BYTES:
            # 과대 파일은 내용 대신 (크기,mtime) 서명 — 변경되지 않은 거대 사용자 파일이
            # 매번 커밋되지 않게 하되, orchestrator 수정 시 mtime/크기 변화로 변경을 잡는다.
            return f"meta:{st.st_size}:{int(st.st_mtime)}"
        try:
            data = full.read_bytes()
        except OSError:
            return None
        return "sha1:" + hashlib.sha1(data).hexdigest()

    def _is_orchestrator_change(self, rel: str) -> bool:
        """rel 이 orchestrator 의 변경(=체크포인트 대상)인지 (#audit16).

        baseline 에 없던 신규 경로·내용이 달라진 경로·사라진(삭제) 경로는 True.
        baseline 과 동일한 사용자 기존파일만 False(제외).
        """
        base = self._baseline_hashes.get(rel)
        if base is None:
            return True  # 신규 파일
        cur = self._hash_file(self.project_dir / rel)
        if cur is None:
            return True  # 삭제됨/접근불가 → 변경으로 본다(삭제 stage 필요)
        return cur != base

    def _ensure_repo(self) -> None:
        if not shutil.which("git"):
            raise RuntimeError("git executable not found")
        self.project_dir.mkdir(parents=True, exist_ok=True)
        top = self._run("rev-parse", "--show-toplevel")
        if top.returncode == 0:
            try:
                top_path: Path | None = Path(top.stdout.strip()).resolve()
            except Exception:
                top_path = None
            if top_path is not None and top_path == self.project_dir.resolve():
                self._ensure_identity()
                return
            if top_path is not None:
                # #audit16: project_dir 가 기존(부모) git repo 하위다. 여기서 git init 하면
                # 혼란스러운 nested repo 가 생기고, 부모 repo 에 커밋하면 사용자 저장소를
                # 오염시킨다. 둘 다 피하고 체크포인트를 끈다(best-effort 기능이므로 degrade).
                raise _NestedRepoError(str(top_path))
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
        # #audit16: 경로명 차집합 대신 내용 해시 기반 판정(_is_orchestrator_change)으로 거른다.
        # baseline 과 동일한 사용자 기존파일만 제외하고, 신규/수정/삭제는 모두 포함한다.
        all_changed = {
            p for p in _parse_status_paths_z(status.stdout) if self._is_orchestrator_change(p)
        }
        # #RA-git: rename 짝(R/C) 과 tracked-삭제 경로를 미리 뽑아 둔다(필터링 시 짝을 함께 끌어옴).
        rename_pairs = [
            (o, n)
            for (o, n) in _parse_rename_pairs_z(status.stdout)
            if self._is_orchestrator_change(o) or self._is_orchestrator_change(n)
        ]
        deleted = {
            p for p in _parse_deleted_paths_z(status.stdout) if self._is_orchestrator_change(p)
        }
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
        # #audit16: nested-repo 회피로 한 번 비활성화되면 이후 호출은 즉시 skip 사유 반환.
        if self._checkpoints_disabled_reason:
            return False, self._checkpoints_disabled_reason
        try:
            self._ensure_repo()
        except _NestedRepoError as e:
            self._checkpoints_disabled_reason = (
                f"skipped: project dir is inside an existing git repo ({e}); "
                "nested checkpoint repo avoided"
            )
            _log.warning(self._checkpoints_disabled_reason)
            return False, self._checkpoints_disabled_reason
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
