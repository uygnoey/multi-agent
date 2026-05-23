"""Git checkpoint commits for generated target projects."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path


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
        self._disabled_reason: str | None = None

    async def checkpoint(self, message: str) -> tuple[bool, str]:
        if not self.enabled:
            return False, "disabled"
        async with self._lock:
            if self._disabled_reason:
                return False, self._disabled_reason
            try:
                return await asyncio.to_thread(self._checkpoint_sync, message)
            except Exception as e:
                self._disabled_reason = str(e)
                return False, self._disabled_reason

    def _run(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.project_dir), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def _ensure_repo(self) -> None:
        if not shutil.which("git"):
            raise RuntimeError("git executable not found")
        self.project_dir.mkdir(parents=True, exist_ok=True)
        top = self._run("rev-parse", "--show-toplevel")
        if top.returncode == 0:
            try:
                if Path(top.stdout.strip()).resolve() == self.project_dir.resolve():
                    return
            except Exception:
                pass
        init = self._run("init")
        if init.returncode != 0:
            raise RuntimeError((init.stderr or init.stdout or "git init failed").strip())

    def _has_changes(self) -> bool:
        status = self._run("status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0:
            raise RuntimeError((status.stderr or status.stdout or "git status failed").strip())
        return bool(status.stdout.strip())

    def _checkpoint_sync(self, message: str) -> tuple[bool, str]:
        self._ensure_repo()
        if not self._has_changes():
            return False, "no changes"
        add = self._run("add", "-A")
        if add.returncode != 0:
            raise RuntimeError((add.stderr or add.stdout or "git add failed").strip())
        if not self._has_changes():
            return False, "no changes"
        commit = self._run("commit", "-m", message, timeout=60.0)
        if commit.returncode != 0:
            raise RuntimeError((commit.stderr or commit.stdout or "git commit failed").strip())
        rev = self._run("rev-parse", "--short", "HEAD")
        sha = rev.stdout.strip() if rev.returncode == 0 else "committed"
        return True, sha
