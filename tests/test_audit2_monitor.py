"""2차 감사 수정 검증: agents 프론트매터(#136), workspace 재기록(#140/#141),
monitor 숫자 가드(#142)·로그 tail/mtime(#36), 그리고 기존 수정 회귀 체크
(#70 손상 보드 / #90 rerun argv / #132 실패 라벨 / #133 백엔드 오버플로 / #14·#24 stop·rerun).
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.agents import _split_frontmatter
from orchestrator.config import ROLES
from orchestrator.monitor import (
    _num,
    _read_agent_log_cached,
    _read_board,
    _state_label,
    _validate_rerun_argv,
    render_snapshot,
)
from orchestrator.workspace import scaffold

STACK = {"frontend": "React/Vite", "backend": "FastAPI", "db": "SQLite"}


# ---------------- #136 frontmatter 닫는 펜스 ----------------


def test_frontmatter_close_requires_exact_dash_line():
    # 본문에 '---extra' 가 있어도 그 줄을 닫는 마커로 오인하면 안 된다 (#136).
    text = "---\nname: x\ntools: Read\n---extra in body\n---\nreal body here\n"
    fm, body = _split_frontmatter(text)
    assert "name: x" in fm
    assert "tools: Read" in fm
    # '---extra' 는 frontmatter 안 본문이 아니라 닫는 펜스로 잘못 잡히면 안 됨
    assert "---extra in body" in fm
    assert body.strip() == "real body here"


def test_frontmatter_close_rejects_four_dashes():
    # '----' (수평선) 는 닫는 펜스가 아니다 (#136).
    text = "---\nname: y\n----\nstill frontmatter\n---\nbody\n"
    fm, body = _split_frontmatter(text)
    assert "----" in fm
    assert "still frontmatter" in fm
    assert body.strip() == "body"


def test_frontmatter_close_allows_trailing_whitespace():
    # 닫는 펜스 줄에 뒤 공백이 있어도 정확히 '---' 로 인정 (#136).
    text = "---\nname: z\n---   \nbody text\n"
    fm, body = _split_frontmatter(text)
    assert "name: z" in fm
    assert body.strip() == "body text"


def test_frontmatter_first_line_must_be_exact_dashes():
    # 여는 검사 유지: 첫 줄이 정확히 '---' 가 아니면 frontmatter 아님.
    text = "----\nnot frontmatter\n---\n"
    fm, body = _split_frontmatter(text)
    assert fm == ""
    assert body == text


def test_frontmatter_no_close_returns_whole_as_body():
    text = "---\nname: open\nno closing fence\n"
    fm, body = _split_frontmatter(text)
    assert fm == ""
    assert body == text


# ---------------- #140/#141 spec.md/CLAUDE.md/AGENTS.md 항상 재기록 ----------------


def test_scaffold_rewrites_stale_spec(tmp_path: Path):
    target = tmp_path / "proj"
    scaffold(target, "first spec", STACK)
    assert (target / ".orchestrator" / "spec.md").read_text(encoding="utf-8") == "first spec"
    # 같은 디렉터리에 새 spec 으로 다시 돌리면 stale 하지 않게 갱신돼야 함 (#140)
    scaffold(target, "second spec", STACK)
    assert (target / ".orchestrator" / "spec.md").read_text(encoding="utf-8") == "second spec"


def test_scaffold_rewrites_stale_claude_and_agents(tmp_path: Path):
    target = tmp_path / "proj"
    scaffold(target, "old product spec", STACK)
    # 새 stack/spec 으로 재실행 → 생성물에 현재 내용이 반영돼야 함 (#141)
    new_stack = {"frontend": "Vue", "backend": "Django", "db": "Postgres"}
    scaffold(target, "brand new spec excerpt", new_stack)
    claude = (target / "CLAUDE.md").read_text(encoding="utf-8")
    agents = (target / "AGENTS.md").read_text(encoding="utf-8")
    assert "brand new spec excerpt" in claude
    assert "old product spec" not in claude
    assert "Postgres" in claude
    assert "brand new spec excerpt" in agents
    assert "Django" in agents


def test_scaffold_preserves_user_authored_subagents(tmp_path: Path):
    # 사용자가 직접 쓴 .claude/agents/*.md 는 보존 (#12) — 재기록 대상 아님.
    target = tmp_path / "proj"
    dest = target / ".claude" / "agents"
    dest.mkdir(parents=True)
    custom = dest / "backend-developer.md"
    custom.write_text("# my edited agent\n", encoding="utf-8")
    scaffold(target, "spec", STACK)
    assert custom.read_text(encoding="utf-8") == "# my edited agent\n"


# ---------------- #142 비숫자 board 필드 가드 ----------------


def test_num_coerces_bad_values():
    assert _num(1.25) == 1.25
    assert _num("2.5") == 2.5
    assert _num(None) == 0.0
    assert _num("not-a-number") == 0.0
    assert _num([1, 2, 3]) == 0.0
    assert _num({}) == 0.0
    assert _num(True) == 0.0  # bool 은 비용/토큰으로 취급 안 함


def test_render_snapshot_survives_corrupt_numeric_fields():
    # 수동 편집/부분 손상으로 비숫자가 들어와도 :.4f / :, 포매팅이 터지면 안 됨 (#142).
    board = {
        "phase": "build",
        "total_cost_usd": "corrupt",
        "total_tokens": None,
        "units": [{"status": "done"}, {"status": "failed"}],
        "agents": {
            "qa": {"status": "running", "cost_usd": "bad", "tokens": "x", "calls": None},
        },
    }
    out = render_snapshot(board, list(ROLES))  # 예외 없이 렌더링
    assert "cost=$0.0000" in out
    assert "tokens=0" in out
    assert "qa" in out


def test_render_snapshot_normal_numbers_still_formatted():
    board = {
        "phase": "done",
        "total_cost_usd": 1.5,
        "total_tokens": 1234,
        "units": [{"status": "done"}],
        "agents": {"qa": {"status": "idle", "cost_usd": 0.25, "tokens": 999, "calls": 3}},
    }
    out = render_snapshot(board, list(ROLES))
    assert "cost=$1.5000" in out
    assert "tokens=1,234" in out


# ---------------- #36 로그 tail / mtime 캐시 ----------------


def test_read_agent_log_cached_tails_and_caches(tmp_path: Path):
    orch = tmp_path / ".orchestrator"
    (orch / "agents").mkdir(parents=True)
    log = orch / "agents" / "qa.log"
    log.write_text("\n".join(f"line{i}" for i in range(2000)), encoding="utf-8")

    out = _read_agent_log_cached(orch, "qa", n=500)
    lines = out.splitlines()
    assert len(lines) == 500  # 전체가 아니라 최근 tail 만
    assert lines[-1] == "line1999"
    assert lines[0] == "line1500"

    # 두 번째 호출: 파일이 안 바뀌면 같은 캐시 결과 (재읽기 회피)
    assert _read_agent_log_cached(orch, "qa", n=500) == out


def test_read_agent_log_cached_refreshes_on_change(tmp_path: Path):
    import os
    import time

    orch = tmp_path / ".orchestrator"
    (orch / "agents").mkdir(parents=True)
    log = orch / "agents" / "qa.log"
    log.write_text("first\n", encoding="utf-8")
    assert "first" in _read_agent_log_cached(orch, "qa")

    time.sleep(0.01)
    log.write_text("first\nsecond\n", encoding="utf-8")
    # mtime/size 가 바뀌도록 명시적으로 갱신 (빠른 파일시스템 보호)
    now = time.time() + 1
    os.utime(log, (now, now))
    out = _read_agent_log_cached(orch, "qa")
    assert "second" in out


def test_read_agent_log_cached_missing_file(tmp_path: Path):
    orch = tmp_path / ".orchestrator"
    (orch / "agents").mkdir(parents=True)
    assert _read_agent_log_cached(orch, "missing") == ""


# ---------------- 회귀 체크: #70 / #90 / #132 / #14·#24 ----------------


def test_corrupt_board_sentinel(tmp_path: Path):
    # 손상 board.json 을 빈 보드로 숨기지 않고 _corrupt 로 표시 (#70).
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "board.json").write_text("{not valid json", encoding="utf-8")
    assert _read_board(orch) == {"_corrupt": True}
    # 비-dict JSON 도 손상으로 취급
    (orch / "board.json").write_text("[1,2,3]", encoding="utf-8")
    assert _read_board(orch) == {"_corrupt": True}
    # 파일 없음은 빈 보드(대기)
    (orch / "board.json").unlink()
    assert _read_board(orch) == {}


def test_rerun_argv_validation_present():
    # #90: list[str] 아니거나 첫 토큰이 절대경로/비플래그면 거부.
    # #11(round-6): --help 는 rerun argv 로 거부(거짓 "재실행 시작" 방지). 정상 store-true 는 허용.
    assert _validate_rerun_argv(["--mock"])[0] is True
    assert _validate_rerun_argv(["--help"])[0] is False
    assert _validate_rerun_argv("not-a-list")[0] is False
    assert _validate_rerun_argv([1, 2])[0] is False
    assert _validate_rerun_argv(["/bin/sh"])[0] is False
    assert _validate_rerun_argv(["rm"])[0] is False  # 비플래그 첫 토큰


def test_state_label_exposes_failed_and_blocked():
    # #132: 실패·블록 unit 이 라벨에 노출돼야 함.
    units = [{"status": "failed"}, {"status": "blocked"}, {"status": "done"}]
    label = _state_label(alive=False, phase="done", warnings=[], units=units)
    assert "failed" in label and "blocked" in label
    assert _state_label(alive=True, phase="build", warnings=[], units=[]) == "running"
