"""Tests for orchestrator.workspace.scaffold and expose_team_agents."""

from __future__ import annotations

from pathlib import Path

from orchestrator.workspace import expose_team_agents, scaffold

STACK = {"frontend": "React/Vite", "backend": "FastAPI", "db": "SQLite"}


def test_scaffold_creates_expected_layout(tmp_path: Path):
    target = tmp_path / "proj"
    scaffold(target, "the spec body", STACK)

    assert (target / "CLAUDE.md").exists()
    assert (target / "AGENTS.md").exists()
    assert (target / ".gitignore").exists()
    assert (target / ".orchestrator").is_dir()
    assert (target / ".orchestrator" / "results").is_dir()
    assert (target / ".orchestrator" / "qa").is_dir()
    assert (target / ".orchestrator" / "spec.md").read_text(encoding="utf-8") == "the spec body"

    # 10 role definitions copied into the target's native subagent dir.
    agents = list((target / ".claude" / "agents").glob("*.md"))
    assert len(agents) == 10


def test_gitignore_seeds_orchestrator(tmp_path: Path):
    target = tmp_path / "proj"
    scaffold(target, "spec", STACK)
    gi = (target / ".gitignore").read_text(encoding="utf-8")
    assert ".orchestrator/" in gi


def test_scaffold_is_non_destructive_for_existing_claude_md(tmp_path: Path):
    target = tmp_path / "proj"
    target.mkdir()
    sentinel = "# my own CLAUDE.md, do not touch\n"
    (target / "CLAUDE.md").write_text(sentinel, encoding="utf-8")

    scaffold(target, "spec", STACK)

    # Pre-existing CLAUDE.md is preserved verbatim.
    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == sentinel
    # But AGENTS.md (absent before) is created.
    assert (target / "AGENTS.md").exists()


def test_scaffold_appends_to_existing_gitignore(tmp_path: Path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / ".gitignore").write_text("dist/\n", encoding="utf-8")
    scaffold(target, "spec", STACK)
    gi = (target / ".gitignore").read_text(encoding="utf-8")
    assert "dist/" in gi
    assert ".orchestrator/" in gi


def test_expose_team_agents_copies_ten_files(tmp_path: Path):
    target = tmp_path / "proj"
    target.mkdir()
    count = expose_team_agents(target)
    assert count == 10
    copied = list((target / ".claude" / "agents").glob("*.md"))
    assert len(copied) == 10
    # Content is a faithful copy of the framework definition.
    assert "backend-developer" in (
        target / ".claude" / "agents" / "backend-developer.md"
    ).read_text(encoding="utf-8")
