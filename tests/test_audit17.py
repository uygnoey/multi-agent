"""Regression tests for audit17 (Claude↔Codex 4-round 교차검증 합의 — scope A).

각 테스트는 수정 전 동작(버그)을 재현하던 시나리오가 이제 올바른지 검증한다.
R1·R2 는 audit16 회귀, N1~N5 는 신규 확정 결함.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from orchestrator import webui
from orchestrator.board import Board, _coerce_finite_float, _dumps_safe
from orchestrator.config import DEV_ROLES, RunConfig
from orchestrator.monitor import _coerce_board_schema, render_snapshot
from orchestrator.scheduler import Scheduler


def _cfg(tmp_path: Path, sample_spec_path: Path, **kw) -> RunConfig:
    base = dict(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        mock=True,
        poll_interval=600.0,
        auto_commit=False,  # git checkpoint 노이즈 없이 빠르게
    )
    base.update(kw)
    return RunConfig(**base)


# ---------------------------------------------------------------------------
# R1 — dev 성공 시 _external_repeat 가 clear 되어 test 로 누수되지 않음 (audit16 회귀)
# ---------------------------------------------------------------------------


def test_r1_external_repeat_cleared_at_dev_test_boundary(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": list(DEV_ROLES)}])
        unit = next(u for u in sched.board.units() if u["id"] == "U1")
        # dev 수리 중 일시적 외부장애로 카운터가 1까지 올라간 상태를 모사
        sched._external_repeat["U1"] = 1
        # _test_unit 진입(=dev→test 경계)에서 클리어된 직후 즉시 반환하도록 stop 설정.
        sched._stop.set()

        async def fake(role, unit=None):
            return {"_ok": True, "status": "done", "artifacts": []}

        sched.runner.run_role = fake
        await sched._test_unit(unit, asyncio.Semaphore(1), 1)
        return sched._external_repeat.get("U1")

    after = asyncio.run(scenario())
    # 수정 전엔 dev 단계 잔여(1)가 검증 단계로 새어 test 첫 외부장애에서 곧장 >=2 거짓 BLOCK 됐다.
    assert after is None


# ---------------------------------------------------------------------------
# R2 — _coerce_board_schema 가 agent 스칼라 필드를 str 로 정규화 → render 크래시 방지
# ---------------------------------------------------------------------------


def test_r2_render_snapshot_survives_nonstr_agent_scalars():
    board = _coerce_board_schema(
        {
            "phase": "dev",
            "units": [],
            "agents": {
                "qa": {"model": 123, "status": {"x": 1}, "backend": ["b"], "current_unit": {}}
            },
        }
    )
    qa = board["agents"]["qa"]
    assert isinstance(qa["model"], str) and isinstance(qa["status"], str)
    # 수정 전엔 model=123 →  (a.get("model") or ...)[:18] 에서 TypeError 크래시.
    out = render_snapshot(board, ["qa"], alive=True)
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# N1 — webui agent status 가 esc() 로 이스케이프됨 (stored XSS 방지)
# ---------------------------------------------------------------------------


def test_n1_webui_status_escaped():
    src = Path(webui.__file__).read_text(encoding="utf-8")
    assert 'esc(a.status||"idle")' in src  # 이스케이프된 형태 존재
    assert "+(a.status||" not in src  # raw 연결 형태 제거됨


# ---------------------------------------------------------------------------
# N2 — --max-units 로 스킵된 dep 에 의존하면 stall 없이 즉시 blocked + warning
# ---------------------------------------------------------------------------


def test_n2_skipped_dep_blocks_immediately(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path))
        await sched.board.init("spec", {})
        await sched.board.add_units(
            [
                {"id": "U1", "title": "dep", "roles": list(DEV_ROLES)},
                {"id": "U2", "title": "dependent", "deps": ["U1"], "roles": list(DEV_ROLES)},
            ]
        )
        u2 = next(u for u in sched.board.units() if u["id"] == "U2")
        t0 = time.monotonic()
        ok = await sched._wait_for_deps(u2, skipped_unit_ids={"U1"})
        elapsed = time.monotonic() - t0
        warns = sched.board.snapshot().get("warnings", [])
        return ok, elapsed, warns

    ok, elapsed, warns = asyncio.run(scenario())
    assert ok is False  # 성공으로 치지 않음
    assert elapsed < 1.0  # stall 윈도까지 헛대기하지 않고 즉시 반환
    assert any(("스킵" in w) or ("--max-units" in w) for w in warns)


# ---------------------------------------------------------------------------
# N3 — _coerce_finite_float 가 bool 을 거부 (cost_add=True 가 $1 누적 방지)
# ---------------------------------------------------------------------------


def test_n3_coerce_finite_float_rejects_bool():
    assert _coerce_finite_float(True) == 0.0
    assert _coerce_finite_float(False) == 0.0
    # 정상 숫자는 그대로
    assert _coerce_finite_float(1.5) == 1.5
    assert _coerce_finite_float(3) == 3.0


# ---------------------------------------------------------------------------
# N4 — _dumps_safe 가 int/str 키 충돌을 단일 키로 합쳐 중복 JSON 키를 안 만든다
# ---------------------------------------------------------------------------


def test_n4_no_duplicate_json_keys():
    s = _dumps_safe({1: "a", "1": "b"})
    parsed = json.loads(s)  # 깨끗이 파싱
    assert list(parsed.keys()) == ["1"]  # 단일 키
    assert s.count('"1"') == 1  # 직렬화 문자열에도 "1" 키가 한 번만


# ---------------------------------------------------------------------------
# N5 — Board.init 가 비-str spec_text 에도 안전 (board.json 정상 생성)
# ---------------------------------------------------------------------------


def test_n5_init_accepts_nonstr_spec_text(tmp_path):
    async def scenario(idx, spec):
        b = Board(tmp_path / f"p{idx}")
        await b.init(spec, {})
        return b.path.exists(), b.snapshot().get("spec_excerpt")

    for idx, spec in enumerate((123, {"a": 1}, ["x"])):
        exists, excerpt = asyncio.run(scenario(idx, spec))
        assert exists  # 수정 전엔 int/dict 에서 TypeError 로 board.json 미생성
        assert isinstance(excerpt, str)
