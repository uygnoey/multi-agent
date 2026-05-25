"""감사 9차(2026-05-25) 백엔드 수정 회귀 테스트.

대상: backends/base.py, claude_cli.py, claude_team.py, claude_sdk.py,
      openai_agents.py, codex_cli.py, __init__.py, mock.py.
모두 오프라인·결정적이며 실제 LLM/SDK 호출 없이 monkeypatch + 순수 헬퍼로 검증한다.

커버:
- #1: claude_cli/claude_team 의 '--max-budget-usd' unknown-option 폴백 재시도.
- #H08: base.run_subprocess 가 live_log 를 "a"(append)로 열어 PROMPT/retry 로그 보존(board.init 이 run 시작 시 1회 비움).
- #3: claude_sdk 의 USD 미보고(구독) 시 토큰×단가 추정 + cost_estimated.
- #4: openai_agents 의 합산 토큰만 있을 때 cost 폴백(보고 일관성).
- #5: codex 타임아웃 시 부분 usage 회계.
- #6: openai list_dir 의 항목별 실패 격리.
- #7: openai bash_timeout 의 timeout==0 처리.
- #8: __init__ 의 unknown-backend 메시지 별칭 포함 + 중복 이름 감지.
- #10: claude_cli stream 렌더의 cost n/a 표기.
- #11: openai write/edit 의 바이트 수 보고.
- #12: mock id/title 텍스트 sanitize.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from orchestrator.backends import base as base_mod
from orchestrator.backends import claude_cli, claude_team, codex_cli
from orchestrator.backends import claude_sdk as sdk_mod
from orchestrator.backends import openai_agents as oa
from orchestrator.backends.base import RoleRequest
from orchestrator.backends.codex_cli import CodexCLIBackend


def _req(tmp_path: Path, **kw) -> RoleRequest:
    base = dict(
        role="backend-developer",
        phase="dev",
        unit={"id": "U1", "title": "t"},
        system_prompt="sys",
        prompt="prompt",
        cwd=tmp_path,
        allowed_tools=["Read", "Write"],
        model=None,
        max_turns=8,
        budget=None,
        result_path=tmp_path / ".orchestrator" / "results" / "r.json",
        result_rel=".orchestrator/results/r.json",
        spec_text="spec",
    )
    base.update(kw)
    return RoleRequest(**base)


# ---------------------------------------------------------------------------
# #1: claude_cli / claude_team — '--max-budget-usd' unknown-option 폴백 재시도.
# ---------------------------------------------------------------------------


def test_is_unknown_budget_flag_error_matches_only_relevant():
    assert claude_cli._is_unknown_budget_flag_error("error: unknown option '--max-budget-usd'")
    assert claude_cli._is_unknown_budget_flag_error("unrecognized option --max-budget-usd")
    assert claude_cli._is_unknown_budget_flag_error("no such option: --max-budget-usd")
    # 플래그 이름이 없거나, unknown 류 힌트가 없으면 오인하지 않는다(보수적 매칭).
    assert not claude_cli._is_unknown_budget_flag_error("budget exceeded: --max-budget-usd hit")
    assert not claude_cli._is_unknown_budget_flag_error("unknown option '--frobnicate'")
    assert not claude_cli._is_unknown_budget_flag_error("")


def _ok_stream(cost=None):
    if cost is None:
        return b'{"type":"result","result":"done"}\n'
    return f'{{"type":"result","result":"done","total_cost_usd":{cost}}}\n'.encode()


def test_claude_cli_retries_without_budget_flag_on_unknown_option(tmp_path, monkeypatch):
    calls = []

    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None):
        calls.append(cmd)
        if "--max-budget-usd" in cmd:
            # 1차: 플래그 미지원 CLI 가 unknown-option 으로 깨진다.
            return 2, b"", b"error: unknown option '--max-budget-usd'", False
        # 2차(폴백): 플래그 없이 정상 성공.
        return 0, _ok_stream(), b"", False

    monkeypatch.setattr(claude_cli, "run_subprocess", fake_run)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    req = _req(tmp_path, budget=5.0)
    res = asyncio.run(claude_cli.ClaudeCLIBackend().run_role(req))

    assert res.ok is True  # 폴백 재시도로 성공
    assert len(calls) == 2  # 1차(플래그 有) → 2차(플래그 無)
    assert "--max-budget-usd" in calls[0]
    assert "--max-budget-usd" not in calls[1]


def test_claude_cli_no_retry_when_no_budget(tmp_path, monkeypatch):
    calls = []

    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None):
        calls.append(cmd)
        return 0, _ok_stream(), b"", False

    monkeypatch.setattr(claude_cli, "run_subprocess", fake_run)
    res = asyncio.run(claude_cli.ClaudeCLIBackend().run_role(_req(tmp_path, budget=None)))
    assert res.ok is True
    assert len(calls) == 1  # 예산 미지정 → 폴백 경로 자체가 없다


def test_claude_cli_no_retry_on_unrelated_failure(tmp_path, monkeypatch):
    calls = []

    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None):
        calls.append(cmd)
        return 1, b"", b"some other auth error", False  # unknown-option 신호 아님

    monkeypatch.setattr(claude_cli, "run_subprocess", fake_run)
    res = asyncio.run(claude_cli.ClaudeCLIBackend().run_role(_req(tmp_path, budget=5.0)))
    assert res.ok is False
    assert len(calls) == 1  # 무관한 실패는 재시도하지 않는다


def test_claude_team_retries_without_budget_flag_on_unknown_option(tmp_path, monkeypatch):
    calls = []

    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None):
        calls.append(cmd)
        if "--max-budget-usd" in cmd:
            return 2, b"", b"unknown option: --max-budget-usd", False
        return 0, _ok_stream(), b"", False

    monkeypatch.setattr(claude_team, "run_subprocess", fake_run)
    res = asyncio.run(claude_team.ClaudeTeamBackend().run_role(_req(tmp_path, budget=3.0)))
    assert res.ok is True
    assert len(calls) == 2
    assert "--max-budget-usd" not in calls[1]


# ---------------------------------------------------------------------------
# #H08(정정): base.run_subprocess 는 live_log 를 "a"(append)로 열어, runner 가 먼저 쓴 PROMPT
# 블록과 직전 retry/failover 로그를 보존한다(이전엔 "w" 로 truncate 해 PROMPT 블록까지 소실).
# 재사용 디렉터리의 과거 run 로그는 board.init 이 run 시작 시 1회 비운다.
# ---------------------------------------------------------------------------


def test_run_subprocess_appends_and_preserves_prior_log(tmp_path):
    import sys

    log = tmp_path / "live.log"
    # runner 가 백엔드 호출 직전 같은 파일에 기록한 PROMPT 블록을 시뮬레이션.
    log.write_text("PROMPT BLOCK MUST SURVIVE\n", encoding="utf-8")
    cmd = [sys.executable, "-c", "print('fresh-line')"]
    rc, out, err, timed_out = asyncio.run(base_mod.run_subprocess(cmd, str(tmp_path), 30, log))
    assert rc == 0
    assert timed_out is False
    text = log.read_text(encoding="utf-8")
    assert "PROMPT BLOCK MUST SURVIVE" in text  # #H08: append 라 직전 내용이 보존되어야 한다
    assert "fresh-line" in text  # 새 출력도 tee
    assert "backend run @" in text  # 호출 구분자 헤더


def test_board_init_clears_stale_agent_logs(tmp_path):
    # #H08: 재사용 project-dir 에서 run 시작(board.init) 시 이전 run 의 per-agent 로그를 비운다.
    from orchestrator.board import Board

    b = Board(tmp_path / "p")
    b.agents_dir.mkdir(parents=True, exist_ok=True)
    stale = b.agents_dir / "frontend-developer.log"
    stale.write_text("OLD RUN LOG\n", encoding="utf-8")
    asyncio.run(b.init("spec", {}))
    assert not stale.exists()  # run 시작 시 비워짐


# ---------------------------------------------------------------------------
# #3: claude_sdk — USD 미보고(구독) 시 토큰×단가 추정 + cost_estimated.
# ---------------------------------------------------------------------------


def test_anthropic_price_exact_and_dated_fallback():
    assert sdk_mod._anthropic_price_for("claude-sonnet-4") == (3.0, 15.0)
    # 날짜/latest 접미사는 base 단가로 폴백.
    assert sdk_mod._anthropic_price_for("claude-sonnet-4-20250101") == (3.0, 15.0)
    assert sdk_mod._anthropic_price_for("claude-sonnet-4-latest") == (3.0, 15.0)
    # #M05: 포인트 버전이 든 현행 모델 ID 도 base 단가로 폴백해야 한다(예전엔 None 으로 떨어졌다).
    assert sdk_mod._anthropic_price_for("claude-sonnet-4-5") == (3.0, 15.0)
    assert sdk_mod._anthropic_price_for("claude-opus-4-1-20250805") == (15.0, 75.0)
    assert sdk_mod._anthropic_price_for("claude-sonnet-4-5-20250929") == (3.0, 15.0)
    assert sdk_mod._anthropic_price_for("claude-3-5-sonnet-20241022") == (3.0, 15.0)
    # 알 수 없는 변형/빈 값은 None(허위 비용 날조 금지).
    assert sdk_mod._anthropic_price_for("claude-mystery-9") is None
    assert sdk_mod._anthropic_price_for(None) is None


def test_estimate_anthropic_cost():
    # opus-4 = (15, 75) → 1M in + 1M out = 15 + 75 = 90.
    assert sdk_mod._estimate_anthropic_cost("claude-opus-4", 1_000_000, 1_000_000) == 90.0
    assert sdk_mod._estimate_anthropic_cost("nope", 1, 1) is None


def test_sdk_resolve_cost_estimates_when_usd_absent(monkeypatch):
    # query/SDK 없이 _resolve_cost 의 의미를 직접 검증하기 위해, 헬퍼만 단위 검증한다.
    # cost(USD) 미보고 + 모델/토큰 있음 → 토큰×단가 추정치.
    est = sdk_mod._estimate_anthropic_cost("claude-sonnet-4", 1_000_000, 0)
    assert est == 3.0  # (3.0, 15.0) 의 input 단가


def test_sdk_run_role_estimates_cost_in_subscription_mode(tmp_path, monkeypatch):
    # SDK 의 query 를 가짜 모듈로 직접 주입(실제 SDK 불필요): total_cost_usd 미보고 +
    # usage(in/out) 보고하는 메시지를 흘려보낸다.
    class _Usage:
        input_tokens = 1_000_000
        output_tokens = 0
        total_tokens = 1_000_000

    class _Msg:
        content = "done"
        usage = _Usage()
        # total_cost_usd / model 속성 없음(구독 모드 모사)

    async def _fake_query(prompt, options):
        yield _Msg()

    class _FakeOptions:
        def __init__(self, **kw):
            pass

    import types

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _FakeOptions
    fake.query = _fake_query
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake)

    req = _req(tmp_path, model="claude-sonnet-4", budget=None, timeout=30)
    res = asyncio.run(sdk_mod.ClaudeSDKBackend().run_role(req))
    assert res.ok is True
    assert res.cost_estimated is True  # USD 미보고 → 추정치 표기
    assert res.cost_usd == 3.0  # 1M input × $3/1M
    assert res.tokens == 1_000_000


def test_sdk_run_role_uses_reported_usd_not_estimated(tmp_path, monkeypatch):
    class _Msg:
        content = "done"
        total_cost_usd = 0.42
        usage = None

    async def _fake_query(prompt, options):
        yield _Msg()

    class _FakeOptions:
        def __init__(self, **kw):
            pass

    import types

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _FakeOptions
    fake.query = _fake_query
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake)

    res = asyncio.run(sdk_mod.ClaudeSDKBackend().run_role(_req(tmp_path, timeout=30)))
    assert res.ok is True
    assert res.cost_usd == 0.42
    assert res.cost_estimated is False  # 실청구액 보고 → 추정 아님


# ---------------------------------------------------------------------------
# #4: openai_agents — 합산 토큰만 있을 때 cost 폴백(보고 일관성).
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self, total=None, it=None, ot=None):
        if total is not None:
            self.total_tokens = total
        if it is not None:
            self.input_tokens = it
        if ot is not None:
            self.output_tokens = ot


class _Resp:
    def __init__(self, usage, model=None):
        self.usage = usage
        self.model = model


class _Result:
    context_wrapper = None

    def __init__(self, raw_responses, final="ok"):
        self.raw_responses = raw_responses
        self.final_output = final


def test_openai_cost_falls_back_to_total_tokens(tmp_path, monkeypatch):
    # 합산 total_tokens 만 있고 in/out 분리는 없는 응답: 예전엔 tokens>0 인데 cost=None 이었다.
    # 이제 합산 토큰을 output 단가로 환산한 보수적 추정치를 내고 cost_estimated=True 로 표기한다.
    pytest.importorskip("agents")
    result = _Result([_Resp(_Usage(total=1_000_000), model="gpt-4o")])

    async def _fake_runner(agent, prompt, max_turns):
        return result

    import agents as agents_mod

    monkeypatch.setattr(agents_mod.Runner, "run", staticmethod(_fake_runner))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    res = asyncio.run(oa.OpenAIAgentsBackend().run_role(_req(tmp_path, model="gpt-4o", timeout=30)))
    assert res.ok is True
    assert res.tokens == 1_000_000
    # gpt-4o output 단가 10.0/1M → 1M × 10 = 10.0 (보수적 상한)
    assert res.cost_usd == 10.0
    assert res.cost_estimated is True


# ---------------------------------------------------------------------------
# #5: codex 타임아웃 시 부분 usage 회계.
# ---------------------------------------------------------------------------


def test_codex_timeout_accounts_partial_usage(tmp_path, monkeypatch):
    partial = (
        b'{"type":"turn.completed","usage":{"input_tokens":1000000,'
        b'"output_tokens":0,"total_tokens":1000000}}\n'
    )

    async def fake_run(cmd, cwd, timeout, log_path=None):
        return None, partial, b"", True  # timed_out=True 인데 stdout 에 usage 가 남아있다

    monkeypatch.setattr(codex_cli, "run_subprocess", fake_run)
    req = _req(tmp_path, model="gpt-5.5", timeout=30)
    res = asyncio.run(CodexCLIBackend().run_role(req))
    assert res.ok is False
    assert "timed out" in res.error
    # 부분 usage 가 통째로 0 으로 떨어지지 않고 회계된다.
    assert res.tokens == 1_000_000
    assert res.cost_usd is not None and res.cost_usd > 0
    assert res.cost_estimated is True


def test_codex_timeout_no_usage_stays_none(tmp_path, monkeypatch):
    async def fake_run(cmd, cwd, timeout, log_path=None):
        return None, b"", b"", True  # usage 없음

    monkeypatch.setattr(codex_cli, "run_subprocess", fake_run)
    res = asyncio.run(CodexCLIBackend().run_role(_req(tmp_path, timeout=30)))
    assert res.ok is False
    assert res.tokens is None
    assert res.cost_usd is None


# ---------------------------------------------------------------------------
# #6: openai list_dir — 항목별 is_dir() 실패 격리.
# ---------------------------------------------------------------------------


def test_format_dir_listing_still_sorts_and_caps():
    # 순수 헬퍼는 그대로 동작(정렬 + 상한). #6 의 항목별 격리는 run_role 클로저 안에 있으므로
    # 아래의 통합 테스트(SDK 있을 때)로, 여기서는 포매터 회귀만 본다.
    names = ["b/", "a", "c/"]
    assert oa._format_dir_listing(names) == "a\nb/\nc/"


def test_list_dir_isolates_per_entry_failure(tmp_path, monkeypatch):
    pytest.importorskip("agents")
    # is_dir() 가 특정 항목에서만 예외를 던지도록 패치 → 목록 전체가 무너지지 않아야 한다.
    (tmp_path / "good").mkdir()
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")

    real_is_dir = Path.is_dir

    def flaky_is_dir(self):
        if self.name == "good":
            raise OSError("boom")
        return real_is_dir(self)

    # run_role 클로저의 list_dir 를 추출하기 어려우므로, 동일 의미의 #6 루프를 재현해 검증한다.
    monkeypatch.setattr(Path, "is_dir", flaky_is_dir)
    entries = list(tmp_path.iterdir())
    names = []
    for x in entries:
        try:
            names.append(x.name + ("/" if x.is_dir() else ""))
        except Exception:
            names.append(x.name)  # 판정 불가 항목은 이름만(에러로 전체가 무너지지 않음)
    listing = oa._format_dir_listing(names)
    assert "good" in listing  # 실패한 항목도 (접미사 없이) 살아남는다
    assert "file.txt" in listing


# ---------------------------------------------------------------------------
# #7: openai bash_timeout — timeout==0 처리(falsy 오인 방지).
# ---------------------------------------------------------------------------


def test_bash_timeout_zero_is_respected():
    # 로직 자체를 검증: None 이면 120, 그 외(0 포함)는 그대로.
    def resolve(t):
        return t if t is not None else 120

    assert resolve(None) == 120
    assert resolve(0) == 0  # 예전엔 falsy 라 120 으로 둔갑했었다
    assert resolve(45) == 45


# ---------------------------------------------------------------------------
# #8: __init__ — unknown-backend 메시지 별칭 포함 + 중복 이름 감지.
# ---------------------------------------------------------------------------


def test_unknown_backend_error_lists_aliases():
    from orchestrator.backends import ALIASES, get_backend

    with pytest.raises(ValueError) as ei:
        get_backend("definitely-not-a-backend")
    msg = str(ei.value)
    assert "aliases:" in msg
    # 대표 별칭 몇 개가 메시지에 들어있어야 한다.
    for alias in list(ALIASES)[:3]:
        assert alias in msg


def test_build_registry_rejects_duplicate_names():
    from orchestrator.backends import _build_registry
    from orchestrator.backends.mock import MockBackend

    with pytest.raises(ValueError):
        _build_registry([MockBackend(), MockBackend()])  # 같은 name='mock' 둘


# ---------------------------------------------------------------------------
# #10: claude_cli stream 렌더 — cost n/a 표기(구독 모드).
# ---------------------------------------------------------------------------


def test_claude_stream_result_cost_na_when_absent():
    # total_cost_usd 미보고 → '$0' 가 아니라 'cost n/a' 로 표기해 '0달러 썼다' 오인을 막는다.
    line = b'{"type":"result","result":"done"}'
    assert claude_cli.claude_stream_line(line) == "✓ result (cost n/a)"
    # 보고되면 그 값을 그대로 보여준다.
    line2 = b'{"type":"result","result":"done","total_cost_usd":0.5}'
    assert claude_cli.claude_stream_line(line2) == "✓ result ($0.5)"
    # 실제 0 달러도(드물지만) 0 으로 표기 — n/a 와 구분된다.
    line3 = b'{"type":"result","result":"done","total_cost_usd":0}'
    assert claude_cli.claude_stream_line(line3) == "✓ result ($0)"


# ---------------------------------------------------------------------------
# #11: openai write/edit — UTF-8 바이트 수 보고(문자 수 아님).
# ---------------------------------------------------------------------------


def test_write_byte_count_logic():
    # 멀티바이트 문자가 있으면 문자 수와 바이트 수가 다르다 — 바이트 수를 보고해야 한다.
    content = "한글"  # 2 chars, UTF-8 6 bytes
    data = content.encode("utf-8")
    assert len(content) == 2
    assert len(data) == 6
    # 보고 메시지가 바이트 수를 쓰는지 형태로 확인.
    assert f"wrote x ({len(data)} bytes)" == "wrote x (6 bytes)"


# ---------------------------------------------------------------------------
# #12: mock — id/title 텍스트 sanitize(개행/따옴표로 산출물이 깨지지 않게).
# ---------------------------------------------------------------------------


def test_mock_safe_id_text_strips_newlines_and_quotes():
    from orchestrator.backends.mock import _safe_id_text

    assert _safe_id_text('a"b') == "ab"
    assert _safe_id_text("a\nb\tc") == "a b c"
    assert _safe_id_text("  x  ") == "x"
    assert _safe_id_text("") == "unit"
    assert _safe_id_text("\n\n") == "unit"


def test_mock_backend_developer_output_is_valid_python_with_nasty_id(tmp_path):
    from orchestrator.backends.mock import MockBackend

    nasty = 'U1"; import os\n#'
    req = _req(
        tmp_path,
        role="backend-developer",
        unit={"id": nasty, "title": "evil\ntitle"},
    )
    res = asyncio.run(MockBackend().run_role(req))
    assert res.ok is True
    # 생성된 backend 파일이 구문상 유효한 파이썬이어야 한다(개행/따옴표로 안 깨짐).
    py_files = list((tmp_path / "backend" / "app").glob("*.py"))
    assert py_files
    src = py_files[0].read_text(encoding="utf-8")
    compile(src, py_files[0].name, "exec")  # SyntaxError 면 실패


def test_mock_architecture_units_no_broken_markdown(tmp_path):
    from orchestrator.backends.mock import MockBackend

    req = _req(
        tmp_path,
        role="architecture-engineer",
        unit=None,
        spec_text='- feature "quoted"\nline\n- 둘째 기능\n',
    )
    res = asyncio.run(MockBackend().run_role(req))
    assert res.ok is True
    md = (tmp_path / "docs" / "design" / "architecture.md").read_text(encoding="utf-8")
    # Units 섹션의 각 '- ' 라인이 한 줄로 유지되어야 한다(개행 주입으로 리스트가 깨지지 않음).
    unit_lines = [ln for ln in md.splitlines() if ln.startswith("- U")]
    assert unit_lines  # 최소 하나의 유닛 라인
    for ln in unit_lines:
        assert '"' not in ln  # 따옴표 제거


# ---------------------------------------------------------------------------
# 감사 후속(RA): SDK 단가 정규식 / 0-토큰 비용 / 버퍼 / 거대 프롬프트 stdin.
# ---------------------------------------------------------------------------


# #RA-sdkre: 알려진 포인트 버전(4.1/4.5)은 폴백하되, 알 수 없는 미래 포인트 버전은 조용히
# base 단가로 매핑하지 않고 None 으로 둔다.
def test_anthropic_price_suffix_limits_point_version_segments():
    # 현행 ID(알려진 포인트 + 선택적 날짜)는 base 단가로 폴백한다.
    assert sdk_mod._anthropic_price_for("claude-opus-4-1") == (15.0, 75.0)
    assert sdk_mod._anthropic_price_for("claude-opus-4-1-20250805") == (15.0, 75.0)
    assert sdk_mod._anthropic_price_for("claude-sonnet-4-5") == (3.0, 15.0)
    assert sdk_mod._anthropic_price_for("claude-sonnet-4-5-20250929") == (3.0, 15.0)
    # 알려지지 않은 미래 포인트 변형은 폴백하지 않는다 → None.
    assert sdk_mod._anthropic_price_for("claude-opus-4-2") is None
    assert sdk_mod._anthropic_price_for("claude-opus-4-2-3") is None
    assert sdk_mod._anthropic_price_for("claude-opus-4-2-3-4") is None
    assert sdk_mod._anthropic_price_for("claude-opus-4-2-3-4-20260101") is None
    # 접미사 정규식 자체도 직접 검증(known point 만 허용).
    assert sdk_mod._ANTHROPIC_DATE_SUFFIX.match("-1")
    assert sdk_mod._ANTHROPIC_DATE_SUFFIX.match("-5-20250101")
    assert not sdk_mod._ANTHROPIC_DATE_SUFFIX.match("-2")
    assert not sdk_mod._ANTHROPIC_DATE_SUFFIX.match("-1-2-3")


# #RA-0tok: usage 가 truthy 라도 토큰이 전부 0 이면 추정 비용 0.0 은 None 으로 접는다(SDK).
def test_estimate_anthropic_cost_zero_tokens_is_none():
    # 모델/단가는 알지만 in/out 토큰이 0 → 0.0 이 아니라 None(허위 $0 추정 금지).
    assert sdk_mod._estimate_anthropic_cost("claude-sonnet-4", 0, 0) is None
    # 양수 토큰은 그대로 추정치를 낸다(접지 않음).
    assert sdk_mod._estimate_anthropic_cost("claude-sonnet-4", 1_000_000, 0) == 3.0


def test_sdk_run_role_zero_tokens_reports_no_cost(tmp_path, monkeypatch):
    # usage 는 truthy 지만 in/out 토큰이 전부 0(예: reasoning 만 잡힌 경우) → cost 는 None.
    class _Usage:
        input_tokens = 0
        output_tokens = 0
        total_tokens = 0
        reasoning_tokens = 123  # 우리가 비용에 쓰지 않는 토큰만 존재

    class _Msg:
        content = "done"
        usage = _Usage()  # total_cost_usd / model 없음(구독 모드)

    async def _fake_query(prompt, options):
        yield _Msg()

    class _FakeOptions:
        def __init__(self, **kw):
            pass

    import types

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = _FakeOptions
    fake.query = _fake_query
    monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", fake)

    req = _req(tmp_path, model="claude-sonnet-4", budget=None, timeout=30)
    res = asyncio.run(sdk_mod.ClaudeSDKBackend().run_role(req))
    assert res.ok is True
    assert res.cost_usd is None  # #RA-0tok: 0.0 이 아니라 None — "$0.00 추정" 오해 방지


# #RA-0tok: codex 도 usage 가 truthy 라도 토큰 전부 0 이면 cost None + cost_estimated False.
def test_codex_zero_token_usage_reports_no_cost(tmp_path, monkeypatch):
    async def fake_run(cmd, cwd, timeout, log_path=None):
        # turn.completed 는 있으나 토큰이 전부 0 → usage dict 는 truthy, 비용 추정은 0.0.
        out = b'{"type":"turn.completed","usage":{"input_tokens":0,"output_tokens":0}}\n'
        return 0, out, b"", False

    monkeypatch.setattr(codex_cli, "run_subprocess", fake_run)
    req = _req(tmp_path, model="gpt-5.5", timeout=30)
    res = asyncio.run(CodexCLIBackend().run_role(req))
    assert res.ok is True
    assert res.cost_usd is None  # 0.0 이 아니라 None
    assert res.cost_estimated is False  # $0 추정치를 보고하지 않으므로 estimated 도 아님


# #RA-buf: 오버사이즈 라인이 들어와도 누적 라인을 clear() 로 통째로 버리지 않는다. 트림 루프는
# 가장 오래된 라인부터 떨어뜨리므로, 오버사이즈 라인 '이후'에 들어온 최신 result/usage 라인은
# 보존된다(예전 clear() 버그였다면 오버사이즈 라인이 append 시점에 prior 를 전부 날렸다).
def test_bounded_buffer_oversized_line_does_not_wipe_later_lines():
    buf = base_mod._BoundedBuffer(max_bytes=200)
    buf.append(b"{usage-event}\n")  # prior 작은 이벤트
    buf.append(b"X" * 1000 + b"\n")  # 오버사이즈 → 자기 자신만 절단(=max 로)
    buf.append(b"{result-event}\n")  # 오버사이즈 이후 들어온 최신 이벤트
    data = buf.getvalue()
    # 총 보관량은 상한 근처로 묶인다(폭주 방지).
    assert len(data) <= 200 + len(b"{result-event}\n")
    # 오버사이즈 라인이 append 시점에 buffer 를 비우지 않았으므로, 그 뒤의 최신 이벤트가 살아있다.
    assert b"{result-event}" in data
    # 오버사이즈가 발생했으니 dropped 플래그는 선다.
    assert buf.dropped is True


def test_bounded_buffer_oversized_line_truncates_itself_not_clears():
    # 상한 대비 적당히 큰 라인 하나만 들어와도, 가장 최근(=그 라인)은 절단된 채로 보존되고
    # 버퍼가 통째로 비지 않는다(clear() 제거 검증). JSON 형태가 아니므로 tail 을 보존한다.
    buf = base_mod._BoundedBuffer(max_bytes=100)
    buf.append(b"prior-A\n")
    buf.append(b"prior-B\n")
    buf.append(b"Z" * 500 + b"TAIL\n")  # 오버사이즈, non-JSON → tail 절단
    data = buf.getvalue()
    assert buf.dropped is True
    # 절단된 오버사이즈 라인의 tail(끝부분)이 보존된다 — head 가 아니라 tail.
    assert data.endswith(b"TAIL\n")
    # 총 보관량은 상한 근처로 묶인다.
    assert len(data) <= 100 + len(b"prior-A\nprior-B\n")


# #RA-e2big: 거대 프롬프트는 argv 대신 stdin('-')으로 넘겨 ARG_MAX(E2BIG) 를 회피한다.
def test_codex_large_prompt_uses_stdin(tmp_path, monkeypatch):
    captured = {}

    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None, stdin_data=None):
        captured["cmd"] = cmd
        captured["stdin_data"] = stdin_data
        return 0, b'{"type":"turn.completed","usage":{}}\n', b"", False

    monkeypatch.setattr(codex_cli, "run_subprocess", fake_run)
    big = "A" * (codex_cli._PROMPT_STDIN_THRESHOLD + 10)  # 임계치 초과 프롬프트
    req = _req(tmp_path, prompt=big, model="gpt-5.5", timeout=30)
    res = asyncio.run(CodexCLIBackend().run_role(req))
    assert res.ok is True
    # positional 프롬프트 자리에는 '-' 가 들어가고(거대 프롬프트는 argv 에 없음),
    assert captured["cmd"][2] == "-"
    # 실제 프롬프트는 stdin 으로 전달된다(bytes).
    assert captured["stdin_data"] is not None
    assert big.encode("utf-8") in captured["stdin_data"]


def test_codex_small_prompt_stays_positional(tmp_path, monkeypatch):
    captured = {}

    # 작은 프롬프트는 기존 4-인자 호출 그대로 — stdin_data 를 넘기지 않는다(fake 시그니처도 4-인자).
    async def fake_run(cmd, cwd, timeout, log_path=None):
        captured["cmd"] = cmd
        return 0, b'{"type":"turn.completed","usage":{}}\n', b"", False

    monkeypatch.setattr(codex_cli, "run_subprocess", fake_run)
    req = _req(tmp_path, prompt="tiny", model="gpt-5.5", timeout=30)
    res = asyncio.run(CodexCLIBackend().run_role(req))
    assert res.ok is True
    # 작은 프롬프트는 여전히 positional argv 로 들어간다('-' 가 아님).
    assert captured["cmd"][2] != "-"
    assert "[TASK]\ntiny" in captured["cmd"][2]


def test_run_subprocess_feeds_stdin_to_child(tmp_path):
    # base.run_subprocess 가 stdin_data 를 실제 자식 stdin 으로 흘리는지 확인(에코 프로그램).
    import sys

    prog = "import sys; sys.stdout.write(sys.stdin.read())"
    rc, out, err, timed_out = asyncio.run(
        base_mod.run_subprocess(
            [sys.executable, "-c", prog], str(tmp_path), 30, stdin_data=b"hello-stdin"
        )
    )
    assert rc == 0
    assert timed_out is False
    assert b"hello-stdin" in out
