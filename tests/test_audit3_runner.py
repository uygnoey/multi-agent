"""audit3 runner 회귀 테스트 — #39(동시 호출 예산 초과) 및 #3(프롬프트 로깅 노출).

ONLY orchestrator/runner.py 를 다룬다. 오프라인·키 불필요: 제어 가능한 가짜 백엔드를
runner.get_backend 에 주입해 동시 호출/로깅 동작을 결정적으로 검증한다.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestrator import runner as runner_mod
from orchestrator.backends.base import RoleResult
from orchestrator.board import Board
from orchestrator.config import RunConfig


class _FakeBackend:
    """호출당 고정 비용을 보고하고, 동시성 검증을 위해 호출 시작을 잠시 대기시키는 백엔드."""

    name = "fake"

    def __init__(self, cost: float, *, gate: asyncio.Event | None = None):
        self.cost = cost
        self.gate = gate
        self.calls = 0

    def available(self) -> tuple[bool, str]:
        return True, "fake ready"

    async def run_role(self, req) -> RoleResult:
        self.calls += 1
        if self.gate is not None:
            # 모든 동시 시작자가 사전점검을 통과(혹은 막힘)한 뒤에야 비용을 커밋하도록 게이트.
            await self.gate.wait()
        return RoleResult(ok=True, final_message=f"[{req.role}] fake done", cost_usd=self.cost)


def _make_runner(
    tmp_path,
    *,
    budget=None,
    backend,
    default_backend_name: str = "mock",
    model: str | None = None,
) -> tuple[runner_mod.Runner, Board]:
    cfg = RunConfig(
        spec_path=tmp_path / "spec.md",
        project_dir=tmp_path / "proj",
        budget=budget,
        # #audit22: in-flight 예약이 백엔드/모델별로 추정됨. mock 은 의도적으로 0 예약
        # (예산 회계 비오염). N-way 예약 차단 검증 테스트는 default_backend_name + model
        # 인자로 비-mock 백엔드를 명시해 양수 추정을 받게 한다.
        default_backend=default_backend_name,
        model=model,
    )
    board = Board(cfg.project_dir)
    asyncio.run(board.init("spec body", {}))
    return runner_mod.Runner(cfg, board), board


# ── #39: 동시 호출이 시작 시점에 N-way 로 예산을 초과하지 못하게 한다 ──────────────


def test_concurrent_calls_do_not_all_pass_budget_check(tmp_path, monkeypatch):
    # 예산이 거의 소진된 상태에서 여러 역할을 동시에 시작하면, in-flight 예약 때문에
    # 하나만 통과하고 나머지는 blocked 가 되어 N-way 초과를 막는다.
    gate = asyncio.Event()
    backend = _FakeBackend(cost=0.0, gate=gate)
    monkeypatch.setattr(runner_mod, "get_backend", lambda name: backend)
    # #audit22: 백엔드/모델별 동적 추정으로 변경. mock 은 0 예약(비오염)이므로 비-mock +
    # 명시 모델로 예약치를 받는다. budget = 예약치 1회분 → 첫 호출만 통과, 이후 projected
    # >= budget 으로 막힘. 의도(N-way 동시 시작 차단)는 그대로.
    reserve = runner_mod._estimate_inflight_reserve("claude-cli", "claude-sonnet-4-6")
    runner, _board = _make_runner(
        tmp_path,
        budget=reserve,
        backend=backend,
        default_backend_name="claude-cli",
        model="claude-sonnet-4-6",
    )

    async def drive():
        roles = ["frontend-developer", "backend-developer", "dba"]
        tasks = [asyncio.create_task(runner.run_role(r, {"id": "U1"})) for r in roles]
        # 동시 시작자들이 모두 사전점검을 끝낼 시간을 준 뒤 게이트 개방.
        await asyncio.sleep(0.05)
        gate.set()
        return await asyncio.gather(*tasks)

    outs = asyncio.run(drive())
    blocked = [o for o in outs if o["status"] == "blocked"]
    passed = [o for o in outs if o["status"] != "blocked"]
    # 하나만 통과, 나머지는 예산으로 blocked → N-way 초과 방지.
    assert len(passed) == 1, outs
    assert len(blocked) == 2, outs
    assert all("budget" in b["blockers"][0] for b in blocked)
    # 실제 백엔드 호출도 통과한 한 번만 일어난다.
    assert backend.calls == 1


def test_inflight_reservation_released_after_call(tmp_path, monkeypatch):
    # 호출이 끝나면 예약이 해제되어 후속 호출이 정상적으로 통과한다(예약이 영구 누적되지 않음).
    backend = _FakeBackend(cost=0.0)
    monkeypatch.setattr(runner_mod, "get_backend", lambda name: backend)
    runner, _board = _make_runner(tmp_path, budget=1.0, backend=backend)

    async def drive():
        # 순차 실행: 매번 예약→해제 → 누적 비용은 0 이라 계속 통과해야 한다.
        outs = []
        for _ in range(3):
            outs.append(await runner.run_role("backend-developer", {"id": "U1"}))
        return outs

    outs = asyncio.run(drive())
    assert all(o["status"] != "blocked" for o in outs), outs
    assert runner._inflight_reserved == 0.0


def test_budget_none_skips_reservation(tmp_path, monkeypatch):
    # 예산 미설정이면 예약 로직을 거치지 않고 그대로 실행된다(기존 동작).
    backend = _FakeBackend(cost=5.0)
    monkeypatch.setattr(runner_mod, "get_backend", lambda name: backend)
    runner, _board = _make_runner(tmp_path, budget=None, backend=backend)
    out = asyncio.run(runner.run_role("backend-developer", {"id": "U1"}))
    assert out["status"] != "blocked"
    assert runner._inflight_reserved == 0.0


def test_committed_cost_still_blocks(tmp_path, monkeypatch):
    # 보드 누적 비용이 이미 예산 이상이면 (#112) 즉시 blocked — 예약 도입 후에도 유지.
    backend = _FakeBackend(cost=0.0)
    monkeypatch.setattr(runner_mod, "get_backend", lambda name: backend)
    runner, board = _make_runner(tmp_path, budget=1.0, backend=backend)
    asyncio.run(board.add_cost(2.0))  # 예산 초과 상태로 만든다
    out = asyncio.run(runner.run_role("backend-developer", {"id": "U1"}))
    assert out["status"] == "blocked"
    assert backend.calls == 0


# ── #3: 프롬프트 본문 로깅을 환경변수로 끌 수 있다 ─────────────────────────────


def test_log_prompt_bodies_default_true(monkeypatch):
    # 기본(미설정)은 전체 본문 기록 유지 — 하위호환·디버깅 편의.
    monkeypatch.delenv("ORCH_LOG_PROMPTS", raising=False)
    assert runner_mod._log_prompt_bodies() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "FALSE", "Off", ""])
def test_log_prompt_bodies_disabled(monkeypatch, val):
    monkeypatch.setenv("ORCH_LOG_PROMPTS", val)
    assert runner_mod._log_prompt_bodies() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_log_prompt_bodies_enabled(monkeypatch, val):
    monkeypatch.setenv("ORCH_LOG_PROMPTS", val)
    assert runner_mod._log_prompt_bodies() is True


def test_prompt_body_suppressed_when_flag_off(tmp_path, monkeypatch):
    # ORCH_LOG_PROMPTS=0 → per-agent 로그에 spec/directives 등 본문이 남지 않고 짧은 메모만.
    monkeypatch.setenv("ORCH_LOG_PROMPTS", "0")
    backend = _FakeBackend(cost=0.0)
    monkeypatch.setattr(runner_mod, "get_backend", lambda name: backend)
    cfg = RunConfig(spec_path=tmp_path / "spec.md", project_dir=tmp_path / "proj")
    board = Board(cfg.project_dir)
    # spec excerpt 에 민감해 보이는 토큰을 넣어 로그 누출 여부를 확인한다(프롬프트에 합성됨).
    asyncio.run(board.init("SECRET-SPEC-TOKEN should never hit the log", {}))
    runner = runner_mod.Runner(cfg, board)
    asyncio.run(runner.run_role("backend-developer", {"id": "U1"}))
    log = board.agent_log_tail("backend-developer", n=500)
    assert "prompt body suppressed" in log
    assert "SECRET-SPEC-TOKEN" not in log


def test_prompt_body_logged_when_flag_on(tmp_path, monkeypatch):
    # 기본(미설정)에서는 SYSTEM/TASK 본문이 그대로 로그에 남는다(기존 동작 유지).
    monkeypatch.delenv("ORCH_LOG_PROMPTS", raising=False)
    backend = _FakeBackend(cost=0.0)
    monkeypatch.setattr(runner_mod, "get_backend", lambda name: backend)
    runner, board = _make_runner(tmp_path, budget=None, backend=backend)
    asyncio.run(runner.run_role("backend-developer", {"id": "U1"}))
    log = board.agent_log_tail("backend-developer", n=500)
    assert "[SYSTEM]" in log
    assert "[TASK]" in log
