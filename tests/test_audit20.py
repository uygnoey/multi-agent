"""audit20 회귀 테스트 — Claude↔Codex 5라운드 교차검증 합의 수정.

수정 항목(합의된 최종표):
  #1 (HIGH) --feature 가 rerun 화이트리스트에 없어 feature 모드 run 의 TUI 재실행이 전부 거부됨.
  #2 (MED)  claude-cli/claude-team 이 거대 프롬프트를 positional argv 로 넘겨 E2BIG 위험(codex 만
            stdin 우회). 임계치 초과 시 stdin 으로 우회.
  #3 (MED)  /api/run 의 feature 가 타입/길이/NUL 검증 없이 --feature=<값> argv 로 흘러가 argv 폭발.
            + 웹 폼/JS 에 feature 입력 경로 부재.
  #5 (MED)  codex _usage_from_jsonl 이 중첩 input_tokens_details.cached_tokens 를 누락(비용 과대추정).
  #6 (LOW)  RunConfig(role_priority/role_backend) 의 alias '키' 미정규화로 핀이 조용히 무시됨.
  #7 (LOW)  orchestrator.webui --port 가 range 검증 없이 OverflowError 로 터짐.
  board._flush stale tmp 정리 누락(LOW).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import webui
from orchestrator.backends import claude_cli, claude_team
from orchestrator.backends.base import RoleRequest
from orchestrator.backends.claude_cli import ClaudeCLIBackend
from orchestrator.backends.claude_team import ClaudeTeamBackend
from orchestrator.backends.codex_cli import _usage_from_jsonl
from orchestrator.board import Board
from orchestrator.config import RunConfig
from orchestrator.monitor import _validate_rerun_argv


# ---------------------------------------------------------------------------
# #1 — --feature 가 rerun 화이트리스트에 포함되어 feature 모드 run 재실행이 통과
# ---------------------------------------------------------------------------
def test_feature_flag_allowed_in_rerun_whitelist():
    ok, _ = _validate_rerun_argv(["--feature", "add-login", "--project-dir", "/tmp/p", "--mock"])
    assert ok is True
    ok_eq, _ = _validate_rerun_argv(["--feature=add-login", "--project-dir", "/tmp/p", "--mock"])
    assert ok_eq is True


def test_feature_flag_requires_value_arity():
    # 값을 요구하는 플래그이므로 바로 뒤에 플래그가 오면 거부(arity 검증 동작 확인).
    ok, why = _validate_rerun_argv(["--feature", "--mock"])
    assert ok is False
    assert "--feature" in why


# ---------------------------------------------------------------------------
# #6 — role_priority / role_backend 의 alias '키' 정규화
# ---------------------------------------------------------------------------
def test_role_priority_alias_key_normalized():
    c = RunConfig(
        spec_path=Path("/tmp/s"),
        project_dir=Path("/tmp/p"),
        default_backend="mock",
        role_priority={"backend": ["codex"]},
    )
    assert "backend-developer" in c.role_priority
    assert c.backends_for("backend-developer") == ["codex"]


def test_role_backend_alias_key_normalized():
    c = RunConfig(
        spec_path=Path("/tmp/s"),
        project_dir=Path("/tmp/p"),
        default_backend="mock",
        role_backend={"qa": "codex"},  # qa 는 정규명이지만 alias 도 정규화 경로를 타는지 확인
    )
    # 정규화된 키로 조회 가능해야 한다.
    assert c.backends_for("qa") == ["codex"]


# ---------------------------------------------------------------------------
# #5 — codex _usage_from_jsonl 이 flat 과 nested cached 모두 집계
# ---------------------------------------------------------------------------
def test_codex_usage_flat_cached_tokens():
    line = (
        b'{"type":"turn.completed","usage":'
        b'{"input_tokens":10,"cached_input_tokens":7,"output_tokens":3}}'
    )
    u = _usage_from_jsonl(line)
    assert u["cached_input_tokens"] == 7
    assert u["input_tokens"] == 10
    assert u["output_tokens"] == 3


def test_codex_usage_nested_cached_tokens_lifted():
    line = (
        b'{"type":"turn.completed","usage":'
        b'{"input_tokens":10,"input_tokens_details":{"cached_tokens":7},"output_tokens":3}}'
    )
    u = _usage_from_jsonl(line)
    # 중첩 cached_tokens 가 평면 cached_input_tokens 로 끌어올려져야 한다.
    assert u["cached_input_tokens"] == 7
    assert u["input_tokens"] == 10


def test_codex_usage_no_double_count_when_both_present():
    # 같은 usage 에 평면 키가 있으면 중첩값으로 중복 합산하지 않는다.
    line = (
        b'{"type":"turn.completed","usage":'
        b'{"input_tokens":10,"cached_input_tokens":4,'
        b'"input_tokens_details":{"cached_tokens":7},"output_tokens":3}}'
    )
    u = _usage_from_jsonl(line)
    assert u["cached_input_tokens"] == 4  # 평면 우선, 중첩은 무시


# ---------------------------------------------------------------------------
# #2 — claude-cli / claude-team 의 거대 프롬프트 stdin 우회
# ---------------------------------------------------------------------------
def _make_request(tmp_path: Path, prompt: str, role: str = "backend-developer") -> RoleRequest:
    rel = f".orchestrator/results/{role}__U1.json"
    return RoleRequest(
        role=role,
        phase="dev",
        unit={"id": "U1", "title": "x"},
        system_prompt="sys",
        prompt=prompt,
        cwd=tmp_path,
        allowed_tools=["Read", "Write"],
        model=None,
        max_turns=20,
        budget=None,
        result_path=tmp_path / rel,
        result_rel=rel,
        spec_text="- f1\n",
    )


def _capture_backend(mod, backend, req):
    captured = {}

    async def fake_run(cmd, cwd, timeout, log_path=None, line_render=None, **kw):
        captured["cmd"] = cmd
        captured["stdin"] = kw.get("stdin_data")
        return 0, b'{"type":"result","result":"ok","total_cost_usd":0.0}', b"", False

    old = mod.run_subprocess
    mod.run_subprocess = fake_run
    try:
        asyncio.run(backend.run_role(req))
    finally:
        mod.run_subprocess = old
    return captured


@pytest.mark.parametrize(
    "mod,backend",
    [(claude_cli, ClaudeCLIBackend()), (claude_team, ClaudeTeamBackend())],
)
def test_claude_small_prompt_uses_positional_argv(mod, backend, tmp_path):
    cap = _capture_backend(mod, backend, _make_request(tmp_path, "small prompt"))
    assert cap["stdin"] is None
    # 작은 프롬프트는 positional 자리(인덱스 2)가 플래그가 아니어야 한다.
    assert not cap["cmd"][2].startswith("--")


@pytest.mark.parametrize(
    "mod,backend",
    [(claude_cli, ClaudeCLIBackend()), (claude_team, ClaudeTeamBackend())],
)
def test_claude_large_prompt_switches_to_stdin(mod, backend, tmp_path):
    big = "X" * 200_000
    cap = _capture_backend(mod, backend, _make_request(tmp_path, big))
    # 거대 프롬프트는 argv 에서 빠지고 stdin 으로 전달된다.
    assert cap["stdin"] is not None and len(cap["stdin"]) >= 200_000
    assert big not in cap["cmd"]
    assert cap["cmd"][:3] == ["claude", "-p", "--output-format"]


# ---------------------------------------------------------------------------
# #7 — webui --port range 검증
# ---------------------------------------------------------------------------
def test_webui_port_arg_validates_range():
    assert webui._port_arg("8765") == 8765
    for bad in ("70000", "0", "-1", "abc"):
        with pytest.raises(argparse.ArgumentTypeError):
            webui._port_arg(bad)


# ---------------------------------------------------------------------------
# board._flush — write 실패 시 stale .tmp 를 남기지 않는다
# ---------------------------------------------------------------------------
def test_board_flush_cleans_tmp_on_write_failure(tmp_path, monkeypatch):
    b = Board(tmp_path)
    asyncio.run(b.init("# spec", {}))
    tmp = b.path.with_suffix(".json.tmp")
    assert not tmp.exists()

    real_fsync = __import__("os").fsync

    def boom(fd):
        raise OSError("simulated ENOSPC")

    monkeypatch.setattr("orchestrator.board.os.fsync", boom)
    with pytest.raises(OSError):
        b._flush()
    monkeypatch.setattr("orchestrator.board.os.fsync", real_fsync)
    assert not tmp.exists(), "stale board.json.tmp 가 정리되지 않았다"


# ---------------------------------------------------------------------------
# #3 — /api/run 의 feature 검증 (HTTP 하니스)
# ---------------------------------------------------------------------------
@pytest.fixture
def server(tmp_path):
    httpds = []
    spawned: list = []

    def fake_spawn(cmd, log_path):
        spawned.append(cmd)

        class _P:
            pid = 44444

            def poll(self):
                return 0

            def wait(self):
                return 0

        return _P()

    manager = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, None))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    httpds.append(httpd)
    yield {"base": f"http://127.0.0.1:{port}", "spawned": spawned}
    for h in httpds:
        h.shutdown()
        h.server_close()


def _post(base, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/run", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_api_run_rejects_oversized_feature(server):
    code, body = _post(
        server["base"], {"spec_text": "# s", "backend": "mock", "feature": "x" * 1_000_000}
    )
    assert code == 400
    assert "feature" in body.get("error", "").lower()


def test_api_run_rejects_nonstring_feature(server):
    code, body = _post(server["base"], {"spec_text": "# s", "backend": "mock", "feature": 123})
    assert code == 400


def test_api_run_rejects_nul_in_feature(server):
    code, body = _post(server["base"], {"spec_text": "# s", "backend": "mock", "feature": "a\x00b"})
    assert code == 400


def test_api_run_accepts_valid_feature_and_passes_flag(server):
    code, body = _post(server["base"], {"spec_text": "# s", "backend": "mock", "feature": "add login"})
    assert code == 200, body
    assert "run_id" in body
    # spawn 된 argv 에 --feature=add login 이 들어가야 한다(웹/API feature 모드 동작).
    assert server["spawned"], "spawn 이 호출되지 않았다"
    assert any(str(a) == "--feature=add login" for a in server["spawned"][-1])
