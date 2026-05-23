"""audit7 회귀: board fsync/내구성·비-직렬화 격하, runner 후보 처리 예외 시 폴오버.

이 파일이 검증하는 것 (오프라인·tmp_path 전용, 실제 백엔드/네트워크 없음):
- board: _flush 가 (1) 비-직렬화 값(set 등)을 default=str 로 격하해 단일 writer 를 죽이지 않고,
  (2) init+변형 후 board.json 이 존재·유효 JSON 으로 영속화된다(fsync 경로 sanity).
- runner: 한 후보를 처리하는 도중(백엔드 호출이 아닌 단계 — write_agent_block 등)에 예기치 못한
  예외가 나도 그 후보만 실패로 보고 다음 후보로 폴오버한다(role 전체를 죽이지 않음).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from orchestrator import runner as runner_mod
from orchestrator.backends.base import RoleResult
from orchestrator.board import Board
from orchestrator.config import RunConfig


def _run(coro):
    return asyncio.run(coro)


# ── board: _flush 내구성 + 비-직렬화 값 default=str 격하 ─────────────────────


def test_flush_writes_valid_json_after_mutation(tmp_path: Path):
    # sanity: init + 변형(add_units) 후 board.json 이 존재하고 유효 JSON 으로 영속화된다.
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {"lang": "py"})
        await b.add_units([{"id": "U1", "title": "t"}])
        return b

    b = _run(scenario())
    assert b.path.exists()
    data = json.loads(b.path.read_text(encoding="utf-8"))  # 깨졌으면 여기서 raise
    assert any(u["id"] == "U1" for u in data["units"])


def test_flush_does_not_raise_on_non_serializable_value(tmp_path: Path):
    # 비-직렬화 값(set)이 _data 에 끼어도 _flush 가 TypeError 로 단일 writer 를 죽이면 안 된다.
    # default=str 경로로 문자열 격하되어 파일이 정상적으로 쓰여야 한다.
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    # set 은 json 기본 인코더로 직렬화 불가 → default=str 가 없으면 _flush 가 TypeError 를 던진다.
    b._data["bad_value"] = {"a", "b", "c"}
    b._flush()  # raise 하면 테스트 실패
    data = json.loads(b.path.read_text(encoding="utf-8"))  # 파일이 유효 JSON 으로 쓰였는지
    # set 은 str() 격하되어 문자열로 저장된다(예: "{'a', 'b', 'c'}").
    assert isinstance(data["bad_value"], str)
    assert "a" in data["bad_value"]


def test_flush_handles_bytes_and_custom_object(tmp_path: Path):
    # bytes/커스텀 객체 같은 다른 비-직렬화 값도 default=str 로 격하되어 안전해야 한다.
    class Custom:
        def __str__(self):
            return "custom-repr"

    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    b._data["raw_bytes"] = b"\x01\x02"
    b._data["obj"] = Custom()
    b._flush()
    data = json.loads(b.path.read_text(encoding="utf-8"))
    assert isinstance(data["raw_bytes"], str)
    assert data["obj"] == "custom-repr"


# ── runner: 후보 처리 중 비-백엔드 예외 → 다음 후보로 폴오버 ──────────────────


def test_processing_exception_fails_over_to_next_candidate(
    tmp_path: Path, sample_spec_path, monkeypatch
):
    # 첫 후보를 처리하는 도중 비-백엔드 예외(write_agent_block 가 처음 한 번만 raise)가 나도
    # 그 후보만 실패로 보고 두 번째 후보로 폴오버해 성공 결과를 돌려줘야 한다(role 전체 미사망).
    run_calls: list[str] = []

    class SpyA:
        # 첫 후보의 백엔드는 정상 동작하지만, 그 후보를 처리하는 단계(write_agent_block)에서
        # 예외가 나도록 board 를 monkeypatch 한다 → 후보 A 처리는 예외로 실패.
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            run_calls.append("A")
            return RoleResult(ok=True, final_message="A ran")

    class SpyB:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            run_calls.append("B")
            return RoleResult(ok=True, final_message="B ran")

    spies = {"claude-cli": SpyA(), "codex": SpyB()}
    monkeypatch.setattr(runner_mod, "get_backend", lambda n: spies[n])

    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        # 두 후보를 폴오버 순서로 핀(role_priority 는 backends_for 최상단에서 그대로 사용).
        role_priority={"backend-developer": ["claude-cli", "codex"]},
        budget=None,
    )
    board = Board(cfg.project_dir)
    _run(board.init("s", {}))

    # 첫 후보(A) 결과 JSON 을 디스크에 둬서, 폴오버 성공 시 두 번째 후보 결과가 신선하게 읽히도록
    # 한다. write_agent_block 첫 호출만 raise 시켜 후보 A 처리를 비-백엔드 예외로 깨뜨린다.
    state = {"calls": 0}
    orig_write = board.write_agent_block

    def flaky_write(role, title, body):
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("processing boom (non-backend)")
        return orig_write(role, title, body)

    board.write_agent_block = flaky_write

    # 두 번째 후보(B)가 쓰는 결과 JSON 을 미리 둘 수 없으니, run_role(SpyB) 성공 후
    # _read_result 가 성공으로 합성하도록 result_required=False 페이즈가 아닌 dev 라면 결과파일이
    # 필요하다. 따라서 SpyB 가 결과파일을 쓰게 한다.
    role = "backend-developer"
    from orchestrator.config import ROLES

    spec = ROLES[role]
    result_rel = f".orchestrator/results/{role}__U1.json"
    result_path = cfg.project_dir / result_rel

    async def spy_b_run(req):
        run_calls.append("B")
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"status": "dev_done", "artifacts": ["src/x.py"]}), encoding="utf-8"
        )
        return RoleResult(ok=True, final_message="B ran")

    spies["codex"].run_role = spy_b_run

    out = _run(runner_mod.Runner(cfg, board).run_role(role, {"id": "U1"}))

    # 폴오버가 일어나 두 번째 후보(B)가 실제로 호출됐고, role 은 성공으로 끝나야 한다.
    assert "B" in run_calls, f"second candidate should have run; calls={run_calls}"
    assert out["_ok"] is True, out
    assert out["status"] == "dev_done"
    assert "src/x.py" in out["artifacts"]
    _ = spec  # spec 사용 표시(가독성)


def test_all_candidates_processing_exception_yields_failed_not_propagate(
    tmp_path: Path, sample_spec_path, monkeypatch
):
    # 모든 후보 처리가 비-백엔드 예외로 실패해도 예외가 gather 로 전파되지 않고 실패 결과를 돌려야 한다.
    class Spy:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            return RoleResult(ok=True)

    monkeypatch.setattr(runner_mod, "get_backend", lambda n: Spy())

    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p2",
        role_priority={"backend-developer": ["claude-cli", "codex"]},
        budget=None,
    )
    board = Board(cfg.project_dir)
    _run(board.init("s", {}))

    # 모든 후보의 처리 단계(write_agent_block)가 항상 예외 → 둘 다 실패.
    def always_boom(role, title, body):
        raise RuntimeError("always boom")

    board.write_agent_block = always_boom

    # run_role 이 예외를 전파하지 않고 실패 outcome 을 돌려줘야 한다(gather 형제 취소 방지).
    out = _run(runner_mod.Runner(cfg, board).run_role("backend-developer", {"id": "U1"}))
    assert out["_ok"] is False
    assert out["status"] == "failed"
