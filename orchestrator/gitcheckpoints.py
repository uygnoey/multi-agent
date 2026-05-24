"""Git checkpoint commits for generated target projects."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

_FALLBACK_NAME = "dev-crew-orchestrator"
_FALLBACK_EMAIL = "dev-crew-orchestrator@brillianttiger.io"


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
        return subprocess.run(
            ["git", "-C", str(self.project_dir), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=env,
        )

    def _status_paths_best_effort(self) -> set[str]:
        if not self.enabled or not self.project_dir.exists():
            return set()
        try:
            status = self._run("status", "--porcelain", "--untracked-files=all")
        except Exception:
            return set()
        if status.returncode != 0:
            return set()
        return _parse_status_paths(status.stdout)

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
        status = self._run("status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0:
            raise RuntimeError((status.stderr or status.stdout or "git status failed").strip())
        changed = _parse_status_paths(status.stdout) - self._baseline_paths
        selected = _normalize_paths(paths)
        if selected is not None:
            changed = {p for p in changed if any(p == s or p.startswith(s + "/") for s in selected)}
        return sorted(changed)

    def _has_staged_changes(self) -> bool:
        diff = self._run("diff", "--cached", "--quiet")
        if diff.returncode == 0:
            return False
        if diff.returncode == 1:
            return True
        raise RuntimeError((diff.stderr or diff.stdout or "git diff --cached failed").strip())

    def _reset_paths(self, paths: list[str]) -> None:
        for chunk in _chunks(paths, 100):
            self._run("reset", "-q", "--", *chunk)

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
        commit = self._run("commit", "-m", message, timeout=60.0)
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


def _parse_status_paths(text: str) -> set[str]:
    paths: set[str] = set()
    for raw in text.splitlines():
        if len(raw) < 4:
            continue
        path = raw[3:]
        if " -> " in path:
            old, new = path.split(" -> ", 1)
            if old:
                paths.add(old)
            if new:
                paths.add(new)
        elif path:
            paths.add(path)
    return paths


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
