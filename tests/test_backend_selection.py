"""백엔드 우선순위 풀 · 분산 · 폴오버 선택 로직 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator import runner as runner_mod
from orchestrator.backends.base import RoleResult
from orchestrator.board import Board
from orchestrator.config import ROLES, RunConfig

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


def test_cross_check_alternates_no_hardcoded_groups():
    # 그룹 하드코딩 없음: 미핀 역할들을 두 프로바이더에 번갈아(교차) 배정.
    pool = ["codex", "claude-cli"]
    cfg = _cfg(backend_priority=pool, cross_check=True)
    firsts = [cfg.backends_for(r)[0] for r in ROLES]
    assert set(firsts) == set(pool)  # 두 프로바이더 모두 사용 (한쪽 몰림 X)
    assert 4 <= firsts.count("codex") <= 6  # 대략 반반 (교차)
    # 후보는 전체 풀(상대 프로바이더로 폴오버)
    assert sorted(cfg.backends_for("qa")) == sorted(pool)


def test_cross_check_honors_pin_and_crosses_rest():
    # 웹 케이스: 단일 기본 claude-cli + QA만 codex + cross_check (--backends 미지정).
    cfg = _cfg(default_backend="claude-cli", cross_check=True, role_priority={"qa": ["codex"]})
    assert cfg.backends_for("qa")[0] == "codex"  # 핀 준수
    firsts = [cfg.backends_for(r)[0] for r in ROLES]
    # 나머지가 전부 claude 로 몰리지 않고 두 프로바이더가 교차 사용된다
    assert set(firsts) == {"claude-cli", "codex"}
    assert firsts.count("codex") >= 2 and firsts.count("claude-cli") >= 2


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
        if self._ok:  # 계약 준수 백엔드: 결과 JSON 을 남긴다
            req.result_path.parent.mkdir(parents=True, exist_ok=True)
            req.result_path.write_text('{"status":"done","artifacts":[]}', encoding="utf-8")
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
