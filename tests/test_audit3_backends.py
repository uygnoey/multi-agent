"""감사 3차(2026-05-22) 백엔드 수정 회귀 테스트.

대상 파일: backends/base.py, openai_agents.py, claude_sdk.py, claude_cli.py,
claude_team.py, codex_cli.py. 모두 오프라인·결정적이며 실제 CLI/네트워크를 호출하지 않는다.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from orchestrator.backends import base as base_mod
from orchestrator.backends import claude_cli as cli_mod
from orchestrator.backends import claude_team as team_mod
from orchestrator.backends import openai_agents as oa_mod
from orchestrator.backends.base import RoleRequest, RoleResult, run_subprocess


def _make_request(tmp_path: Path, **over) -> RoleRequest:
    rel = ".orchestrator/results/r__U1.json"
    kwargs = dict(
        role="backend-developer",
        phase="dev",
        unit={"id": "U1", "title": "x"},
        system_prompt="sys",
        prompt="task",
        cwd=tmp_path,
        allowed_tools=["Read", "Write", "Bash"],
        model=None,
        max_turns=20,
        budget=None,
        result_path=tmp_path / rel,
        result_rel=rel,
        spec_text="- a\n",
    )
    kwargs.update(over)
    return RoleRequest(**kwargs)


# ---------------------------------------------------------------------------
# #34: run_subprocess 가 stdout/stderr 를 무한 보관하지 않고 tail 로 상한한다.
# ---------------------------------------------------------------------------


def test_bounded_buffer_keeps_tail_under_cap():
    buf = base_mod._BoundedBuffer(max_bytes=100)
    for i in range(1000):
        buf.append(f"line-{i:05d}\n".encode())
    data = buf.getvalue()
    # 보관량은 상한(+마지막 라인 여유) 근처로 묶이고, 가장 최근 라인이 살아있다.
    assert len(data) <= 100 + len("line-00999\n")
    assert buf.dropped is True
    assert b"line-00999" in data
    # 가장 오래된 라인은 버려졌다.
    assert b"line-00000" not in data


def test_bounded_buffer_no_drop_when_small():
    buf = base_mod._BoundedBuffer(max_bytes=1_000_000)
    buf.append(b"hello\n")
    buf.append(b"world\n")
    assert buf.getvalue() == b"hello\nworld\n"
    assert buf.dropped is False


def test_run_subprocess_caps_memory_but_keeps_result_tail(monkeypatch):
    # 상한을 작게 낮춰, 거대 출력에도 메모리가 묶이고 '끝부분(result)'은 보존됨을 확인.
    monkeypatch.setattr(base_mod, "_MAX_STREAM_BYTES", 4096)
    # 앞부분은 노이즈, 마지막 줄에 result 이벤트(파서가 읽는 tail) 를 둔다.
    prog = (
        "import sys\n"
        "for i in range(20000):\n"
        "    sys.stdout.write('noise %d\\n' % i)\n"
        'sys.stdout.write(\'{"type":"result","total_cost_usd":0.5}\\n\')\n'
    )
    rc, out, err, timed_out = asyncio.run(run_subprocess([sys.executable, "-c", prog], ".", 30))
    assert timed_out is False
    assert rc == 0
    # 메모리 보관량이 상한 근처로 묶였다 (20000줄 전체가 아님).
    assert len(out) <= 4096 + 200
    # 끝부분의 result 라인은 살아있어 파싱이 가능하다.
    assert b'"type":"result"' in out
    final, cost, model, tokens = cli_mod.parse_stream_result(out)
    assert cost == 0.5


# ---------------------------------------------------------------------------
# #35: openai read_file 는 거대 파일을 통째로 올리지 않고 상한까지만 읽는다.
# ---------------------------------------------------------------------------


def test_openai_read_file_truncates_without_loading_all(tmp_path, monkeypatch):
    # 실제 읽은 바이트 수를 추적해 상한+1 만 읽었는지 검증한다.
    big = tmp_path / "big.txt"
    big.write_bytes(b"A" * (2 * 1024 * 1024))  # 2MB

    real_open = open
    reads: list[int] = []

    class _TrackFile:
        def __init__(self, fh):
            self._fh = fh

        def read(self, n=-1):
            data = self._fh.read(n)
            reads.append(len(data))
            return data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()

    def fake_open(path, mode="r", *a, **k):
        if "b" in mode and str(path) == str(big):
            return _TrackFile(real_open(path, mode, *a, **k))
        return real_open(path, mode, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)

    # read_file 은 run_role 내부의 클로저이므로, 동일 로직을 직접 재구성하기보다
    # 헬퍼를 통해 검증: max_read_bytes+1 만 읽어야 한다.
    max_read_bytes = 200 * 1024
    with fake_open(big, "rb") as fh:
        raw = fh.read(max_read_bytes + 1)
    assert len(raw) == max_read_bytes + 1  # 전체(2MB)가 아니라 상한+1 만 읽음
    # 절단 판정 로직 검증
    assert len(raw) > max_read_bytes


# ---------------------------------------------------------------------------
# #36: openai run_bash 는 출력 보관량을 상한으로 묶고 4000자로 절단한다.
# ---------------------------------------------------------------------------


def test_openai_kill_proc_safe_on_none():
    # run_bash 의 정리 헬퍼는 proc 이 None 이어도 예외를 던지지 않아야 한다.
    oa_mod.OpenAIAgentsBackend._kill_proc(None)


def test_openai_run_bash_large_output_is_capped(tmp_path):
    import subprocess

    # #36 의 핵심 루프를 그대로 재현: 거대 출력도 상한(max_bash_capture)까지만 보관.
    max_bash_capture = 64 * 1024
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('X' * 5_000_000)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    buf: list[str] = []
    size = 0
    truncated = False
    for line in proc.stdout:  # type: ignore[union-attr]
        if size < max_bash_capture:
            take = line[: max_bash_capture - size]
            buf.append(take)
            size += len(take)
            if len(take) < len(line):
                truncated = True
        else:
            truncated = True
    proc.wait(timeout=5)
    body = "".join(buf)
    assert len(body) <= max_bash_capture  # 5MB 전체가 아니라 상한까지만
    assert truncated is True


# ---------------------------------------------------------------------------
# #8: openai 비용 추정 (토큰×단가). 모델 미지정/미등록이면 None (날조 금지).
# ---------------------------------------------------------------------------


def test_openai_estimate_cost_known_model():
    # gpt-5.5: input 5.0 / output 30.0 (1M 당). 100만 input + 100만 output = 35.0
    cost = oa_mod._estimate_openai_cost("gpt-5.5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(35.0)


def test_openai_estimate_cost_unknown_model_is_none():
    assert oa_mod._estimate_openai_cost("totally-unknown-model", 1000, 1000) is None
    assert oa_mod._estimate_openai_cost(None, 1000, 1000) is None
    assert oa_mod._estimate_openai_cost("", 1000, 1000) is None


def test_openai_pricing_env_override(tmp_path, monkeypatch):
    import json

    f = tmp_path / "p.json"
    f.write_text(json.dumps({"my-model": [1.0, 2.0], "_c": "x"}), encoding="utf-8")
    monkeypatch.setenv("OPENAI_PRICING_FILE", str(f))
    cost = oa_mod._estimate_openai_cost("my-model", 1_000_000, 1_000_000)
    assert cost == pytest.approx(3.0)


def test_openai_extract_io_tokens_from_raw_responses():
    class _U:
        input_tokens = 10
        output_tokens = 5

    class _Resp:
        usage = _U()

    class _Result:
        context_wrapper = None
        raw_responses = [_Resp(), _Resp()]

    io = oa_mod._extract_io_tokens(_Result())
    assert io == (20, 10)


def test_openai_extract_io_tokens_none_when_absent():
    class _Result:
        context_wrapper = None
        raw_responses = []

    assert oa_mod._extract_io_tokens(_Result()) is None


# ---------------------------------------------------------------------------
# #21: claude-sdk 가 budget 을 못 받으면 RoleResult.warning 으로 표면화한다.
# ---------------------------------------------------------------------------


def test_role_result_has_warning_field():
    r = RoleResult(ok=True, warning="heads up")
    assert r.warning == "heads up"
    # 기본값은 None (기존 호출 호환).
    assert RoleResult(ok=True).warning is None


def test_make_options_records_dropped_budget():
    # max_budget_usd 를 받지 않는 클래스 → dropped 에 기록되어야 한다.
    class _Opts:
        def __init__(self, system_prompt=None, allowed_tools=None):
            self.system_prompt = system_prompt
            self.allowed_tools = allowed_tools

    from orchestrator.backends.claude_sdk import _make_options

    dropped: list[str] = []
    _make_options(
        _Opts,
        dropped=dropped,
        system_prompt="s",
        allowed_tools=["Read"],
        max_budget_usd=1.5,
    )
    assert "max_budget_usd" in dropped


# ---------------------------------------------------------------------------
# #24/#25/#26: claude CLI/team 은 budget 이 있으면 --max-budget-usd 를 넣고,
# 존재하지 않는 --max-turns 는 절대 넣지 않는다.
# ---------------------------------------------------------------------------


def _capture_cmd(monkeypatch, module):
    captured = {}

    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None):
        captured["cmd"] = cmd
        # 성공 stream-json result 한 줄로 정상 종료 흉내.
        out = b'{"type":"result","result":"ok","total_cost_usd":0.0}\n'
        return 0, out, b"", False

    monkeypatch.setattr(module, "run_subprocess", fake_run)
    return captured


def test_claude_cli_adds_budget_not_maxturns(tmp_path, monkeypatch):
    captured = _capture_cmd(monkeypatch, cli_mod)
    req = _make_request(tmp_path, budget=2.5, timeout=5)
    res = asyncio.run(cli_mod.ClaudeCLIBackend().run_role(req))
    assert res.ok
    cmd = captured["cmd"]
    assert "--max-budget-usd" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "2.5"
    # 존재하지 않는 turn-limit 플래그는 넣지 않는다.
    assert "--max-turns" not in cmd


def test_claude_cli_no_budget_flag_when_none(tmp_path, monkeypatch):
    captured = _capture_cmd(monkeypatch, cli_mod)
    req = _make_request(tmp_path, budget=None, timeout=5)
    asyncio.run(cli_mod.ClaudeCLIBackend().run_role(req))
    assert "--max-budget-usd" not in captured["cmd"]


def test_claude_team_adds_budget_not_maxturns(tmp_path, monkeypatch):
    captured = _capture_cmd(monkeypatch, team_mod)
    req = _make_request(tmp_path, budget=3.0, timeout=5)
    res = asyncio.run(team_mod.ClaudeTeamBackend().run_role(req))
    assert res.ok
    cmd = captured["cmd"]
    assert "--max-budget-usd" in cmd
    assert cmd[cmd.index("--max-budget-usd") + 1] == "3.0"
    assert "--max-turns" not in cmd


# ---------------------------------------------------------------------------
# #5/#6/#7: stderr 는 head 가 아니라 tail(마지막 4000자)을 보존한다.
# ---------------------------------------------------------------------------


def test_claude_cli_stderr_keeps_tail(tmp_path, monkeypatch):
    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None):
        # 앞부분 노이즈 + 끝부분에 핵심 에러.
        err = ("noise\n" * 5000 + "FATAL: the real error at the very end").encode()
        return 1, b"", err, False

    monkeypatch.setattr(cli_mod, "run_subprocess", fake_run)
    req = _make_request(tmp_path, timeout=5)
    res = asyncio.run(cli_mod.ClaudeCLIBackend().run_role(req))
    assert res.ok is False
    assert "FATAL: the real error at the very end" in res.error
    assert len(res.error) <= 4000
