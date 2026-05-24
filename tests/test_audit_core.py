"""Audit 회귀 테스트 (runner / monitor / agents / config / workspace / prompts).

대상 이슈: 11, 12, 14, 25, 31, 32, 37, 47, 48, 49, 50, 70, 90, 91, 92, 93, 94,
112, 132, 133.
모두 offline·mock 전용이며 tmp_path 아래에만 쓴다 (API 키/네트워크 불필요).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator import runner as runner_mod
from orchestrator.agents import AgentDef, _norm_model
from orchestrator.backends.base import RoleResult
from orchestrator.board import Board
from orchestrator.config import RO_TOOLS, ROLES, RunConfig
from orchestrator.monitor import (
    _read_board,
    _state_label,
    _validate_rerun_argv,
    render_snapshot,
)
from orchestrator.prompts import compose_prompt
from orchestrator.runner import Runner, _coerce_result, _safe_rel_artifact, _under_results_dir
from orchestrator.workspace import expose_team_agents, scaffold

STACK = {"frontend": "React", "backend": "FastAPI", "db": "SQLite"}


def _cfg(tmp_path: Path, sample_spec_path: Path, **kw) -> RunConfig:
    base = dict(
        spec_path=sample_spec_path,
        project_dir=tmp_path / "p",
        poll_interval=600.0,
    )
    base.update(kw)
    return RunConfig(**base)


# ---- #11: runner 아티팩트 경로 검증 ---------------------------------------


def test_coerce_result_drops_unsafe_artifacts():
    res = RoleResult(ok=True)
    data = {
        "status": "done",
        "artifacts": [
            "backend/app/api.py",  # OK
            "/etc/passwd",  # 절대경로 → drop
            "../../escape.txt",  # traversal → drop
            "C:\\windows\\evil",  # 드라이브 절대경로 → drop
            "..\\win-escape",  # traversal → drop
            123,  # 비-str → drop
            "  ",  # 공백 → drop
            "src/ok.py",  # OK
        ],
    }
    out = _coerce_result(data, res)
    assert out["artifacts"] == ["backend/app/api.py", "src/ok.py"]
    assert out["_ok"] is True


def test_safe_rel_artifact_helper():
    assert _safe_rel_artifact("a/b.py") == "a/b.py"
    assert _safe_rel_artifact("/abs") is None
    assert _safe_rel_artifact("../x") is None
    assert _safe_rel_artifact("") is None
    assert _safe_rel_artifact(None) is None


# ---- #25: result_path 가 results_dir 밖이면 삭제 안 함 ----------------------


def test_under_results_dir_guard(tmp_path):
    board = Board(tmp_path / "p")
    asyncio.run(board.init("s", {}))
    inside = board.results_dir / "frontend-developer__U1.json"
    outside = tmp_path / "p" / "something_else.json"
    assert _under_results_dir(inside, board) is True
    assert _under_results_dir(outside, board) is False


def test_run_role_does_not_delete_outside_results(tmp_path, sample_spec_path, monkeypatch):
    # mock 백엔드로 정상 run → 외부 파일은 건드리지 않고 결과파일은 results 안에 남는다.
    cfg = _cfg(tmp_path, sample_spec_path, mock=True, retries=0)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    sentinel = cfg.project_dir / "DO_NOT_DELETE.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    asyncio.run(Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert sentinel.exists()  # results 밖 파일은 삭제 로직이 건드리지 않음


def test_run_role_setup_exception_returns_failed_outcome(tmp_path, sample_spec_path, monkeypatch):
    cfg = _cfg(tmp_path, sample_spec_path, mock=True, retries=0)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    monkeypatch.setattr(runner_mod, "load_agent", lambda role: (_ for _ in ()).throw(OSError("x")))

    out = asyncio.run(Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))

    assert out["_ok"] is False
    assert out["status"] == "failed"
    assert "setup/preflight" in out["blockers"][0]


# ---- #31/#32: write_agent_block 본문 절단(보드) 확인 -------------------------


def test_agent_block_body_truncated(tmp_path, sample_spec_path):
    cfg = _cfg(tmp_path, sample_spec_path, mock=True, retries=0)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    huge = "X" * 50000
    board.write_agent_block("backend-developer", "BIG", huge)
    log = (board.agents_dir / "backend-developer.log").read_text(encoding="utf-8")
    assert "…(truncated)" in log  # 본문이 ~20000자로 절단됨
    assert log.count("X") <= 20001


# ---- #37: RunConfig 정수 정규화는 raw ValueError 를 던지지 않는다 ------------


def test_runconfig_bad_int_does_not_raise(tmp_path):
    # 라이브러리 호출부가 잘못된 값을 줘도 안전한 기본값으로 클램프 (raw ValueError X).
    cfg = RunConfig(
        spec_path=Path("s.md"),
        project_dir=tmp_path / "p",
        concurrency="oops",  # type: ignore[arg-type]
        max_attempts=None,  # type: ignore[arg-type]
        retries="x",  # type: ignore[arg-type]
        max_units="bad",  # type: ignore[arg-type]
    )
    assert cfg.concurrency == 3
    assert cfg.max_attempts == 0
    assert cfg.retries == 1
    assert cfg.max_units == 1


def test_runconfig_clamps_out_of_range():
    cfg = RunConfig(
        spec_path=Path("s.md"),
        project_dir=Path("p"),
        concurrency=0,
        max_attempts=-5,
        retries=-1,
        max_units=0,
    )
    assert cfg.concurrency == 1
    assert cfg.max_attempts == 0
    assert cfg.retries == 0
    assert cfg.max_units is None


# ---- #47/#48: 완료 보고 프롬프트가 status/경로/id 규칙을 명시 ----------------


def test_completion_report_prompt_specifies_status_and_path_rules():
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit={"id": "U1", "title": "t", "description": "d", "deps": []},
        directives="",
        result_rel=".orchestrator/results/backend-developer__U1.json",
        spec_excerpt="",
    )
    # 허용 status enum 명시
    assert "`done`" in out and "`failed`" in out and "`blocked`" in out
    # 프로젝트 상대경로 / traversal 금지
    assert "project-relative" in out
    assert ".." in out  # '..' parent-traversal 금지 문구
    assert "absolute" in out.lower()
    # id 는 단순 슬러그
    assert "simple slug" in out


# ---- #49/#50: 감독(PM/PL) 툴셋은 Bash 미포함, Read 전용 ----------------------


def test_supervisor_tools_have_no_bash():
    assert "Bash" not in RO_TOOLS
    assert RO_TOOLS == ("Read",)
    for role in ("project-manager", "project-leader"):
        tools = ROLES[role].tools
        assert "Bash" not in tools
        assert "Read" in tools


# ---- #70: 손상 board.json 은 빈 보드로 숨기지 않고 sentinel 반환 ------------


def test_read_board_missing_vs_corrupt(tmp_path):
    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    # 파일 없음 → {}
    assert _read_board(orch) == {}
    # 손상된 JSON → {"_corrupt": True}
    (orch / "board.json").write_text("{not valid json", encoding="utf-8")
    assert _read_board(orch) == {"_corrupt": True}
    # 비-객체 JSON 도 손상으로 취급
    (orch / "board.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert _read_board(orch) == {"_corrupt": True}
    # 정상 객체는 그대로
    (orch / "board.json").write_text('{"phase": "done"}', encoding="utf-8")
    assert _read_board(orch) == {"phase": "done"}


# ---- #90: rerun.json argv 경량 검증 ---------------------------------------


def test_validate_rerun_argv():
    assert _validate_rerun_argv(["--mock", "--spec", "x.md"])[0] is True
    # #11(round-6): --help 는 rerun argv 로 거부(도움말만 찍고 끝나는 거짓 "재실행" 방지).
    assert _validate_rerun_argv(["--help"])[0] is False
    # list[str] 아님
    assert _validate_rerun_argv("not-a-list")[0] is False
    assert _validate_rerun_argv(["--ok", 5])[0] is False
    # 첫 토큰이 절대경로/다른 프로그램
    assert _validate_rerun_argv(["/bin/sh", "-c", "rm -rf /"])[0] is False
    assert _validate_rerun_argv(["evilprog", "--mock"])[0] is False


def test_rerun_refuses_corrupt_argv(tmp_path):
    from orchestrator.monitor import _rerun

    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "rerun.json").write_text('{"argv":["/bin/sh","-c","echo hi"]}', encoding="utf-8")
    ok, _msg = _rerun(orch)
    assert ok is False  # 절대경로 프로그램 거부


# ---- #14: 실행 중이면 rerun 거부 (기존 동작 유지) ---------------------------


def test_rerun_refused_when_alive(tmp_path):
    import os

    from orchestrator.monitor import _rerun

    orch = tmp_path / ".orchestrator"
    orch.mkdir()
    (orch / "rerun.json").write_text('{"argv":["--help"]}', encoding="utf-8")
    (orch / "run.pid").write_text(str(os.getpid()), encoding="utf-8")
    ok, msg = _rerun(orch)
    assert ok is False and "실행 중" in msg


# ---- #93/#94: agent.model fallback + 'inherit' 정규화 -----------------------


def test_norm_model_treats_inherit_as_none():
    assert _norm_model("inherit") is None
    assert _norm_model("INHERIT") is None
    assert _norm_model("Inherit") is None
    assert _norm_model("") is None
    assert _norm_model(None) is None
    assert _norm_model("claude-opus-4") == "claude-opus-4"


def test_build_req_uses_agent_model_as_fallback(tmp_path, sample_spec_path, monkeypatch):
    # cfg.model 미지정 + frontmatter model 이 'inherit' 가 아니면 그 모델을 RoleRequest 로 전달.
    from orchestrator.agents import AgentDef

    cfg = _cfg(tmp_path, sample_spec_path, mock=True)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    runner = Runner(cfg, board)
    spec = ROLES["backend-developer"]
    agent = AgentDef("backend-developer", "d", list(spec.tools), "my-frontmatter-model", "sys")
    req = runner._build_req(
        "backend-developer", spec, {"id": "U1"}, agent, "p", Path("r"), "r", "mock"
    )
    assert req.model == "my-frontmatter-model"


def test_build_req_cfg_model_wins_over_agent_model(tmp_path, sample_spec_path):
    from orchestrator.agents import AgentDef

    cfg = _cfg(tmp_path, sample_spec_path, mock=True, model="cli-model")
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    runner = Runner(cfg, board)
    spec = ROLES["backend-developer"]
    agent = AgentDef("backend-developer", "d", list(spec.tools), "frontmatter-model", "sys")
    req = runner._build_req(
        "backend-developer", spec, {"id": "U1"}, agent, "p", Path("r"), "r", "mock"
    )
    assert req.model == "cli-model"  # 명시 cfg 모델이 우선


def test_build_teammates_inherit_becomes_none(tmp_path, sample_spec_path):
    # bundled teammate(.md) 의 model: inherit 는 None 으로 전달돼야 한다.
    cfg = _cfg(tmp_path, sample_spec_path, mock=True, delegate=True)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    mates = Runner(cfg, board)._build_teammates("backend-developer")
    assert mates  # backend-developer → dba teammate
    for m in mates:
        assert m["model"] is None  # 'inherit' 가 모델명으로 새지 않음


def test_codex_delegate_passes_teammates_without_task_tool(tmp_path, sample_spec_path):
    cfg = _cfg(tmp_path, sample_spec_path, delegate=True, full_access=True)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    runner = Runner(cfg, board)
    spec = ROLES["backend-developer"]
    agent = AgentDef("backend-developer", "d", list(spec.tools), None, "sys")
    req = runner._build_req(
        "backend-developer", spec, {"id": "U1"}, agent, "p", Path("r"), "r", "codex"
    )
    assert req.delegate is True
    assert req.teammates
    assert "Task" not in req.allowed_tools
    assert req.full_access is True


# ---- #112: 누적 예산 enforcement (모든 백엔드 공통) -------------------------


def test_budget_blocks_when_already_exceeded(tmp_path, sample_spec_path):
    cfg = _cfg(tmp_path, sample_spec_path, mock=True, budget=1.0)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    asyncio.run(board.add_cost(1.5))  # 이미 예산 초과
    out = asyncio.run(Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert out["_ok"] is False
    assert out["status"] == "blocked"
    assert any("budget exceeded" in b for b in out["blockers"])


def test_budget_allows_when_under(tmp_path, sample_spec_path):
    cfg = _cfg(tmp_path, sample_spec_path, mock=True, budget=100.0)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    out = asyncio.run(Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert out["_ok"] is True  # 예산 내 → 정상 실행


def test_no_budget_means_no_block(tmp_path, sample_spec_path):
    cfg = _cfg(tmp_path, sample_spec_path, mock=True, budget=None)
    board = Board(cfg.project_dir)
    asyncio.run(board.init("s", {}))
    asyncio.run(board.add_cost(999.0))
    out = asyncio.run(Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert out["_ok"] is True  # 예산 미설정 → 차단 안 함


# ---- #132: state 라벨이 failed/blocked unit 을 노출 -------------------------


def test_state_label_shows_failed_and_blocked():
    units = [
        {"status": "done"},
        {"status": "failed"},
        {"status": "failed"},
        {"status": "blocked"},
    ]
    label = _state_label(False, "done", [], units)
    assert "2 failed" in label
    assert "1 blocked" in label
    # 깨끗한 run 은 부가 표시 없음
    assert _state_label(False, "done", [], [{"status": "done"}]) == "done"
    # 경고도 함께
    warn_label = _state_label(False, "done", ["w1"], [{"status": "failed"}])
    assert "⚠1" in warn_label and "1 failed" in warn_label


def test_render_snapshot_surfaces_failed_units():
    board = {
        "phase": "done",
        "units": [{"status": "done"}, {"status": "failed"}],
        "agents": {},
    }
    out = render_snapshot(board, list(ROLES), alive=False)
    assert "1 failed" in out


# ---- #133: 백엔드 뷰 오버플로 가드 (구조 검증) ------------------------------


def test_draw_backends_has_overflow_guard():
    from orchestrator import monitor

    class _Screen:
        def __init__(self):
            self.writes = []

        def getmaxyx(self):
            return (5, 120)

        def addnstr(self, y, x, text, n, attr=0):
            self.writes.append((y, x, text[:n], attr))

    old_status = monitor.backend_status
    monitor.backend_status = lambda: [
        {"name": f"b{i}", "ok": False, "reason": "missing"} for i in range(20)
    ]
    try:
        screen = _Screen()
        monitor._draw_backends(screen)
    finally:
        monitor.backend_status = old_status

    rendered = "\n".join(str(w[2]) for w in screen.writes)
    assert "resize terminal" in rendered


# ---- #12: expose_team_agents 가 사용자 편집본을 보존 -----------------------


def test_expose_team_agents_preserves_user_edits(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    # 1차: 전부 새로 기록
    count1 = expose_team_agents(target)
    assert count1 == 11
    # 사용자가 한 파일을 수정
    edited = target / ".claude" / "agents" / "backend-developer.md"
    edited.write_text("# my custom override\n", encoding="utf-8")
    # 2차: 기존 파일은 건드리지 않음 → 새로 쓴 파일 0개, 편집본 보존
    count2 = expose_team_agents(target)
    assert count2 == 0
    assert edited.read_text(encoding="utf-8") == "# my custom override\n"


def test_expose_team_agents_writes_when_missing(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    expose_team_agents(target)
    one = target / ".claude" / "agents" / "backend-developer.md"
    one.unlink()  # 하나 삭제
    count = expose_team_agents(target)
    assert count == 1  # 없는 파일만 새로 기록
    assert one.exists()


# ---- #140: scaffold 가 spec.md 를 현재 run 내용으로 항상 (재)기록 ----------
# (이전 #91 의 "기존 spec.md 보존" 동작은 stale 메타데이터 버그(#140)로 판명돼 뒤집힘.
#  spec.md 는 오케스트레이터 생성물이므로 새 spec 으로 돌리면 항상 갱신돼야 한다.)


def test_scaffold_rewrites_existing_spec_md(tmp_path):
    target = tmp_path / "proj"
    scaffold(target, "first spec", STACK)
    spec_md = target / ".orchestrator" / "spec.md"
    assert spec_md.read_text(encoding="utf-8") == "first spec"
    # 같은 디렉터리로 재스캐폴드 → 새 spec 으로 갱신 (stale 방지; #140)
    scaffold(target, "DIFFERENT spec", STACK)
    assert spec_md.read_text(encoding="utf-8") == "DIFFERENT spec"


# ---- #92: gitignore 검사는 라인 단위 (주석/부분일치 오인 방지) --------------


def test_gitignore_comment_does_not_block_seeding(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    # 주석에만 '.orchestrator/' 가 있으면 실제 ignore 가 아님 → 시드돼야 함.
    (target / ".gitignore").write_text("# ignore .orchestrator/ later\ndist/\n", encoding="utf-8")
    scaffold(target, "spec", STACK)
    lines = {ln.strip() for ln in (target / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert ".orchestrator/" in lines  # 실제 ignore 라인이 추가됨


def test_gitignore_real_line_skips_seeding(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    (target / ".gitignore").write_text(".orchestrator/\ndist/\n", encoding="utf-8")
    scaffold(target, "spec", STACK)
    cur = (target / ".gitignore").read_text(encoding="utf-8")
    # 이미 실제 라인이 있으면 중복 시드하지 않음 → 한 번만 등장
    assert cur.count(".orchestrator/") == 1
