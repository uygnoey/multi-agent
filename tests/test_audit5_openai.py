"""감사 5차(2026-05-22) openai_agents 백엔드 수정 회귀 테스트.

대상 파일: backends/openai_agents.py.
모두 오프라인·결정적이며 agents SDK 없이도 모듈 레벨 순수 헬퍼만으로 검증한다
(SDK 미설치 환경에서도 import 가능 — function_tool 데코레이터는 건드리지 않는다).

커버: #3 edit 크기 가드, #4 list_dir 항목 상한, #10 실제 모델 추출, #1 kill 스윕.
"""

from __future__ import annotations

import subprocess
import sys
import time

from orchestrator.backends import openai_agents as oa

# ---------------------------------------------------------------------------
# #3: edit_file 은 read_text() 전에 크기를 검사해 거대 파일을 거부한다(메모리 폭주 방지).
# ---------------------------------------------------------------------------


def test_edit_too_large_at_and_below_cap():
    cap = oa._MAX_EDIT_BYTES
    # 상한 이하/정확히 상한이면 편집 허용(거대 아님).
    assert oa._edit_too_large(0) is False
    assert oa._edit_too_large(cap) is False
    assert oa._edit_too_large(cap - 1) is False


def test_edit_too_large_above_cap():
    cap = oa._MAX_EDIT_BYTES
    # 상한 초과면 거대 → True (로드하지 않고 거부 대상).
    assert oa._edit_too_large(cap + 1) is True


def test_edit_too_large_custom_cap():
    # 상한을 인자로 주입할 수 있다(테스트 용이성).
    assert oa._edit_too_large(11, cap=10) is True
    assert oa._edit_too_large(10, cap=10) is False


def test_edit_max_bytes_matches_read_cap():
    # #3: 편집 상한은 read 상한(~200KB)을 재사용한다 — 의도된 200KB 기준.
    assert oa._MAX_EDIT_BYTES == 200 * 1024


# ---------------------------------------------------------------------------
# #4: list_dir 은 정렬 후 상한까지만 반환하고, 잘렸으면 '... (N more)' 로 안내한다.
# ---------------------------------------------------------------------------


def test_format_dir_listing_sorts_unbounded_when_small():
    names = ["c", "a", "b"]
    out = oa._format_dir_listing(names)
    # 정렬되어 줄바꿈 연결되며, 작으면 절단 표기가 없다.
    assert out == "a\nb\nc"
    assert "more)" not in out


def test_format_dir_listing_caps_and_marks_remaining():
    names = [f"f{i:04d}" for i in range(1200)]
    out = oa._format_dir_listing(names, cap=500)
    lines = out.splitlines()
    # 앞 500개 + 마지막 안내 1줄 = 501줄.
    assert len(lines) == 501
    assert lines[0] == "f0000"  # 정렬된 첫 항목
    assert lines[-1] == "... (700 more)"  # 1200 - 500


def test_format_dir_listing_exactly_at_cap_no_truncation():
    names = [f"e{i:03d}" for i in range(500)]
    out = oa._format_dir_listing(names, cap=500)
    # 정확히 상한이면 절단 안내가 붙지 않는다.
    assert "more)" not in out
    assert len(out.splitlines()) == 500


def test_format_dir_listing_default_cap_is_500():
    # 기본 상한은 500 항목이다.
    assert oa._MAX_LIST_ENTRIES == 500
    names = [f"g{i:04d}" for i in range(501)]
    out = oa._format_dir_listing(names)
    assert out.splitlines()[-1] == "... (1 more)"


# ---------------------------------------------------------------------------
# #10: 호출부가 모델을 고정 안 해도 Runner 결과에서 실제 모델을 best-effort 캡처한다.
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, model):
        self.model = model


class _FakeResult:
    """raw_responses / last_agent 만 흉내내는 가짜 결과(SDK 불필요)."""

    def __init__(self, raw_responses=None, last_agent=None):
        if raw_responses is not None:
            self.raw_responses = raw_responses
        if last_agent is not None:
            self.last_agent = last_agent


def test_extract_model_from_raw_responses():
    result = _FakeResult(raw_responses=[_FakeResp("gpt-5.4-mini")])
    assert oa._extract_model(result) == "gpt-5.4-mini"


