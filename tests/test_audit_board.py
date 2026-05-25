"""감사 발견사항(#26~#33, #82~#87, #96, #103) 회귀 테스트.

전부 결정적·오프라인이며 tmp_path 아래에만 파일을 쓴다.
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

from orchestrator.board import (
    DONE,
    Board,
    _coerce_finite_float,
    _coerce_int,
    _json_safe,
    _safe_artifact,
    _truncate_body,
)


def _run(coro):
    return asyncio.run(coro)


# ---- #26/#27: 아티팩트 경량 검증 ----
def test_safe_artifact_helper_filters_unsafe():
    assert _safe_artifact("backend/app.py") == "backend/app.py"
    assert _safe_artifact("  src/x.py  ") == "src/x.py"
    # 절대경로/traversal/드라이브/비-str/빈값은 모두 drop
    assert _safe_artifact("/etc/passwd") is None
    assert _safe_artifact("\\windows\\system32") is None
    assert _safe_artifact("C:\\secret.txt") is None
    assert _safe_artifact("../../escape.py") is None
    assert _safe_artifact("a/../b") is None
    assert _safe_artifact("") is None
    assert _safe_artifact("   ") is None
    assert _safe_artifact(123) is None
    assert _safe_artifact(None) is None


def test_add_artifacts_validates_and_keeps_safe(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        await b.add_artifacts(
            "U1",
            ["backend/a.py", "/abs/bad.py", "../trav.py", 42, "  rel/b.py  "],
        )
        return b

    b = _run(scenario())
    arts = b.units()[0]["artifacts"]
    assert arts == ["backend/a.py", "rel/b.py"]


def test_add_global_artifacts_validates(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_global_artifacts(["docs/x.md", "/abs.md", "../y.md", None])
        return b

    b = _run(scenario())
    assert b.snapshot()["artifacts"] == ["docs/x.md"]


# ---- #28/#29: deliverables 마크다운 이스케이프 ----
def test_deliverables_escapes_unit_heading_and_artifacts(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "Bad | Title"}])
        # add_artifacts 검증을 우회하기 위해 내부 상태에 직접 주입(개행/파이프 포함)
        b._data["units"][0]["artifacts"] = ["line1\nline2", "a|b"]
        return b

    b = _run(scenario())
    b.write_deliverables()
    text = (tmp_path / "docs" / "DELIVERABLES.md").read_text(encoding="utf-8")
    # 제목의 파이프가 이스케이프됨
    assert "Bad \\| Title" in text
    # 아티팩트의 개행이 중화되어 별도 줄로 새지 않음
    assert "line1 line2" in text
    assert "a\\|b" in text


# ---- #30/#33: 본문 크기 제한 ----
def test_truncate_body_helper():
    assert _truncate_body("short") == "short"
    big = "x" * 50000
    out = _truncate_body(big)
    assert len(out) < len(big)
    assert out.endswith("…(truncated)")


def test_write_agent_block_truncates(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    b.write_agent_block("backend-developer", "TASK", "y" * 100000)
    log = (tmp_path / ".orchestrator" / "agents" / "backend-developer.log").read_text(
        encoding="utf-8"
    )
    assert "…(truncated)" in log
    # 원본 10만 글자가 그대로 들어가지 않음
    assert log.count("y") < 100000


def test_append_directive_truncates(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.append_directive("pm", "z" * 100000)
        return b

    b = _run(scenario())
    assert "…(truncated)" in b.directives()


# ---- #82: set_status unknown unit ----
def test_set_status_unknown_unit_logs_warning(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        await b.set_status("NOPE", DONE)
        await b.set_status("U1", DONE)
        return b

    b = _run(scenario())
    events = b.recent_events(50)
    assert "WARNING: unknown unit" in events
    # 존재하는 unit 은 정상 전이 기록
    assert "status=done" in events
    assert b.units()[0]["status"] == DONE


# ---- #83: set_test_status unknown unit ----
def test_set_test_status_unknown_unit_logs_warning(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.set_test_status("GHOST", "pass")
        return b

    b = _run(scenario())
    assert "WARNING: unknown unit" in b.recent_events(50)


# ---- #84: add_artifacts unknown unit ----
def test_add_artifacts_unknown_unit_logs_warning(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_artifacts("MISSING", ["docs/a.md"])
        return b

    b = _run(scenario())
    assert "WARNING: unknown unit" in b.recent_events(50)


# ---- #85: add_cost 비-유한 float 가드 ----
def test_add_cost_ignores_non_finite(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_cost(1.5)
        await b.add_cost(float("nan"))
        await b.add_cost(float("inf"))
        await b.add_cost(float("-inf"))
        await b.add_cost("oops")  # 비-숫자
        await b.add_cost(0.5)
        return b

    b = _run(scenario())
    total = b.snapshot()["total_cost_usd"]
    assert total == 2.0
    assert math.isfinite(total)
    # board.json 에 NaN/Infinity 가 새지 않음 (표준 JSON)
    raw = (tmp_path / ".orchestrator" / "board.json").read_text(encoding="utf-8")
    assert "NaN" not in raw and "Infinity" not in raw
    json.loads(raw)  # 표준 파서로 로드 가능


# ---- #86: agent_update 잘못된 cost/token 가드 ----
def test_coerce_helpers():
    assert _coerce_finite_float(1.5) == 1.5
    assert _coerce_finite_float(float("nan")) == 0.0
    assert _coerce_finite_float(float("inf")) == 0.0
    assert _coerce_finite_float("bad") == 0.0
    assert _coerce_int(3) == 3
    assert _coerce_int("bad") == 0
    assert _coerce_int(None) == 0


def test_agent_update_survives_bad_metadata(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 잘못된 cost/token 메타데이터가 들어와도 예외 없이 진행
        await b.agent_update("backend-developer", cost_add=float("nan"), tokens_add="x")
        await b.agent_update("backend-developer", cost_add=2.0, tokens_add=10)
        return b

    b = _run(scenario())
    a = b.agents()["backend-developer"]
    assert a["cost_usd"] == 2.0
    assert a["tokens"] == 10
    assert math.isfinite(b.snapshot()["total_cost_usd"])


# ---- #87: role 파일명 안전화 ----
def test_agent_log_role_is_sanitized(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    # traversal 시도 role
    b.write_agent_block("../../escape", "T", "body")
    agents_dir = tmp_path / ".orchestrator" / "agents"
    # agents_dir 밖에 escape.log 가 생기지 않음
    assert not (tmp_path / "escape.log").exists()
    assert not (tmp_path.parent / "escape.log").exists()
    # 안전화된 파일만 agents_dir 안에 존재
    logs = list(agents_dir.glob("*.log"))
    assert logs
    for p in logs:
        assert p.parent == agents_dir
    # 동일 안전화 경로로 읽기도 일관됨
    assert "body" in b.agent_log_tail("../../escape")


# ---- #96: units() 깊은 복사 ----
def test_units_returns_deep_copies(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t", "deps": ["D1"], "roles": ["dba"]}])
        await b.add_artifacts("U1", ["docs/a.md"])
        await b.set_status("U1", DONE, note="n1")
        return b

    b = _run(scenario())
    snapshot = b.units()
    # 호출부가 중첩 리스트를 변형해도 보드 상태에 영향 없어야 함
    snapshot[0]["artifacts"].append("HACK")
    snapshot[0]["deps"].append("HACK")
    snapshot[0]["roles"].append("HACK")
    snapshot[0]["notes"].append("HACK")

    fresh = b.units()[0]
    assert "HACK" not in fresh["artifacts"]
    assert "HACK" not in fresh["deps"]
    assert "HACK" not in fresh["roles"]
    assert "HACK" not in fresh["notes"]


# ---- #103: 빈 보드 리포트 ----
def test_write_report_empty_board_not_ok(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    text = b.write_report().read_text(encoding="utf-8")
    assert "no units" in text
    # 평범한 'ok' 결과 줄이 아님
    assert "- result: **ok**" not in text


def test_write_report_ok_with_units(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        await b.set_status("U1", DONE)
        return b

    b = _run(scenario())
    text = b.write_report().read_text(encoding="utf-8")
    assert "- result: **ok**" in text


# ---- #RA-nan: NaN/Inf 가 board.json/snapshot 으로 새지 않음 ----
def test_json_safe_helper_replaces_non_finite():
    # 비-유한 float 만 0.0 으로 치환, 나머지 값/구조는 보존
    out = _json_safe(
        {
            "a": float("nan"),
            "b": float("inf"),
            "c": float("-inf"),
            "d": 1.5,
            "e": [float("nan"), "keep", 2, {"f": float("inf")}],
        }
    )
    assert out["a"] == 0.0 and out["b"] == 0.0 and out["c"] == 0.0
    assert out["d"] == 1.5
    assert out["e"] == [0.0, "keep", 2, {"f": 0.0}]


def test_flush_sanitizes_non_finite_floats(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        # _data 에 임의 NaN/Inf 를 직접 주입(메타데이터 손상 시나리오 모사)
        b._data["units"][0]["bad_metric"] = float("nan")
        b._data["stack"] = {"weird": float("inf"), "neg": float("-inf")}
        b._flush()
        return b

    b = _run(scenario())
    raw = (tmp_path / ".orchestrator" / "board.json").read_text(encoding="utf-8")
    # 표준-비준수 토큰이 파일에 없음 + 표준 파서로 로드 가능
    assert "NaN" not in raw and "Infinity" not in raw
    json.loads(raw)
    # snapshot()/agents() 도 유한값(0.0)으로 살균되어 반환
    snap = b.snapshot()
    assert snap["units"][0]["bad_metric"] == 0.0
    assert math.isfinite(snap["units"][0]["bad_metric"])
    assert snap["stack"]["weird"] == 0.0 and snap["stack"]["neg"] == 0.0


def test_snapshot_and_agents_finite_with_nan(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.agent_update("backend-developer", status="running")
        # per-agent 필드에 직접 NaN 주입 → agents() 가 NaN 토큰을 흘리면 안 됨
        b._data["agents"]["backend-developer"]["bad"] = float("nan")
        return b

    b = _run(scenario())
    # snapshot/agents 모두 표준 JSON round-trip 후 유한값
    assert b.snapshot()["agents"]["backend-developer"]["bad"] == 0.0
    assert b.agents()["backend-developer"]["bad"] == 0.0


# ---- #RA-loglock: per-agent 로그 동시 추가쓰기가 인터리빙 없이 직렬화 ----
def test_agent_log_appends_under_lock(tmp_path: Path):
    import threading

    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    # 동기 락이 존재하고, 두 메서드가 그 락을 사용하는지 확인
    assert isinstance(b._agent_log_lock, type(threading.Lock()))

    # 여러 스레드가 동일 agent 로그에 동시에 블록을 써도 줄/블록이 깨지지 않음.
    # 본문에 개행을 넣어, 락이 없으면 다른 스레드의 줄이 사이에 끼어드는지 검증한다
    # (body-{i}|END 한 줄이 항상 붙어 있어야 함). prefix 중복을 피하려 |END 마커 사용.
    n = 40
    threads = [
        threading.Thread(
            target=b.write_agent_block,
            args=("backend-developer", f"T{i}|END", f"body-{i}|END"),
        )
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log = (tmp_path / ".orchestrator" / "agents" / "backend-developer.log").read_text(
        encoding="utf-8"
    )
    # 모든 블록이 정확히 한 번씩 기록됨(인터리빙으로 누락/중복 없음)
    for i in range(n):
        assert log.count(f"body-{i}|END") == 1
        assert log.count(f"T{i}|END") == 1
    # 블록 구조가 깨지지 않음: 기록된 블록 수 == 스레드 수
    assert log.count("|END\n") == n * 2  # title 줄 + body 줄 각각 n 개
