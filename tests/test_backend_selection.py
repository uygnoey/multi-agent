"""백엔드 우선순위 풀 · 분산 · 폴오버 선택 로직 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator import runner as runner_mod
from orchestrator.backends.base import RoleResult
from orchestrator.board import Board
from orchestrator.config import RunConfig

DUMMY = Path("/x")


def _cfg(**kw):
    return RunConfig(spec_path=DUMMY, project_dir=DUMMY, **kw)


def test_priority_pool_order():
    cfg = _cfg(backend_priority=["claude-cli", "codex", "claude-sdk"])
    assert cfg.backends_for("backend-developer") == ["claude-cli", "codex", "claude-sdk"]
    assert cfg.backend_for("backend-developer") == "claude-cli"


def test_mock_overrides_everything():
    cfg = _cfg(mock=True, backend_priority=["claude-cli", "codex"])
    assert cfg.backends_for("frontend-developer") == ["mock"]


def test_role_priority_wins_over_global():
    cfg = _cfg(backend_priority=["claude-cli"], role_priority={"dba": ["codex", "claude-sdk"]})
    assert cfg.backends_for("dba") == ["codex", "claude-sdk"]
    assert cfg.backends_for("backend-developer") == ["claude-cli"]


def test_distribute_spreads_roles_across_pool():
    pool = ["claude-cli", "codex", "claude-sdk", "openai-agents"]
    cfg = _cfg(backend_priority=pool, distribute=True)
    # 서로 다른 역할은 서로 다른 1순위를 가져 4종이 동시에 가동된다 (라운드로빈 회전).
    firsts = {cfg.backends_for(r)[0] for r in ROLES_SAMPLE}
    assert len(firsts) > 1
    # 각 역할의 후보는 여전히 전체 풀(폴오버 보장)
    assert sorted(cfg.backends_for("project-manager")) == sorted(pool)


ROLES_SAMPLE = [
    "project-manager",
    "project-leader",
    "architecture-engineer",
    "frontend-developer",
]


def test_cross_check_splits_build_and_verify():
    pool = ["codex", "claude-cli"]
    cfg = _cfg(backend_priority=pool, cross_check=True)
    # 생산자(build) → pool[0]=codex, 검증자(verify) → pool[1]=claude-cli
    for build_role in ("backend-developer", "frontend-developer", "dba", "architecture-engineer"):
        assert cfg.backends_for(build_role)[0] == "codex"
    for verify_role in ("test-engineer", "qa", "testsheet-creator"):
        assert cfg.backends_for(verify_role)[0] == "claude-cli"
    # PM / PL 은 서로 다른 프로바이더 (교차 감독)
    assert cfg.backends_for("project-manager")[0] != cfg.backends_for("project-leader")[0]
    # 후보는 여전히 전체 풀(상대 프로바이더로 폴오버)
    assert sorted(cfg.backends_for("qa")) == sorted(pool)


def test_cross_check_seeded_by_user_pick():
    # 유저가 PM=claude-cli 만 골랐고 나머지는 auto → 이 선택을 시드로 교차 배치.
    pool = ["codex", "claude-cli"]
    cfg = _cfg(
        backend_priority=pool,
        cross_check=True,
        role_priority={"project-manager": ["claude-cli"]},
    )
    # 명시 선택은 그대로
    assert cfg.backends_for("project-manager")[0] == "claude-cli"
    # PM(build 그룹)=claude → build측=claude, verify측=codex
    assert cfg.backends_for("backend-developer")[0] == "claude-cli"  # build
    assert cfg.backends_for("dba")[0] == "claude-cli"  # build
    assert cfg.backends_for("project-leader")[0] == "codex"  # verify (PM과 반대)
    assert cfg.backends_for("qa")[0] == "codex"  # verify
    assert cfg.backends_for("test-engineer")[0] == "codex"  # verify


def test_explicit_picks_always_win():
    # 유저가 각 역할을 다 골랐으면 cross_check 와 무관하게 그 선택을 따른다.
    cfg = _cfg(
        backend_priority=["codex", "claude-cli"],
        cross_check=True,
        role_priority={"qa": ["codex"], "backend-developer": ["claude-cli"]},
    )
    assert cfg.backends_for("qa") == ["codex"]
    assert cfg.backends_for("backend-developer") == ["claude-cli"]


class _FakeBackend:
    def __init__(self, name, ok):
        self.name = name
        self._ok = ok

    def available(self):
        return (True, "ok")

    async def run_role(self, req):
        return RoleResult(
            ok=self._ok,
            final_message=self.name,
            cost_usd=0.0,
            error=None if self._ok else "boom",
        )


def test_runner_fails_over_to_next_backend(tmp_path, monkeypatch, sample_spec_path):
    cfg = RunConfig(
        spec_path=sample_spec_path,
        project_dir=tmp_path / "p",
        backend_priority=["bad", "good"],
        retries=0,
    )
    board = Board(cfg.project_dir)
    board.spec_text = "spec"
    asyncio.run(board.init("spec", {}))

    fakes = {"bad": _FakeBackend("bad", False), "good": _FakeBackend("good", True)}
    monkeypatch.setattr(runner_mod, "get_backend", lambda n: fakes[n])

    out = asyncio.run(
        runner_mod.Runner(cfg, board).run_role("backend-developer", {"id": "U1", "title": "t"})
    )
    assert out["_ok"] is True
    assert board.agents()["backend-developer"]["backend"] == "good"