def test_extract_model_skips_empty_picks_first_nonempty():
    # 첫 응답 model 이 비어 있으면(None) 다음 비어있지 않은 값을 채택한다.
    result = _FakeResult(raw_responses=[_FakeResp(None), _FakeResp("gpt-5.4")])
    assert oa._extract_model(result) == "gpt-5.4"


def test_extract_model_from_last_agent_string():
    # raw_responses 가 없을 때 last_agent.model 이 문자열이면 사용한다.
    class _Agent:
        model = "gpt-4.1"

    result = _FakeResult(last_agent=_Agent())
    assert oa._extract_model(result) == "gpt-4.1"


def test_extract_model_ignores_non_string_last_agent_model():
    # last_agent.model 이 객체(문자열 아님)면 무시하고 None 을 반환한다.
    class _ModelObj:
        pass

    class _Agent:
        model = _ModelObj()

    result = _FakeResult(last_agent=_Agent())
    assert oa._extract_model(result) is None


def test_extract_model_none_when_unknown():
    # 아무 단서도 없으면 None — 가짜 모델명을 날조하지 않는다.
    assert oa._extract_model(_FakeResult()) is None


def test_extract_model_robust_to_garbage_result():
    # 예상치 못한 결과 형태에도 예외 없이 None 으로 떨어진다(guarded getattr).
    assert oa._extract_model(object()) is None
    assert oa._extract_model(None) is None


def test_extracted_model_drives_cost_estimate():
    # #10: 추출된 실제 모델이 비용 추정에 쓰여, 모델 미고정이어도 cost 가 채워진다.
    model = oa._extract_model(_FakeResult(raw_responses=[_FakeResp("gpt-4o-mini")]))
    assert model == "gpt-4o-mini"
    est = oa._estimate_openai_cost(model, 1_000_000, 1_000_000)
    # gpt-4o-mini = (0.15, 0.6) → 0.15 + 0.6 = 0.75
    assert est == 0.75


# ---------------------------------------------------------------------------
# #1: _kill_process_group 은 부모가 graceful 종료해도 마지막 그룹 SIGKILL 로 잔존
# 자식(SIGTERM 무시)을 일소한다.
# ---------------------------------------------------------------------------


def test_kill_process_group_safe_on_none():
    # proc 이 None 이면 예외 없이 무시한다.
    oa._kill_process_group(None)


def test_kill_process_group_terminates_running_proc():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    oa._kill_process_group(proc, grace=2.0)
    assert proc.poll() is not None


def test_kill_process_group_sweeps_sigterm_ignoring_straggler():
    # 부모 셸은 SIGTERM 에 곧바로 죽지만(graceful), trap 으로 SIGTERM 을 무시하는 자식을
    # 백그라운드로 띄운다. 마지막 그룹 SIGKILL 스윕이 없으면 그 자식이 살아남는다(#1).
    import os

    # 자식: SIGTERM(15) 을 trap 으로 무시하고 오래 잔다 → SIGKILL 로만 죽는다.
    child = "trap '' TERM; sleep 30 & echo $! ; wait"
    # 부모: 자식을 background 로 띄우고 그 PID 를 stdout 에 남긴 뒤, 자신은 SIGTERM 에 즉시 죽음.
    parent_cmd = f"bash -c {child!r} & echo $!; sleep 30"
    proc = subprocess.Popen(
        parent_cmd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # 부모/자식이 PID 를 출력할 시간을 잠깐 준다.
    time.sleep(0.5)
    # graceful 종료 경로를 타도록 충분한 유예를 준다(부모는 SIGTERM 에 곧 죽는다).
    oa._kill_process_group(proc, grace=3.0)
    time.sleep(0.5)
    # stdout 에서 PID 들을 회수한다(첫 줄: 자식 래퍼 bash, 다음 줄: 실제 sleep 자식).
    try:
        raw = proc.stdout.read() if proc.stdout else b""
    except Exception:
        raw = b""
    pids = []
    for tok in raw.decode("utf-8", "replace").split():
        if tok.isdigit():
            pids.append(int(tok))
    # 그룹째 SIGKILL 스윕이 동작했다면, 캡처된 PID 중 살아있는 게 없어야 한다.
    survivors = []
    for pid in pids:
        try:
            os.kill(pid, 0)
            survivors.append(pid)
            os.kill(pid, 9)  # 테스트가 좀비를 남기지 않도록 정리
        except ProcessLookupError:
            pass
        except PermissionError:
            survivors.append(pid)
    assert not survivors, f"straggler(s) survived the group SIGKILL sweep: {survivors}"
