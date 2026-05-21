"""감사(audit) 지적사항에 대한 백엔드 회귀 테스트.

다루는 이슈: 16, 17, 21, 41, 42, 43, 44, 45, 46, 95, 108, 109, 110, 111,
113, 114, 115, 116, 117, 118, 119, 122, 123, 124, 125, 126, 127, 128, 129.
모든 테스트는 결정적·오프라인이며 API 키가 필요 없다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from orchestrator.backends import backend_status
from orchestrator.backends.base import RoleRequest, run_subprocess
from orchestrator.backends.codex_cli import _sanitize_key
from orchestrator.backends.mock import MockBackend, _ident, _mock_e2e


def _make_request(tmp_path: Path, role: str, unit: dict | None, **kw) -> RoleRequest:
    key = unit["id"] if unit else "global"
    result_rel = f".orchestrator/results/{role}__{key}.json"
    base = dict(
        role=role,
        phase="dev",
        unit=unit,
        system_prompt="you are a test agent",
        prompt="do the thing",
        cwd=tmp_path,
        allowed_tools=["Read", "Write"],
        model=None,
        max_turns=20,
        budget=None,
        result_path=tmp_path / result_rel,
        result_rel=result_rel,
        spec_text="- feature one\n- feature two\n",
    )
    base.update(kw)
    return RoleRequest(**base)


# ---------------------------------------------------------------------------
# #17 / #21 — run_subprocess 타임아웃: SIGTERM grace 후 SIGKILL, proc.wait 포함
# ---------------------------------------------------------------------------


def test_timeout_returns_timed_out_flag():
    # #21: stdout/stderr 를 닫고도 계속 도는 프로세스가 타임아웃을 우회하지 못한다.
    # (자식이 stdout/err 를 즉시 닫지만 sleep 으로 계속 살아있음)
    code = "import sys,os,time; os.close(1); os.close(2); time.sleep(30)"
    start = time.monotonic()
    rc, out, err, timed_out = asyncio.run(run_subprocess([sys.executable, "-c", code], ".", 0.4))
    elapsed = time.monotonic() - start
    assert timed_out is True
    assert rc is None
    # SIGTERM grace(<=3s) + 종료까지 합쳐도 합리적 시간 안에 끊긴다.
    assert elapsed < 8.0


def test_timeout_sigterm_grace_lets_process_clean_up(tmp_path):
    # #17: 즉시 SIGKILL 이 아니라 SIGTERM 유예가 주어져, 핸들러가 정리할 기회를 갖는다.
    marker = tmp_path / "cleaned.txt"
    code = (
        "import signal,sys,time\n"
        "def h(*a):\n"
        f"    open({str(marker)!r},'w').write('clean')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,h)\n"
        "print('ready',flush=True)\n"
        "time.sleep(30)\n"
    )
    rc, out, err, timed_out = asyncio.run(
        run_subprocess([sys.executable, "-c", code], str(tmp_path), 0.5)
    )
    assert timed_out is True
    # SIGTERM 핸들러가 실행되어 마커 파일을 남겼다 → graceful 종료 경로가 동작.
    assert marker.exists()
    assert marker.read_text() == "clean"


def test_timeout_sigkill_fallback_for_unresponsive_process():
    # #17: SIGTERM 을 무시하는 프로세스는 grace 이후 SIGKILL 로 강제 종료된다.
    code = "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n"
    start = time.monotonic()
    rc, out, err, timed_out = asyncio.run(run_subprocess([sys.executable, "-c", code], ".", 0.3))
    elapsed = time.monotonic() - start
    assert timed_out is True
    # grace(~3s) 후 SIGKILL → 60초 sleep 전에 확실히 종료된다.
    assert elapsed < 8.0


def test_normal_subprocess_still_succeeds():
    rc, out, err, timed_out = asyncio.run(
        run_subprocess([sys.executable, "-c", "print('ok')"], ".", 10)
    )
    assert timed_out is False
    assert rc == 0
    assert b"ok" in out


# ---------------------------------------------------------------------------
# #41 — backend_status 가 개별 백엔드 예외를 격리한다
# ---------------------------------------------------------------------------


def test_backend_status_isolates_broken_backend(monkeypatch):
    import orchestrator.backends as bk

    class _Broken:
        name = "boom"

        def available(self):
            raise RuntimeError("kaboom")

    reg = dict(bk._REGISTRY)
    reg["boom"] = _Broken()
    monkeypatch.setattr(bk, "_REGISTRY", reg)

    rows = backend_status()  # 예외가 새지 않아야 한다
    by_name = {r["name"]: r for r in rows}
    assert by_name["boom"]["ok"] is False
    assert "kaboom" in by_name["boom"]["reason"]
    # 정상 백엔드(mock)는 여전히 보고된다
    assert by_name["mock"]["ok"] is True


# ---------------------------------------------------------------------------
# #42 / #43 / #44 — stderr cap 이 4000 이상으로 늘었다
# ---------------------------------------------------------------------------


def test_cli_backends_stderr_cap_increased():
    import inspect

    from orchestrator.backends import claude_cli, claude_team, codex_cli

    for mod in (claude_cli, claude_team, codex_cli):
        src = inspect.getsource(mod)
        assert "[:500]" not in src, f"{mod.__name__} 가 여전히 stderr 를 500 으로 자름"
        assert "[:4000]" in src, f"{mod.__name__} 에 4000 cap 이 없음"


# ---------------------------------------------------------------------------
# #109 / #110 / #111 — available() reason 이 'auth 미검증'을 정직하게 명시
# ---------------------------------------------------------------------------


def test_available_reason_is_honest_about_auth(monkeypatch):
    import shutil

    from orchestrator.backends.claude_cli import ClaudeCLIBackend
    from orchestrator.backends.claude_team import ClaudeTeamBackend
    from orchestrator.backends.codex_cli import CodexCLIBackend

    # 바이너리가 있는 것처럼 보이게 만든다
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    for backend in (ClaudeCLIBackend(), ClaudeTeamBackend(), CodexCLIBackend()):
        ok, reason = backend.available()
        assert ok is True
        assert "auth NOT verified" in reason


# ---------------------------------------------------------------------------
# #108 — codex out_path key sanitize (경로 탈출 방지)
# ---------------------------------------------------------------------------


def test_codex_sanitize_key_strips_traversal():
    assert _sanitize_key("U1") == "U1"
    assert _sanitize_key("a/b") == "a_b"
    assert _sanitize_key("") == "unit"
    assert _sanitize_key("..") == "unit"
    # 핵심 보안 속성: 경로 구분자 없음 + 선행 점/대시 없음 → 디렉터리 탈출·옵션 오인 불가
    for bad in ["../../etc/passwd", "../x", "a/../b", "/abs/path", "a\\b", ".."]:
        s = _sanitize_key(bad)
        assert "/" not in s and "\\" not in s
        assert not s.startswith(".") and not s.startswith("-")


# ---------------------------------------------------------------------------
# #126 / #127 / #128 — mock 식별자 sanitize
# ---------------------------------------------------------------------------


def test_ident_produces_valid_identifiers():
    # 비단어 문자 치환 + 숫자 선행 방지
    assert _ident("U-1") == "U_1"
    assert _ident("A/B") == "A_B"
    assert _ident("1") == "u_1"
    assert _ident("123abc") == "u_123abc"
    assert _ident("", prefix="Comp") == "Comp"
    # 생성된 이름은 파이썬 식별자로 유효
    for raw in ["U-1", "1", "A/B", "x.y", "9z", "valid_one"]:
        assert _ident(raw).isidentifier()
        assert _ident(raw, prefix="t").lower().isidentifier()


def test_mock_frontend_generates_valid_js_component(tmp_path):
    unit = {"id": "U-1", "title": "Auth"}
    req = _make_request(tmp_path, "frontend-developer", unit)
    asyncio.run(MockBackend().run_role(req))
    data = json.loads(req.result_path.read_text(encoding="utf-8"))
    jsx = next(a for a in data["artifacts"] if a.endswith(".jsx"))
    src = (tmp_path / jsx).read_text(encoding="utf-8")
    # 함수명에 '-' 같은 비식별자 문자가 들어가지 않는다
    assert "function U-1(" not in src
    assert "function Comp_1(" in src or "function U_1(" in src


def test_mock_test_engineer_generates_valid_python(tmp_path):
    unit = {"id": "1", "title": "core"}  # 숫자로 시작하는 위험 id
    req = _make_request(tmp_path, "test-engineer", unit)
    asyncio.run(MockBackend().run_role(req))
    data = json.loads(req.result_path.read_text(encoding="utf-8"))
    pyf = next(a for a in data["artifacts"] if a.endswith(".py"))
    src = (tmp_path / pyf).read_text(encoding="utf-8")
    # 생성된 파이썬은 컴파일 가능해야 한다 (유효 식별자)
    compile(src, pyf, "exec")


def test_mock_dba_generates_valid_sql_table(tmp_path):
    unit = {"id": "U-1", "title": "users"}
    req = _make_request(tmp_path, "dba", unit)
    asyncio.run(MockBackend().run_role(req))
    data = json.loads(req.result_path.read_text(encoding="utf-8"))
    sqlf = next(a for a in data["artifacts"] if a.endswith(".sql"))
    src = (tmp_path / sqlf).read_text(encoding="utf-8")
    # 테이블명에 '-' 등 비식별자 문자가 없다
    assert "t_u-1" not in src.lower()
    assert "create table if not exists t_" in src.lower()


# ---------------------------------------------------------------------------
# #125 — mock ci.yml 이 유효한 YAML 구조 (newline 존재)
# ---------------------------------------------------------------------------


def test_mock_cicd_yaml_has_newline_between_runs_on_and_steps(tmp_path):
    req = _make_request(tmp_path, "cicd", None)
    asyncio.run(MockBackend().run_role(req))
    yml = (tmp_path / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    # runs-on 과 steps 사이에 개행이 있어야 유효 YAML
    assert "ubuntu-latest\n" in yml
    assert "runs-on: ubuntu-lateststeps" not in yml
    # steps 가 별도 줄에 같은 들여쓰기로 존재
    lines = yml.splitlines()
    assert any(line.strip() == "steps:" for line in lines)


# ---------------------------------------------------------------------------
# #129 — mock e2e 가 spec 내용을 반영한다
# ---------------------------------------------------------------------------


def test_mock_e2e_reflects_spec_features():
    spec = "# Spec\n\n- 사용자 로그인\n- 게시글 작성\n- 댓글 달기\n- 무시될 4번째\n"
    out = _mock_e2e(spec)
    assert "사용자 로그인" in out
    assert "게시글 작성" in out
    assert "댓글 달기" in out
    # 최대 3개만 → 4번째는 미포함, 결정적
    assert "무시될 4번째" not in out
    assert _mock_e2e(spec) == out  # deterministic


def test_mock_e2e_empty_spec_still_has_smoke_scenario():
    out = _mock_e2e("")
    assert "스모크" in out


# ---------------------------------------------------------------------------
# #95 — openai Edit/Write 가 전체 덮어쓰기임을 docstring 에 명시
# ---------------------------------------------------------------------------


def test_openai_edit_documented_as_full_overwrite():
    import inspect

    from orchestrator.backends import openai_agents

    src = inspect.getsource(openai_agents)
    # Edit 가 write_file 에 매핑되고, 전체 덮어쓰기임을 코드/문서로 명시
    assert '"Edit": [write_file]' in src
    assert "덮어쓰기" in src


# ---------------------------------------------------------------------------
# #124 — openai run_bash 출력에 exit code 포함 (소스 검증)
# ---------------------------------------------------------------------------


def test_openai_run_bash_includes_exit_code():
    import inspect

    from orchestrator.backends import openai_agents

    src = inspect.getsource(openai_agents)
    assert "r.returncode" in src
    assert "[exit " in src


# ---------------------------------------------------------------------------
# #122 / #123 — openai read/write 크기 상한 (소스 검증)
# ---------------------------------------------------------------------------


def test_openai_read_write_have_size_limits():
    import inspect

    from orchestrator.backends import openai_agents

    src = inspect.getsource(openai_agents)
    assert "max_read_bytes" in src
    assert "max_write_bytes" in src
    assert "write rejected" in src
    assert "truncated" in src


# ---------------------------------------------------------------------------
# #46 — openai 가 model/tokens 를 채운다 (usage 추출기 단위 테스트)
# ---------------------------------------------------------------------------


def test_openai_extract_tokens_from_usage_object():
    from orchestrator.backends.openai_agents import _extract_tokens

    class _Usage:
        total_tokens = 1234

    class _CtxResult:
        class context_wrapper:  # noqa: N801
            usage = _Usage()

    assert _extract_tokens(_CtxResult()) == 1234


def test_openai_extract_tokens_from_raw_responses():
    from orchestrator.backends.openai_agents import _extract_tokens

    class _Usage:
        def __init__(self, i, o):
            self.input_tokens = i
            self.output_tokens = o
            self.total_tokens = None

    class _Resp:
        def __init__(self, u):
            self.usage = u

    class _Result:
        context_wrapper = None
        raw_responses = [_Resp(_Usage(100, 50)), _Resp(_Usage(10, 5))]

    assert _extract_tokens(_Result()) == 165


def test_openai_extract_tokens_none_when_absent():
    from orchestrator.backends.openai_agents import _extract_tokens

    class _Empty:
        pass

    assert _extract_tokens(_Empty()) is None


# ---------------------------------------------------------------------------
# #113 — claude_sdk 가 호환성 때문에 budget 을 떨어뜨리면 표면화한다
# ---------------------------------------------------------------------------


def test_claude_sdk_make_options_records_dropped_budget():
    from orchestrator.backends.claude_sdk import _make_options

    # max_budget_usd 를 받지 않는 옵션 클래스
    class _Opts:
        def __init__(self, system_prompt=None, max_turns=None):
            self.system_prompt = system_prompt
            self.max_turns = max_turns

    dropped: list[str] = []
    opts = _make_options(
        _Opts,
        dropped=dropped,
        system_prompt="x",
        max_turns=5,
        max_budget_usd=10.0,
    )
    assert opts is not None
    assert "max_budget_usd" in dropped


def test_claude_sdk_make_options_keeps_supported():
    from orchestrator.backends.claude_sdk import _make_options

    class _Opts:
        def __init__(self, system_prompt=None, max_budget_usd=None):
            self.system_prompt = system_prompt
            self.max_budget_usd = max_budget_usd

    dropped: list[str] = []
    opts = _make_options(_Opts, dropped=dropped, system_prompt="x", max_budget_usd=10.0)
    assert opts.max_budget_usd == 10.0
    assert "max_budget_usd" not in dropped


# ---------------------------------------------------------------------------
# #45 — claude_sdk 가 usage/model 메시지에서 tokens/model 을 캡처 (#46 와 유사 패턴)
# ---------------------------------------------------------------------------


def test_claude_sdk_run_role_captures_model_and_tokens(monkeypatch):
    import orchestrator.backends.claude_sdk as sdk

    class _Msg:
        content = "all done"
        total_cost_usd = 0.42
        model = "claude-test-model"
        usage = {"input_tokens": 100, "output_tokens": 40}

    class _FakeOptions:
        def __init__(self, **kw):
            pass

    async def _fake_query(prompt=None, options=None):
        yield _Msg()

    # claude_agent_sdk 의 import 를 가짜로 대체
    fake_mod = type(sys)("claude_agent_sdk")
    fake_mod.ClaudeAgentOptions = _FakeOptions
    fake_mod.query = _fake_query
    fake_mod.AgentDefinition = object
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_mod)

    req = _make_request(Path("/tmp"), "backend-developer", {"id": "U1", "title": "x"})
    res = asyncio.run(sdk.ClaudeSDKBackend().run_role(req))
    assert res.ok is True
    assert res.final_message == "all done"
    assert res.cost_usd == 0.42
    assert res.model == "claude-test-model"
    assert res.tokens == 140


# ---------------------------------------------------------------------------
# #112 / #114 / #115 / #116 / #117 / #118 / #119 — 미지원 enforcement 가 명시됨
# ---------------------------------------------------------------------------


def test_budget_turn_limit_documented_as_unsupported_for_clis():
    import inspect

    from orchestrator.backends import claude_cli, claude_team, codex_cli, openai_agents

    for mod in (claude_cli, claude_team, codex_cli):
        src = inspect.getsource(mod)
        # CLI 에는 per-call budget/turn-limit 플래그가 없음을 코드 주석으로 명시
        assert "budget" in src and "max_turns" in src
        assert "불가" in src
    # openai 는 budget 미지원 명시 + max_turns 는 전달
    osrc = inspect.getsource(openai_agents)
    assert "예산 캡 옵션이 없어" in osrc
    assert "max_turns=req.max_turns" in osrc
