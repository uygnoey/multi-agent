"""3차 감사 수정 검증.

- #32 monitor._run_alive 의 좀비 처리 (좀비 pid 는 살아있지 않은 것으로 본다)
- #15 rerun argv 플래그 화이트리스트 (알려진 오케스트레이터 플래그만 허용)
- #40 scaffold 의 생성 마커 기반 보존/갱신 (사용자 CLAUDE.md/AGENTS.md 보존)
"""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.monitor import _is_zombie, _run_alive, _validate_rerun_argv
from orchestrator.workspace import _GEN_MARKER, scaffold

STACK = {"frontend": "React/Vite", "backend": "FastAPI", "db": "SQLite"}


# ---------------- #32 좀비 프로세스 처리 ----------------


def test_run_alive_false_when_no_pidfile(tmp_path: Path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    assert _run_alive(orch) is False  # run.pid 없음 → 죽음


def test_run_alive_false_on_bad_pidfile(tmp_path: Path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "run.pid").write_text("not-a-pid", encoding="utf-8")
    assert _run_alive(orch) is False  # 파싱 실패 → 죽음 (예외 없이)


def test_run_alive_treats_zombie_as_dead(tmp_path: Path, monkeypatch):
    # 좀비는 os.kill(pid,0) 이 성공해도 살아있지 않은 것으로 봐야 한다 (#32, 웹 UI 와 일치).
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "run.pid").write_text(str(os.getpid()), encoding="utf-8")
    # 좀비 판별을 강제로 True 로 만들어 자기 pid 가 좀비인 것처럼 시뮬레이션.
    monkeypatch.setattr("orchestrator.monitor._is_zombie", lambda pid: True)
    assert _run_alive(orch) is False


def test_run_alive_true_for_live_non_zombie(tmp_path: Path, monkeypatch):
    # 살아있고 좀비가 아니면 running 으로 본다.
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "run.pid").write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr("orchestrator.monitor._is_zombie", lambda pid: False)
    assert _run_alive(orch) is True


def test_is_zombie_returns_false_for_self():
    # 현재 프로세스는 좀비가 아니다 (best-effort 판별이 정상 동작하는지 확인).
    assert _is_zombie(os.getpid()) is False


# ---------------- #15 rerun argv 플래그 화이트리스트 ----------------


def test_rerun_argv_allows_known_flags():
    ok, _ = _validate_rerun_argv(["--spec", "spec.md", "--project-dir", "out", "--mock"])
    assert ok is True


def test_rerun_argv_allows_help():
    # argparse 자동 플래그(--help/-h)는 정상 토큰이므로 허용 (기존 동작 유지).
    assert _validate_rerun_argv(["--help"])[0] is True
    assert _validate_rerun_argv(["-h"])[0] is True


def test_rerun_argv_allows_equals_form():
    # '--flag=value' 형태도 '=' 앞부분으로 화이트리스트와 대조해 허용.
    assert _validate_rerun_argv(["--budget=5.0", "--mock"])[0] is True


def test_rerun_argv_rejects_unknown_flag():
    # 화이트리스트에 없는 플래그는 거부 (#15).
    ok, why = _validate_rerun_argv(["--spec", "x", "--exec-evil"])
    assert ok is False
    assert "--exec-evil" in why


def test_rerun_argv_rejects_unknown_first_flag():
    ok, _ = _validate_rerun_argv(["--danger"])
    assert ok is False


def test_rerun_argv_still_rejects_non_list_and_abspath():
    # #90 기존 가드 회귀 체크.
    assert _validate_rerun_argv("not-a-list")[0] is False
    assert _validate_rerun_argv([1, 2])[0] is False
    assert _validate_rerun_argv(["/bin/sh"])[0] is False
    assert _validate_rerun_argv(["rm"])[0] is False  # 비플래그 첫 토큰


# ---------------- #40 생성 마커 기반 보존/갱신 ----------------


def test_scaffold_writes_generated_marker(tmp_path: Path):
    target = tmp_path / "proj"
    scaffold(target, "the spec body", STACK)
    claude = (target / "CLAUDE.md").read_text(encoding="utf-8")
    agents = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert _GEN_MARKER in claude
    assert _GEN_MARKER in agents


def test_scaffold_refreshes_our_generated_files(tmp_path: Path):
    # 우리가 만든 파일(마커 포함)은 재실행 시 현재 spec 으로 갱신돼야 한다 (#141 refresh).
    target = tmp_path / "proj"
    scaffold(target, "old spec body", STACK)
    new_stack = {"frontend": "Vue", "backend": "Django", "db": "Postgres"}
    scaffold(target, "brand new spec body", new_stack)
    claude = (target / "CLAUDE.md").read_text(encoding="utf-8")
    assert "brand new spec body" in claude
    assert "old spec body" not in claude
    assert "Postgres" in claude
    assert _GEN_MARKER in claude  # 마커는 재기록 후에도 유지


def test_scaffold_preserves_user_authored_claude_md(tmp_path: Path):
    # 사용자가 직접 쓴(마커 없는) CLAUDE.md/AGENTS.md 는 덮어쓰지 않는다 (#40).
    target = tmp_path / "proj"
    target.mkdir()
    user_claude = "# My hand-written project guide\nDo not touch this.\n"
    user_agents = "# My agents notes\n"
    (target / "CLAUDE.md").write_text(user_claude, encoding="utf-8")
    (target / "AGENTS.md").write_text(user_agents, encoding="utf-8")

    scaffold(target, "the current spec body", STACK)

    assert (target / "CLAUDE.md").read_text(encoding="utf-8") == user_claude
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == user_agents
    # 사용자 파일이라도 .orchestrator/spec.md 는 항상 기록된다 (내부 상태).
    assert (target / ".orchestrator" / "spec.md").read_text(encoding="utf-8") == (
        "the current spec body"
    )


def test_scaffold_spec_md_always_rewritten(tmp_path: Path):
    # .orchestrator/spec.md 는 사용자 파일이 아니므로 항상 (재)기록 (#140).
    target = tmp_path / "proj"
    scaffold(target, "first spec", STACK)
    assert (target / ".orchestrator" / "spec.md").read_text(encoding="utf-8") == "first spec"
    scaffold(target, "second spec", STACK)
    assert (target / ".orchestrator" / "spec.md").read_text(encoding="utf-8") == "second spec"
