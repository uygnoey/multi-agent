"""감사 발견사항 #5/#6/#7/#15 회귀 테스트.

- #5  음수 tokens_add 누적 차단(per-agent/total 둘 다 감소 금지)
- #6  unit id sanitize 충돌 시 silent drop 금지(접미사 rename + 경고)
- #7  아티팩트 제어문자/개행 제거(빈/유효하지 않은 항목 drop)
- #15 agent_log_tail 의 seek 기반 tail(전체 파일 미독)

전부 결정적·오프라인이며 tmp_path 아래에만 파일을 쓴다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.board import (
    _MAX_DIRECTIVES_BYTES,
    _TAIL_CHUNK_BYTES,
    Board,
    _safe_artifact,
    _tail_lines,
)


def _run(coro):
    return asyncio.run(coro)


# ---- #5: 음수 tokens_add 는 no-op (토큰은 누적만, 절대 감소하지 않음) ----
def test_agent_update_tokens_ignores_negative(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.agent_update("backend-developer", tokens_add=10)
        await b.agent_update("backend-developer", tokens_add=-7)  # 음수: 무시되어야 함
        return b

    b = _run(scenario())
    a = b.agents()["backend-developer"]
    # per-agent 토큰이 음수로 감소하지 않음 (검증된 케이스: +10 후 -7 → 3 이 되면 안 됨)
    assert a["tokens"] == 10
    # total_tokens 도 동일하게 음수로 감소하지 않음
    assert b.snapshot()["total_tokens"] == 10


def test_agent_update_tokens_negative_never_decreases_total(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.agent_update("dba", tokens_add=100)
        before = b.snapshot()["total_tokens"]
        await b.agent_update("dba", tokens_add=-1000)
        after = b.snapshot()["total_tokens"]
        return before, after

    before, after = _run(scenario())
    assert after == before == 100


def test_agent_update_tokens_positive_still_accumulates(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.agent_update("frontend-developer", tokens_add=5)
        await b.agent_update("frontend-developer", tokens_add=8)
        return b

    b = _run(scenario())
    a = b.agents()["frontend-developer"]
    # 양수는 정상 누적
    assert a["tokens"] == 13
    assert b.snapshot()["total_tokens"] == 13


def test_agent_update_tokens_int_coercion_guard_kept(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 비-정수("3.0"은 int() 실패) → _coerce_int 가 0 으로, 결국 no-op
        await b.agent_update("dba", tokens_add="3.0")
        return b

    b = _run(scenario())
    a = b.agents()["dba"]
    assert a["tokens"] == 0
    assert b.snapshot()["total_tokens"] == 0


# ---- #6: sanitize 충돌은 silent drop 금지 → 접미사 rename + 경고 ----
def test_add_units_sanitize_collision_renames_not_dropped(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # "U/1","U 1","U@1" 모두 "U-1" 로 sanitize → 충돌이지만 보존되어야 함
        await b.add_units(
            [
                {"id": "U/1", "title": "slash"},
                {"id": "U 1", "title": "space"},
                {"id": "U@1", "title": "at"},
            ]
        )
        return b

    b = _run(scenario())
    ids = [u["id"] for u in b.units()]
    # 세 unit 모두 보존되어야 함 (조용히 사라지지 않음)
    assert len(ids) == 3
    assert "U-1" in ids
    # 충돌분은 접미사로 disambiguate
    assert "U-1-2" in ids
    assert "U-1-3" in ids


def test_add_units_collision_records_warnings(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units(
            [
                {"id": "U/1", "title": "slash"},
                {"id": "U 1", "title": "space"},
            ]
        )
        return b

    b = _run(scenario())
    warnings = b.snapshot().get("warnings", [])
    # 충돌/rename 이 보드 경고로 가시화되어야 함
    assert any("collision" in w and "U-1-2" in w for w in warnings)


def test_add_units_exact_duplicate_raw_still_skipped(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 완전히 동일한 raw id 의 중복은 진짜 중복으로 skip 해도 됨
        await b.add_units(
            [
                {"id": "U1", "title": "first"},
                {"id": "U1", "title": "dup"},
            ]
        )
        return b

    b = _run(scenario())
    ids = [u["id"] for u in b.units()]
    assert ids == ["U1"]
    # 동일 raw 중복은 충돌 rename 대상은 아니지만 skip 원인은 경고로 남긴다.
    assert any("duplicate raw id" in w for w in b.snapshot().get("warnings", []))


def test_add_units_collision_with_preexisting_id(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U-1", "title": "existing"}])
        # 이미 보드에 있는 "U-1" 과 sanitize 충돌하는 다른 raw 입력
        await b.add_units([{"id": "U/1", "title": "new"}])
        return b

    b = _run(scenario())
    ids = [u["id"] for u in b.units()]
    assert "U-1" in ids
    # 두 번째 호출에서 충돌 → rename 으로 보존
    assert "U-1-2" in ids


# ---- #7: 아티팩트 제어문자/개행 제거 ----
def test_safe_artifact_strips_control_chars():
    # 개행/탭/CR 이 제거되어야 함
    assert _safe_artifact("src/a\nb.py") == "src/ab.py"
    assert _safe_artifact("src/\tfile.py") == "src/file.py"
    assert _safe_artifact("a\r\nb") == "ab"


def test_safe_artifact_drops_when_empty_after_strip():
    # 제어문자/공백만 있는 경우 빈 문자열 → drop
    assert _safe_artifact("\n\t\r ") is None
    assert _safe_artifact("\x00\x01\x02") is None


def test_safe_artifact_keeps_abs_and_dotdot_checks():
    # 기존 절대경로/'..' 차단 유지
    assert _safe_artifact("/etc/passwd") is None
    assert _safe_artifact("../secret") is None
    assert _safe_artifact("C:\\win") is None
    # 제어문자로 traversal 토큰을 위장해도 제거 후 재검사로 차단
    assert _safe_artifact("..\n/secret") is None


def test_add_artifacts_rejects_newline_paths(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.add_units([{"id": "U1", "title": "t"}])
        # 개행이 박힌 가짜 경로는 제어문자 제거 후 정규화되어 저장
        await b.add_artifacts("U1", ["good/x.py", "bad\npath.py", "\n\t "])
        return b

    b = _run(scenario())
    arts = b.units()[0]["artifacts"]
    # 개행이 제거된 형태로 저장되고, 빈 항목은 drop
    assert "good/x.py" in arts
    assert "badpath.py" in arts
    assert all("\n" not in a for a in arts)
    assert len(arts) == 2


# ---- #15: seek 기반 tail (전체 파일 미독) ----
def test_tail_lines_small_file(tmp_path: Path):
    p = tmp_path / "small.log"
    p.write_text("a\nb\nc\n", encoding="utf-8")
    assert _tail_lines(p, 2) == ["b", "c"]
    assert _tail_lines(p, 10) == ["a", "b", "c"]
    assert _tail_lines(p, 0) == []


def test_tail_lines_large_file_returns_last_n(tmp_path: Path):
    p = tmp_path / "big.log"
    # 청크(128KB)보다 훨씬 큰 로그 생성
    lines = [f"line-{i}" for i in range(200_000)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tail = _tail_lines(p, 5)
    assert tail == [f"line-{i}" for i in range(199_995, 200_000)]


def test_tail_lines_large_single_line_keeps_tail_segment(tmp_path: Path):
    p = tmp_path / "single.log"
    p.write_text("A" * 200_000, encoding="utf-8")

    tail = _tail_lines(p, 1)

    assert tail == ["A" * _TAIL_CHUNK_BYTES]


def test_tail_lines_partial_first_line_dropped(tmp_path: Path):
    # 청크 시작이 줄 중간이면 첫 불완전 줄은 버려져야 함(끝 줄만 정확히 반환).
    p = tmp_path / "big.log"
    lines = [f"row-{i:08d}" for i in range(100_000)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tail = _tail_lines(p, 3)
    # 반환된 줄은 모두 온전한 줄이어야 함
    assert tail == ["row-00099997", "row-00099998", "row-00099999"]


def test_tail_lines_decode_errors_graceful(tmp_path: Path):
    p = tmp_path / "bin.log"
    # 유효하지 않은 UTF-8 바이트가 섞여도 죽지 않고 graceful 하게 처리
    p.write_bytes(b"ok1\n\xff\xfe bad bytes\nok2\n")
    tail = _tail_lines(p, 2)
    assert tail[-1] == "ok2"


def test_agent_log_tail_uses_seek_tail(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 여러 줄을 per-agent 로그에 기록
        for i in range(50):
            await b.agent_update("backend-developer", activity=f"act-{i}")
        return b

    b = _run(scenario())
    out = b.agent_log_tail("backend-developer", n=3)
    tail_lines = out.splitlines()
    assert len(tail_lines) == 3
    # 마지막 활동들이 포함되어야 함
    assert "act-49" in out
    assert "act-47" in out


def test_agent_log_tail_missing_file(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    # 로그 파일이 없으면 빈 문자열
    assert b.agent_log_tail("nonexistent-role") == ""


# ---- #21: directives() 는 매 프롬프트에 주입되므로 끝(최신)에서 상한까지만 읽는다 ----
def test_directives_bounded_and_keeps_latest(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        # 상한(16KB)을 훌쩍 넘기도록 여러 디렉티브를 누적.
        for i in range(400):
            await b.append_directive("pm", f"directive-{i} " + "x" * 200)
        return b

    b = _run(scenario())
    out = b.directives()
    # 반환 크기는 상한 + 마커/첫 줄 여유분 정도로 묶여야 한다(파일 전체가 아님).
    assert len(out.encode("utf-8")) <= _MAX_DIRECTIVES_BYTES + 200
    # 가장 최근 디렉티브는 반드시 포함.
    assert "directive-399" in out
    # 잘렸으니 생략 마커가 붙는다.
    assert "오래된 directives 생략" in out
    # 가장 오래된 디렉티브는 잘려 나갔다.
    assert "directive-0 " not in out


def test_directives_small_returned_whole(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        await b.append_directive("pl", "be careful")
        return b

    b = _run(scenario())
    out = b.directives()
    assert "be careful" in out
    # 작으면 절단 마커가 없다.
    assert "생략" not in out


def test_directives_empty_when_absent(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        return b

    b = _run(scenario())
    assert b.directives() == ""


# ---- #20: recent_events 도 seek-tail 로 마지막 n 줄만 반환 ----
def test_recent_events_tail(tmp_path: Path):
    async def scenario():
        b = Board(tmp_path)
        await b.init("spec", {})
        for i in range(100):
            await b.log_event("board", f"evt-{i}")
        return b

    b = _run(scenario())
    out = b.recent_events(3)
    lines = out.splitlines()
    assert len(lines) == 3
    # 마지막 이벤트가 포함되고, 오래된 것은 빠진다.
    assert "evt-99" in out
    assert "evt-0 " not in out


def test_recent_events_missing_file(tmp_path: Path):
    # events.log 가 없으면 빈 문자열(예외 없이).
    b = Board(tmp_path)
    assert b.recent_events(5) == ""
