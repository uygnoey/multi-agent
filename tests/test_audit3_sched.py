"""Audit3 회귀 테스트 (config.py / __main__.py / scheduler.py).

대상 이슈: 28 / 29 / 33.
모두 offline·mock 전용이며 tmp_path 아래에만 쓴다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.__main__ import _print_summary
from orchestrator.board import BLOCKED, FAILED
from orchestrator.config import RunConfig
from orchestrator.scheduler import Scheduler

# ---- 헬퍼 ----------------------------------------------------------------


def _cfg(tmp_path: Path, sample_spec_path: Path, **kw) -> RunConfig:
    base = dict(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        mock=True,
        poll_interval=600.0,
    )
    base.update(kw)
    return RunConfig(**base)


# ---- #33: poll_interval 0/음수/비-숫자 → 안전 하한으로 클램프 -------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 5.0),  # 0 → 안전 바닥(5초)
        (0.0, 5.0),
        (-1.0, 5.0),  # 음수 → 안전 바닥
        (-100, 5.0),
        (0.5, 1.0),  # 0<x<1 → 최소 1초로 끌어올림
        ("nope", 5.0),  # 비-숫자 → 기본값(20)으로 안전화 후 양수라 그대로
    ],
)
def test_poll_interval_clamped_to_safe_floor(tmp_path, sample_spec_path, raw, expected):
    cfg = _cfg(tmp_path, sample_spec_path, poll_interval=raw)
    if raw == "nope":
        # 비-숫자는 기본값 20.0 으로 안전화되어 busy-loop 이 생기지 않는다.
        assert cfg.poll_interval == 20.0
    else:
        assert cfg.poll_interval == expected
    # 어떤 경우에도 양수 + 1초 이상이어야 busy-loop 이 불가능하다.
    assert cfg.poll_interval >= 1.0


def test_poll_interval_normal_value_preserved(tmp_path, sample_spec_path):
    # 정상적인 큰 값은 그대로 유지되어야 한다(클램프가 정상 동작을 망치지 않음).
    cfg = _cfg(tmp_path, sample_spec_path, poll_interval=600.0)
    assert cfg.poll_interval == 600.0


def test_supervise_uses_clamped_poll_interval(tmp_path, sample_spec_path, monkeypatch):
    # _supervise 가 클램프된 cfg.poll_interval 값을 asyncio.wait_for 로 넘기는지 확인.
    cfg = _cfg(tmp_path, sample_spec_path, poll_interval=0)  # → 5.0 으로 클램프
    assert cfg.poll_interval == 5.0
    sched = Scheduler(cfg)
    asyncio.run(sched.board.init("spec", {}))

    captured: dict = {}

    async def fake_run_role(role, unit=None):
        return {"_ok": True, "notes": [], "artifacts": []}

    async def fake_wait_for(coro, timeout):
        captured["timeout"] = timeout
        # 한 번 캡처한 뒤 즉시 멈춰 루프를 끝낸다.
        sched._stop.set()
        # 넘겨받은 coroutine 을 소비해 경고가 새지 않게 한다.
        coro.close()
        raise asyncio.TimeoutError

    sched.runner.run_role = fake_run_role
    monkeypatch.setattr("orchestrator.scheduler.asyncio.wait_for", fake_wait_for)
    asyncio.run(sched._supervise("project-manager"))
    assert captured["timeout"] == 5.0


# ---- #29: _print_summary 가 비-숫자 total_cost_usd 에서 안 죽음 ------------


@pytest.mark.parametrize("bad_cost", ["oops", None, [1, 2], {"a": 1}])
def test_print_summary_survives_non_numeric_cost(tmp_path, sample_spec_path, capsys, bad_cost):
    cfg = _cfg(tmp_path, sample_spec_path)
    snap = {
        "phase": "done",
        "units": [],
        "total_cost_usd": bad_cost,
        "warnings": [],
    }
    # 손상된 cost 값이어도 예외 없이 보드/리포트 경로까지 출력되어야 한다.
    _print_summary(snap, cfg)
    out = capsys.readouterr().out
    assert "cost        : $0.0000" in out  # 폴백 0.0 으로 출력
    assert "board       :" in out
    assert "report      :" in out


def test_print_summary_numeric_cost_formatted(tmp_path, sample_spec_path, capsys):
    cfg = _cfg(tmp_path, sample_spec_path)
    snap = {"phase": "done", "units": [], "total_cost_usd": 1.23456, "warnings": []}
    _print_summary(snap, cfg)
    out = capsys.readouterr().out
    assert "cost        : $1.2346" in out  # 정상 숫자는 그대로 포맷


# ---- #28: 빌드 미완료 시 cicd/docs 는 돌되 done 으로 끝나지 않음 ----------


def test_broken_build_runs_cicd_docs_but_phase_failed(tmp_path, sample_spec_path):
    # max_attempts=2(유한): 영구 실패 dev 가 retries 소진 후 FAILED 로 수렴해야 phase=failed 를
    # 검증할 수 있다. 기본 0(제품 완주 모드)은 완료까지 무한 수리라 종료하지 않는다(#C1).
    cfg = _cfg(tmp_path, sample_spec_path, max_attempts=2)
    sched = Scheduler(cfg)

    ran: list[str] = []

    async def design_then_fail(role, unit=None):
        if unit is None and role == "architecture-engineer":
            return {
                "_ok": True,
                "artifacts": [],
                "status": "done",
                "units": [{"id": "U1", "title": "a"}],
            }
        if unit is None:
            # cicd/docs/testsheet/supervisor 등 unit 없는 호출은 성공으로 둔다.
            ran.append(role)
            return {"_ok": True, "artifacts": [], "status": "done", "units": []}
        # dev 호출은 실패 → unit blocked/failed
        return {"_ok": False, "artifacts": [], "status": "failed", "blockers": ["x"]}

    sched.runner.run_role = design_then_fail
    snap = asyncio.run(sched.run())

    # 빌드가 깨졌어도 cicd/docs 산출물 페이즈는 여전히 실행되어야 한다(부분 산출물 유용).
    assert "cicd" in ran
    assert "docs-writer" in ran
    # 그러나 최종 phase 는 'done' 이 아니라 'failed' 여야 한다(불완전 빌드의 묵시적 done 금지).
    assert snap["phase"] == "failed"
    # 깨진 unit 경고 + 불완전 빌드 위에서 cicd/docs 가 돌았다는 로그가 남아야 한다.
    assert any("미완료" in w or "failed/blocked" in w for w in (snap.get("warnings") or []))
    events = (sched.board.orch_dir / "events.log").read_text(encoding="utf-8")
    assert "INCOMPLETE build" in events


def test_clean_build_phase_done(tmp_path, sample_spec_path):
    # 정상(mock) 빌드는 done 으로 끝나야 한다(클램프/경고 로직이 정상 성공을 망치지 않음).
    cfg = _cfg(tmp_path, sample_spec_path)
    snap = asyncio.run(Scheduler(cfg).run())
    assert snap["phase"] == "done"
    broken = [u for u in snap["units"] if u["status"] in (FAILED, BLOCKED)]
    assert not broken
