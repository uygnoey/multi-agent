"""Tests for the orchestrator's incremental feature-addition mode (#feature 개발 지속성).

기존 프로젝트 위에 기능을 *추가*하는 모드: --feature 로 진입하며, 그린필드 재빌드가 아니라
기존 코드 컨텍스트를 아키텍트/개발자에게 주입해 델타 unit 만 계획·편집하게 한다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator import workspace
from orchestrator.__main__ import build_config, main, parse_args
from orchestrator.config import RunConfig
from orchestrator.scheduler import Scheduler


def _existing_project(tmp_path: Path) -> Path:
    proj = tmp_path / "existing-app"
    proj.mkdir()
    (proj / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (proj / "README.md").write_text("# Existing App\nA tiny calculator.\n", encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    # 노이즈 디렉터리(컨텍스트에서 제외돼야 함)
    (proj / "node_modules").mkdir()
    (proj / "node_modules" / "junk.js").write_text("x", encoding="utf-8")
    (proj / ".git").mkdir()
    (proj / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
    return proj


# ---------------------------------------------------------------------------
# gather_repo_context
# ---------------------------------------------------------------------------


def test_gather_repo_context_lists_files_and_excerpts(tmp_path):
    proj = _existing_project(tmp_path)
    ctx = workspace.gather_repo_context(proj)
    assert "app.py" in ctx
    assert "tests/test_app.py" in ctx
    # 핵심 파일 발췌
    assert "tiny calculator" in ctx
    # 노이즈 디렉터리는 제외
    assert "node_modules" not in ctx
    assert ".git/HEAD" not in ctx


def test_gather_repo_context_missing_dir():
    assert workspace.gather_repo_context(Path("/nonexistent/xyz")) == "(no existing project files)"


def test_gather_repo_context_file_cap(tmp_path):
    proj = tmp_path / "big"
    proj.mkdir()
    for i in range(50):
        (proj / f"f{i}.txt").write_text("x", encoding="utf-8")
    ctx = workspace.gather_repo_context(proj, max_files=10)
    assert "+ paths)" in ctx  # 상한 초과 표시 (truncated)


# ---------------------------------------------------------------------------
# _compose_feature_spec
# ---------------------------------------------------------------------------


def test_compose_feature_spec_includes_instruction_and_context(tmp_path):
    proj = _existing_project(tmp_path)
    cfg = RunConfig(
        spec_path=proj / ".orchestrator" / "spec.md",
        project_dir=proj,
        mock=True,
        feature="Add a subtract(a, b) function and a test",
    )
    spec = Scheduler(cfg)._compose_feature_spec()
    assert "INCREMENTAL FEATURE REQUEST" in spec
    assert "subtract" in spec  # 기능 요청
    assert "app.py" in spec  # 기존 파일 트리
    assert "tiny calculator" in spec  # README 발췌
    # 증분 핵심 지시가 앞부분(spec_excerpt 가 잡는 영역)에 있어야 한다
    assert "INCREMENTAL FEATURE REQUEST" in spec[:1500]
    # 핵심: 추가 개발 전에 'audit → fix → add feature' 순서를 강제(스펙은 AI-facing 영어).
    assert "Blank-slate audit" in spec
    assert "Required order of work" in spec
    audit_pos = spec.find("Blank-slate audit")
    feat_pos = spec.find("## Feature to add")
    assert 0 <= audit_pos < feat_pos  # 감사 지시가 기능 요청보다 앞에 온다


# ---------------------------------------------------------------------------
# build_config + CLI validation
# ---------------------------------------------------------------------------


def test_build_config_feature_mode_without_spec(tmp_path):
    proj = _existing_project(tmp_path)
    a = parse_args(["--feature", "add X", "--project-dir", str(proj), "--mock"])
    cfg = build_config(a)
    assert cfg.feature == "add X"
    # --spec 없으면 spec_path 는 project_dir/.orchestrator/spec.md placeholder
    assert cfg.spec_path == (proj.resolve() / ".orchestrator" / "spec.md")


def test_cli_feature_requires_project_dir():
    with pytest.raises(SystemExit):
        main(["--feature", "add X"])  # no --project-dir → reject before running


def test_cli_feature_rejects_missing_project_dir(tmp_path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit):
        main(["--feature", "add X", "--project-dir", str(missing)])


# ---------------------------------------------------------------------------
# end-to-end (mock): feature run on an existing project
# ---------------------------------------------------------------------------


def test_feature_mode_e2e_mock(tmp_path):
    proj = _existing_project(tmp_path)
    cfg = RunConfig(
        spec_path=proj / ".orchestrator" / "spec.md",
        project_dir=proj,
        mock=True,
        poll_interval=600.0,
        auto_commit=False,
        feature="Add a subtract(a, b) function and a test for it",
    )
    snap = asyncio.run(Scheduler(cfg).run())
    assert snap["phase"] == "done"
    assert len(snap.get("units", [])) >= 1
    # 합성된 증분 스펙이 .orchestrator/spec.md 로 기록됨 (에이전트가 Read 가능)
    spec_md = (proj / ".orchestrator" / "spec.md").read_text(encoding="utf-8")
    assert "INCREMENTAL FEATURE REQUEST" in spec_md
    assert "subtract" in spec_md
    # 기존 사용자 파일은 보존(증분 모드는 재빌드가 아님)
    assert (proj / "app.py").exists()
    assert "def add(a, b)" in (proj / "app.py").read_text(encoding="utf-8")
