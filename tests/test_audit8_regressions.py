"""Regression tests for audit7 follow-up fixes."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from orchestrator import webui
from orchestrator.backends import openai_agents as oa
from orchestrator.board import Board
from orchestrator.gitcheckpoints import GitCheckpointer


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_board_snapshot_and_agents_stringify_non_json_values(tmp_path: Path):
    b = Board(tmp_path)
    b._data["agents"] = {"role": {"bad": {"set-value"}}}
    b._data["bad"] = {"bytes": b"x"}

    assert b.snapshot()["bad"]["bytes"] == "b'x'"
    assert b.agents()["role"]["bad"] == "{'set-value'}"


def test_webui_malformed_origin_port_returns_403_not_500(tmp_path: Path):
    spawned = []

    def fake_spawn(cmd, log_path):
        spawned.append(cmd)

        class _P:
            pid = 44444

            def poll(self):
                return None

        return _P()

    manager = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, None))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        req = Request(
            f"http://127.0.0.1:{port}/api/run",
            data=b'{"spec_text":"# s","backend":"mock"}',
            headers={"Content-Type": "application/json", "Origin": "http://h:99999"},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(req, timeout=5)
        assert exc.value.code == 403
        assert not spawned
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_openai_file_helpers_allow_internal_symlink_and_reject_escape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "inside.txt"
    inside.write_text("hello", encoding="utf-8")
    internal = root / "internal-link"
    os.symlink(str(inside), str(internal))

    raw = oa._read_file_bytes_under_root(internal, root, 100)
    assert raw == b"hello"
    oa._write_file_bytes_under_root(internal, root, b"updated")
    assert inside.read_text(encoding="utf-8") == "updated"

    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    external = root / "external-link"
    os.symlink(str(outside), str(external))

    with pytest.raises(OSError):
        oa._read_file_bytes_under_root(external, root, 100)
    assert outside.read_text(encoding="utf-8") == "secret"


def test_git_checkpoint_uses_local_identity_and_preserves_baseline_dirty(
    tmp_path: Path, monkeypatch
):
    if not shutil.which("git"):
        pytest.skip("git executable not available")
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)

    project = tmp_path / "project"
    project.mkdir()
    assert _git(project, "init").returncode == 0
    (project / "preexisting.txt").write_text("user work", encoding="utf-8")

    cp = GitCheckpointer(project)
    (project / "generated.txt").write_text("generated", encoding="utf-8")

    committed, detail = asyncio.run(cp.checkpoint("orchestrator: test checkpoint"))

    assert committed, detail
    assert _git(project, "log", "--format=%s", "-1").stdout.strip() == (
        "orchestrator: test checkpoint"
    )
    assert _git(project, "config", "--get", "user.name").stdout.strip() == "dev-crew-orchestrator"
    assert (
        _git(project, "config", "--get", "user.email").stdout.strip()
        == "dev-crew-orchestrator@brillianttiger.io"
    )
    show = _git(project, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
    assert "generated.txt" in show
    assert "preexisting.txt" not in show
    status = _git(project, "status", "--porcelain", "--untracked-files=all").stdout
    assert "?? preexisting.txt" in status

    (project / "unit-a.txt").write_text("unit a", encoding="utf-8")
    (project / "unit-b-partial.txt").write_text("unit b partial", encoding="utf-8")
    committed, detail = asyncio.run(
        cp.checkpoint("orchestrator: scoped unit checkpoint", ["unit-a.txt"])
    )
    assert committed, detail
    show = _git(project, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
    assert "unit-a.txt" in show
    assert "unit-b-partial.txt" not in show
    status = _git(project, "status", "--porcelain", "--untracked-files=all").stdout
    assert "?? unit-b-partial.txt" in status
