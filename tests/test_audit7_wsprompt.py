"""Audit r7: workspace.scaffold 안전 가드/spec 보존/마커 + prompts 감독자·이벤트 캡 검증.

오프라인 전용 — 외부 프로세스/네트워크 없이 순수 함수 동작만 확인한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import FRAMEWORK_ROOT
from orchestrator.prompts import compose_prompt
from orchestrator.workspace import _GEN_MARKER, scaffold

STACK = {"frontend": "React/Vite", "backend": "FastAPI", "db": "SQLite"}


# --------------------------------------------------------------------------- #
# workspace.scaffold: 위험 타깃 거부
# --------------------------------------------------------------------------- #
def test_scaffold_refuses_home_dir(monkeypatch: pytest.MonkeyPatch):
    # 환경변수 우회 플래그가 없는 상태를 보장한다.
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    with pytest.raises(ValueError):
        scaffold(Path.home(), "spec", {})


def test_scaffold_refuses_filesystem_root(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    with pytest.raises(ValueError):
        scaffold(Path("/"), "spec", {})


def test_scaffold_refuses_framework_root(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    with pytest.raises(ValueError):
        scaffold(FRAMEWORK_ROOT, "spec", {})


def test_scaffold_allows_home_with_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # 우회 플래그가 있으면 위험 타깃도 허용된다. 단 실제 홈을 오염시키지 않도록
    # Path.home() 을 임시 디렉터리로 가리키게 패치한 뒤 그 경로를 타깃으로 넘긴다.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", "1")
    # 우회 플래그가 있으므로 예외 없이 진행되어야 한다.
    scaffold(fake_home, "spec body", STACK)
    assert (fake_home / ".orchestrator").is_dir()


def test_scaffold_normal_subdir_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    target = tmp_path / "proj"
    scaffold(target, "the spec body", STACK)
    assert (target / ".orchestrator").is_dir()
    assert (target / ".orchestrator" / "spec.md").read_text(encoding="utf-8") == "the spec body"


def test_scaffold_home_subdir_ok_without_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # 홈 자체는 거부하지만 홈의 *하위* 디렉터리는 우회 없이도 정상 타깃이어야 한다.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    target = fake_home / "myproject"
    scaffold(target, "spec body", STACK)
    assert (target / ".orchestrator").is_dir()


# --------------------------------------------------------------------------- #
# workspace.scaffold: 빈 spec 으로 기존 spec.md 를 덮어쓰지 않음
# --------------------------------------------------------------------------- #
def test_scaffold_empty_spec_preserves_existing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    target = tmp_path / "proj"
    # 먼저 정상 spec 으로 한 번 스캐폴딩 → spec.md 가 생긴다.
    scaffold(target, "good spec content", STACK)
    spec_path = target / ".orchestrator" / "spec.md"
    assert spec_path.read_text(encoding="utf-8") == "good spec content"

    # 빈/공백 spec 으로 재실행해도 이전 정상 spec 이 보존되어야 한다.
    scaffold(target, "   \n  ", STACK)
    assert spec_path.read_text(encoding="utf-8") == "good spec content"

    scaffold(target, "", STACK)
    assert spec_path.read_text(encoding="utf-8") == "good spec content"


# --------------------------------------------------------------------------- #
# workspace.scaffold: 생성 마커는 본문 맨 앞에 있어야만 우리 생성물로 인정
# --------------------------------------------------------------------------- #
def test_scaffold_preserves_user_file_with_marker_not_at_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    target = tmp_path / "proj"
    target.mkdir()
    # 사용자 파일이 우연히 본문 *중간*에 마커 문자열을 포함하더라도 보존되어야 한다.
    user_body = f"# My own CLAUDE.md\n\nWe discussed the {_GEN_MARKER} convention here.\n"
    (target / "CLAUDE.md").write_text(user_body, encoding="utf-8")

    scaffold(target, "spec body", STACK)

    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == user_body


def test_scaffold_refreshes_file_starting_with_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    target = tmp_path / "proj"
    target.mkdir()
    stale = f"{_GEN_MARKER}\n# stale CLAUDE.md\n"
    (target / "CLAUDE.md").write_text(stale, encoding="utf-8")

    scaffold(target, "the current spec body", STACK)

    refreshed = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert refreshed != stale
    assert "the current spec body" in refreshed
    # 선행 공백이 있어도 lstrip 후 맨 앞이면 인정되어야 한다.
    target2 = tmp_path / "proj2"
    target2.mkdir()
    (target2 / "CLAUDE.md").write_text(f"\n\n  {_GEN_MARKER}\n# old\n", encoding="utf-8")
    scaffold(target2, "fresh spec", STACK)
    assert "fresh spec" in (target2 / "CLAUDE.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# prompts.compose_prompt: 감독자에게는 결과 JSON 지시 없음
# --------------------------------------------------------------------------- #
def test_supervisor_pm_has_no_completion_report():
    out = compose_prompt(
        role="project-manager",
        phase="supervisor",
        unit=None,
        directives="",
        result_rel=".orchestrator/results/project-manager.json",
        spec_excerpt="spec",
    )
    assert "Completion report (required)" not in out
    # 결과 JSON 을 쓰라고 지시하는 문구가 없어야 한다(경로는 "쓰지 말라"는 안내에 등장할 수 있음).
    assert "write your result as JSON" not in out
    # 산문으로 응답하라는 안내가 있어야 한다.
    assert "do NOT write any files" in out


def test_supervisor_pl_has_no_completion_report():
    out = compose_prompt(
        role="project-leader",
        phase="supervisor",
        unit=None,
        directives="",
        result_rel=".orchestrator/results/project-leader.json",
        spec_excerpt="spec",
    )
    assert "Completion report (required)" not in out
    assert "write your result as JSON" not in out
    assert "do NOT write any files" in out


def test_normal_role_has_completion_report():
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit=None,
        directives="",
        result_rel=".orchestrator/results/backend-developer__U1.json",
        spec_excerpt="spec",
    )
    assert "Completion report (required)" in out
    assert ".orchestrator/results/backend-developer__U1.json" in out


# --------------------------------------------------------------------------- #
# prompts.compose_prompt: recent_events 길이 캡
# --------------------------------------------------------------------------- #
def test_recent_events_truncated():
    huge = "E" * 9000
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit=None,
        directives="",
        result_rel="res.json",
        spec_excerpt="spec",
        recent_events=huge,
    )
    assert "## Recent events" in out
    # 캡 없이 통째로 실리면 9000개의 E 가 나오므로, 캡(최근 ~2000자)이 동작했는지 확인한다.
    # 전체 프롬프트의 다른 단어에도 'E' 가 섞이므로 정확히 2000 보다 약간 클 수 있지만,
    # 입력 9000 보다 훨씬 작아야 한다. 이벤트 블록 본문을 직접 잘라 길이를 검증한다.
    body = out.split("## Recent events\n", 1)[1].split("\n\n", 1)[0]
    assert body == "E" * 2000
    assert out.count("E") < 9000
