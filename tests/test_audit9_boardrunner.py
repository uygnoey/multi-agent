"""audit9 회귀: board 손상-방어/누적-방어/락 분리/타임스탬프/원자적 쓰기,
runner BOM JSON·TOCTOU unlink·result-path 살균·done-stage 보존·백오프 지터.

모두 오프라인·tmp_path 전용(실제 백엔드/네트워크 없음). 검증 대상:
- board: 외부 손상(board.json 의 unit 에 id/status 누락)에도 변형이 KeyError 로 죽지 않음.
- board: 손상된 누적값(total_cost/total_tokens/per-agent 가 문자열)에도 add 가 TypeError 로 안 죽음.
- board: events/directives append 가 _flush 와 다른 락(_log_lock)을 쓴다.
- board: 타임스탬프에 날짜가 포함된다(자정/장기 run 단조성).
- board: report.md / DELIVERABLES*.md 가 원자적으로 쓰인다(임시파일 잔재 없음).
- runner: result JSON 의 UTF-8 BOM 을 허용해 정상 파싱.
- runner: result_path 가 .exists() 후 사라져도 unlink 가 죽지 않음(missing_ok).
- runner: raw unit id 의 '/'·'..' 가 살균되어 result 파일이 results 디렉터리 밖을 못 가리킨다.
- runner: done-stage 로깅 실패가 성공한 outcome 을 'failed' 로 오보하지 않는다.
- runner: 재시도 백오프에 지터가 더해진다(캡 이하).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from orchestrator import runner as runner_mod
from orchestrator.backends.base import RoleResult
from orchestrator.board import DONE, Board, _safe_unit_id
from orchestrator.config import RunConfig


def _run(coro):
    return asyncio.run(coro)


# ── board: 손상 보드(id/status 누락)에서도 변형이 KeyError 로 죽지 않음 (#1) ──────


def test_set_status_skips_corrupt_unit_missing_id(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        # board.json 이 외부에서 손상되어 id 없는 unit 이 끼어든 상황 모사
        b._data["units"].insert(0, {"title": "no-id", "status": "todo"})
        b._data["units"].append({"id": "U2"})  # status/notes 누락
        # KeyError 없이 정상 unit 을 갱신할 수 있어야 한다
        await b.set_status("U1", DONE, note="done!")
        # status 누락 unit 에 대한 갱신도 KeyError 없이 동작(notes 누락도 setdefault 로 방어)
        await b.set_status("U2", DONE, note="ok")
        return b

    b = _run(scenario())
    units = {u.get("id"): u for u in b._data["units"] if u.get("id")}
    assert units["U1"]["status"] == DONE
    assert "done!" in units["U1"]["notes"]
    assert units["U2"]["status"] == DONE
    assert units["U2"]["notes"] == ["ok"]


def test_add_artifacts_and_test_status_skip_corrupt_units(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        b._data["units"].insert(0, {"title": "no-id"})  # id 누락
        b._data["units"].append({"id": "U3"})  # artifacts/test_status 누락
        await b.add_artifacts("U1", ["src/a.py"])
        await b.add_artifacts("U3", ["src/b.py"])  # artifacts 누락 → setdefault
        await b.set_test_status("U1", "pass")
        await b.set_test_status("U3", "pass")
        return b

    b = _run(scenario())
    units = {u.get("id"): u for u in b._data["units"] if u.get("id")}
    assert "src/a.py" in units["U1"]["artifacts"]
    assert units["U1"]["test_status"] == "pass"
    assert "src/b.py" in units["U3"]["artifacts"]
    assert units["U3"]["test_status"] == "pass"


def test_add_units_existing_set_ignores_corrupt_unit_missing_id(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 손상된 id-누락 unit 이 이미 보드에 있어도 add_units 가 KeyError 없이 진행
        b._data["units"].append({"title": "corrupt-no-id"})
        await b.add_units([{"id": "U1", "title": "t"}])
        return b

    b = _run(scenario())
    assert any(u.get("id") == "U1" for u in b._data["units"])


def test_write_report_and_deliverables_survive_corrupt_units(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        b._data["units"].insert(0, {"title": "no-id-no-status"})  # id/status 누락
        return b

    b = _run(scenario())
    # KeyError 없이 리포트/산출물 문서를 생성해야 한다(복구성 보장)
    report = b.write_report()
    assert report.exists()
    written = b.write_deliverables()
    assert "docs/DELIVERABLES.md" in written


# ── board: 손상된 누적값(문자열)에도 add 가 TypeError 로 안 죽음 (#2) ───────────


def test_add_cost_coerces_corrupt_existing_total(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 외부 손상: total_cost_usd 가 숫자가 아니라 문자열
        b._data["total_cost_usd"] = "corrupt"
        await b.add_cost(1.25)  # TypeError 없이 누적되어야 함(손상값은 0 으로 코어션)
        return b

    b = _run(scenario())
    assert b._data["total_cost_usd"] == 1.25


def test_agent_update_coerces_corrupt_cost_and_tokens(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 외부 손상: per-agent cost_usd / tokens 와 total_tokens 가 문자열
        agents = b._data.setdefault("agents", {})
        agents["backend-developer"] = {
            "status": "idle",
            "calls": 0,
            "cost_usd": "bad",
            "tokens": "bad",
        }
        b._data["total_tokens"] = "also-bad"
        await b.agent_update("backend-developer", cost_add=0.5, tokens_add=10)
        return b

    b = _run(scenario())
    a = b._data["agents"]["backend-developer"]
    assert a["cost_usd"] == 0.5  # "bad" → 0 으로 코어션 후 0.5 누적
    assert a["tokens"] == 10
    assert b._data["total_tokens"] == 10


# ── board: events/directives 가 _flush 와 분리된 락을 쓴다 (#3) ─────────────────


def test_log_lock_separate_from_main_lock(tmp_path: Path):
    b = Board(tmp_path)
    assert b._log_lock is not b._lock

    async def scenario():
        await b.init("spec", {})
        # 메인 락(_lock)을 잡고 있어도 log_event/append_directive 는 _log_lock 을 쓰므로
        # 데드락/블록 없이 진행되어야 한다.
        async with b._lock:
            await asyncio.wait_for(b.log_event("x", "while-holding-main-lock"), timeout=2.0)
            await asyncio.wait_for(b.append_directive("pm", "directive"), timeout=2.0)

    _run(scenario())
    assert "while-holding-main-lock" in b.events_path.read_text(encoding="utf-8")
    assert "directive" in b.directives_path.read_text(encoding="utf-8")


# ── board: 타임스탬프에 날짜가 포함된다 (#4) ──────────────────────────────────

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def test_timestamps_include_date(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.log_event("who", "event-msg")
        await b.append_directive("pm", "directive-body")
        b.write_agent_block("backend-developer", "TITLE", "body")
        return b

    b = _run(scenario())
    assert _DATE_RE.search(b.events_path.read_text(encoding="utf-8"))
    assert _DATE_RE.search(b.directives_path.read_text(encoding="utf-8"))
    log = (b.agents_dir / "backend-developer.log").read_text(encoding="utf-8")
    assert _DATE_RE.search(log)


# ── board: report.md / DELIVERABLES*.md 가 원자적으로 쓰인다 (#5) ───────────────


def test_report_and_deliverables_written_atomically(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        return b

    b = _run(scenario())
    report = b.write_report()
    assert report.exists()
    # 임시파일(.tmp) 잔재가 남으면 안 된다(원자적 교체 후 정리됨)
    assert not (b.orch_dir / "report.md.tmp").exists()

    written = b.write_deliverables()
    docs_dir = b.project_dir / "docs"
    assert (docs_dir / "DELIVERABLES.md").exists()
    assert not (docs_dir / "DELIVERABLES.md.tmp").exists()
    assert not (docs_dir / "DELIVERABLES.ko.md.tmp").exists()
    _ = written


# ── runner: result JSON 의 UTF-8 BOM 허용 (#7) ────────────────────────────────


def test_read_result_accepts_utf8_bom(tmp_path: Path):
    rp = tmp_path / "r.json"
    # 선행 UTF-8 BOM 을 붙인 유효 JSON (예전 decode('utf-8') 는 이를 거부했었다)
    rp.write_bytes(b"\xef\xbb\xbf" + json.dumps({"status": "dev_done"}).encode("utf-8"))
    res = RoleResult(ok=True, final_message="ok")
    out = runner_mod.Runner._read_result(rp, res, result_required=True, phase="dev", role="x")
    assert out["status"] == "dev_done"
    assert out["_ok"] is True


# ── runner: result_path TOCTOU unlink (missing_ok) (#8) ───────────────────────


def test_candidate_unlink_survives_missing_result_file(
    tmp_path: Path, sample_spec_path, monkeypatch
):
    # result_path 가 .exists() 통과 후 백엔드 시작 전에 사라져도 unlink(missing_ok)로 죽지 않고
    # 정상적으로 진행되어야 한다. flaky FS 를 직접 만들기 어려우니, 결과파일을 미리 두고 백엔드가
    # 새 결과를 쓰게 해 unlink 경로를 통과시킨다(회귀 sanity).
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        role_priority={"backend-developer": ["codex"]},
        budget=None,
    )
    board = Board(cfg.project_dir)
    _run(board.init("s", {}))

    role = "backend-developer"
    result_rel = f".orchestrator/results/{role}__U1.json"
    result_path = cfg.project_dir / result_rel
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps({"status": "stale"}), encoding="utf-8")

    class Spy:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            # exists() 와 unlink 사이에 외부가 먼저 지운 상황 모사
            result_path.unlink(missing_ok=True)
            result_path.write_text(json.dumps({"status": "dev_done"}), encoding="utf-8")
            return RoleResult(ok=True, final_message="ok")

    monkeypatch.setattr(runner_mod, "get_backend", lambda n: Spy())
    out = _run(runner_mod.Runner(cfg, board).run_role(role, {"id": "U1"}))
    assert out["status"] == "dev_done"
    assert out["_ok"] is True


# ── runner: raw unit id 살균으로 result 파일이 results 밖을 못 가리킨다 (#9) ─────


def test_malicious_unit_id_sanitized_in_result_path(tmp_path: Path, sample_spec_path, monkeypatch):
    captured = {}

    class Spy:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            captured["result_path"] = Path(req.result_path)
            captured["result_rel"] = req.result_rel
            return RoleResult(ok=True, final_message="ok")

    monkeypatch.setattr(runner_mod, "get_backend", lambda n: Spy())
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        role_priority={"backend-developer": ["codex"]},
        budget=None,
    )
    board = Board(cfg.project_dir)
    _run(board.init("s", {}))

    # traversal 을 노린 악성 raw id
    _run(runner_mod.Runner(cfg, board).run_role("backend-developer", {"id": "../../etc/passwd"}))

    rp = captured["result_path"].resolve()
    results_dir = (cfg.project_dir / ".orchestrator" / "results").resolve()
    # 살균된 경로는 반드시 results 디렉터리 하위에 있어야 한다(밖으로 탈출 금지)
    assert results_dir in rp.parents, rp
    assert ".." not in captured["result_rel"]
    assert "/" not in _safe_unit_id("../../etc/passwd")


# ── runner: done-stage 로깅 실패가 성공 outcome 을 'failed' 로 오보하지 않음 (#10) ─


def test_done_stage_logging_error_preserves_success(tmp_path: Path, sample_spec_path, monkeypatch):
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        role_priority={"backend-developer": ["codex"]},
        budget=None,
    )
    board = Board(cfg.project_dir)
    _run(board.init("s", {}))

    role = "backend-developer"
    result_rel = f".orchestrator/results/{role}__U1.json"
    result_path = cfg.project_dir / result_rel

    class Spy:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(
                json.dumps({"status": "dev_done", "artifacts": ["src/x.py"]}), encoding="utf-8"
            )
            return RoleResult(ok=True, final_message="ok")

    monkeypatch.setattr(runner_mod, "get_backend", lambda n: Spy())

    # done-stage 에서 호출되는 agent_update(status="idle", ...)만 골라 한 번 폭발시킨다.
    # (start 단계의 agent_update(status="running")은 그대로 통과시켜야 백엔드까지 도달)
    orig_agent_update = board.agent_update

    async def flaky_agent_update(role_, **kw):
        if kw.get("status") == "idle":
            raise RuntimeError("done-stage agent_update boom")
        return await orig_agent_update(role_, **kw)

    board.agent_update = flaky_agent_update

    out = _run(runner_mod.Runner(cfg, board).run_role(role, {"id": "U1"}))
    # 성공한 백엔드 결과가 done-stage 로깅 예외로 'failed' 로 오보되면 안 된다.
    assert out["_ok"] is True, out
    assert out["status"] == "dev_done"
    assert "src/x.py" in out["artifacts"]
    # setup 실패 blocker 가 outcome 에 끼지 않았는지 확인
    assert not any("setup/preflight" in b for b in out.get("blockers", []))


# ── runner: 재시도 백오프에 지터가 더해진다(캡 이하) (#11) ─────────────────────


def test_retry_backoff_has_jitter(tmp_path: Path, sample_spec_path, monkeypatch):
    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)
    # random.random() 을 0 으로 고정 → 지터 계수는 정확히 0.5 가 되어 base 의 절반이 되어야 한다.
    monkeypatch.setattr(runner_mod.random, "random", lambda: 0.0)

    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        role_priority={"backend-developer": ["codex"]},
        budget=None,
        retries=2,
        retry_backoff=4.0,
    )
    board = Board(cfg.project_dir)
    _run(board.init("s", {}))

    class FailBackend:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            return RoleResult(ok=False, error="boom")

    req = object()  # _run_with_retries 는 req 를 backend.run_role 로만 넘긴다
    runner = runner_mod.Runner(cfg, board)
    _run(runner._run_with_retries(FailBackend(), req, "backend-developer", "U1"))

    # retries=2 → 시도 3회, 사이 sleep 2회. base = min(4*2^i, 60), 지터 계수 0.5.
    assert delays, "expected backoff sleeps"
    assert delays[0] == 4.0 * 1 * 0.5  # i=0: base=4 → 2.0
    assert delays[1] == 4.0 * 2 * 0.5  # i=1: base=8 → 4.0
    # 항상 캡(60s) 이하
    assert all(d <= 60.0 for d in delays)


def test_retry_backoff_jitter_upper_bound(tmp_path: Path, sample_spec_path, monkeypatch):
    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)
    # random.random()=1.0 → 지터 계수 1.0 → base 그대로(상한). 캡 이하 보장 확인.
    monkeypatch.setattr(runner_mod.random, "random", lambda: 1.0)

    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        role_priority={"backend-developer": ["codex"]},
        budget=None,
        retries=1,
        retry_backoff=4.0,
    )
    board = Board(cfg.project_dir)
    _run(board.init("s", {}))

    class FailBackend:
        def available(self):
            return (True, "ok")

        async def run_role(self, req):
            return RoleResult(ok=False, error="boom")

    runner = runner_mod.Runner(cfg, board)
    _run(runner._run_with_retries(FailBackend(), object(), "backend-developer", "U1"))
    assert delays[0] == 4.0  # base=4, 계수 1.0


# ── runner: final_message 비-str 도 안전하게 슬라이스 (#12) ────────────────────


def test_read_result_handles_non_str_final_message(tmp_path: Path):
    # final_message 가 list 같은 비-str 이라도 str() 후 슬라이스되어 TypeError 가 안 난다.
    res = RoleResult(ok=False, error="boom")
    res.final_message = ["not", "a", "string"]  # type: ignore[assignment]
    out = runner_mod.Runner._read_result(
        tmp_path / "missing.json", res, result_required=False, phase=None, role=None
    )
    assert out["_ok"] is False
    assert out["notes"]  # str() 격하된 메시지가 들어감
    assert isinstance(out["notes"][0], str)
