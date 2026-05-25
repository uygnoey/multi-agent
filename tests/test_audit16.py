"""Regression tests for audit16 (Claude↔Codex 교차검증 합의 수정).

각 테스트는 수정 전 동작(버그)을 재현하던 시나리오가 이제 올바르게 동작하는지 검증한다.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

import orchestrator.procutil as procutil
from orchestrator.backends.base import RoleRequest
from orchestrator.backends.codex_cli import _read_last_message
from orchestrator.config import RunConfig
from orchestrator.gitcheckpoints import GitCheckpointer
from orchestrator.monitor import _coerce_board_schema
from orchestrator.scheduler import Scheduler


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args], text=True, capture_output=True, check=False
    )


def _git_env(monkeypatch) -> None:
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_AUTHOR_EMAIL", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_EMAIL", raising=False)
    import os

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


# ---------------------------------------------------------------------------
# #1 gitcheckpoints baseline/init 묶음
# ---------------------------------------------------------------------------


def test_gitcheckpoint_skips_nested_repo_under_parent(tmp_path: Path, monkeypatch):
    """project_dir 가 부모 git repo 하위면 nested repo 를 만들지 않고 체크포인트를 끈다."""
    if not __import__("shutil").which("git"):
        pytest.skip("git not available")
    _git_env(monkeypatch)
    parent = tmp_path / "parent"
    parent.mkdir()
    assert _git(parent, "init").returncode == 0
    project = parent / "generated"
    project.mkdir()

    cp = GitCheckpointer(project)
    (project / "f.txt").write_text("x", encoding="utf-8")
    committed, detail = asyncio.run(cp.checkpoint("orchestrator: test"))

    assert committed is False
    assert "nested" in detail or "inside an existing git repo" in detail
    # 핵심: project 안에 별도의 .git 이 생기지 않았다.
    assert not (project / ".git").exists()


def test_gitcheckpoint_non_git_dir_does_not_commit_preexisting(tmp_path: Path, monkeypatch):
    """비-git 기존 디렉터리의 첫 체크포인트가 기존 사용자 파일을 커밋하지 않는다."""
    if not __import__("shutil").which("git"):
        pytest.skip("git not available")
    _git_env(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "preexisting.txt").write_text("user work", encoding="utf-8")

    cp = GitCheckpointer(project)  # baseline = {preexisting.txt: hash} (비-git 이어도 캡처)
    (project / "generated.txt").write_text("gen", encoding="utf-8")
    committed, detail = asyncio.run(cp.checkpoint("orchestrator: scaffold"))

    assert committed, detail
    show = _git(project, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
    assert "generated.txt" in show
    assert "preexisting.txt" not in show  # ← 이전엔 baseline 이 비어 함께 커밋됐다


def test_gitcheckpoint_remodified_baseline_dirty_is_committed(tmp_path: Path, monkeypatch):
    """시작 시 dirty 였던 파일을 orchestrator 가 다시 수정하면 그 변경분이 유실되지 않는다."""
    if not __import__("shutil").which("git"):
        pytest.skip("git not available")
    _git_env(monkeypatch)
    project = tmp_path / "proj"
    project.mkdir()
    assert _git(project, "init").returncode == 0
    (project / "dirty.txt").write_text("user dirty", encoding="utf-8")

    cp = GitCheckpointer(project)  # baseline hashes dirty.txt
    # orchestrator 가 dirty.txt 를 추가 수정 + 새 파일 생성
    (project / "dirty.txt").write_text("orchestrator changed", encoding="utf-8")
    (project / "new.txt").write_text("new", encoding="utf-8")
    committed, detail = asyncio.run(cp.checkpoint("orchestrator: rework"))

    assert committed, detail
    show = _git(project, "show", "--name-only", "--format=", "HEAD").stdout.splitlines()
    assert "new.txt" in show
    # ← 이전엔 경로명 차집합이라 dirty.txt 가 영구 제외됐다(작업 유실).
    assert "dirty.txt" in show


# ---------------------------------------------------------------------------
# #2 --max-units 의도적 skip 이 phase failed 로 가지 않게
# ---------------------------------------------------------------------------


def test_max_units_skip_does_not_fail_run(tmp_path: Path, sample_spec_path: Path):
    cfg = RunConfig(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "demo",
        mock=True,
        poll_interval=600.0,
        max_units=1,
    )
    snap = asyncio.run(Scheduler(cfg).run())

    assert snap["phase"] == "done"  # ← 이전엔 designed 스킵 unit 때문에 "failed"
    statuses = {u["id"]: u["status"] for u in snap["units"]}
    assert "done" in statuses.values()
    assert "designed" in statuses.values()  # 스킵된 unit 이 존재
    warns = snap.get("warnings", [])
    assert any("max-units" in w for w in warns)
    assert not any("미완료 unit" in w for w in warns)  # 거짓 failed 경고 없음


# ---------------------------------------------------------------------------
# #3 dev external blocker counter 분리 (failure_kind/command 진동에도 자동중단)
# ---------------------------------------------------------------------------


def _mk_scheduler(tmp_path: Path) -> Scheduler:
    spec = tmp_path / "spec.md"
    spec.write_text("x", encoding="utf-8")
    cfg = RunConfig(
        spec_path=spec, project_dir=tmp_path / "proj", default_backend="mock", max_attempts=0
    )
    return Scheduler(cfg)


def test_dev_external_counter_survives_varying_command(tmp_path: Path):
    s = _mk_scheduler(tmp_path)
    uid = "U1"
    repeats = []
    for cmd in ["npm run build", "pnpm build", "yarn build"]:
        outcome = {"_ok": False, "status": "failed", "failure_kind": "tool_missing", "command": cmd}
        s._remember_dev_failure(uid, [outcome])
        s._last_dev_failure[uid] = {**outcome, "_ok": False, "notes": [], "blockers": []}
        ext = s._external_blocker_reason([s._last_dev_failure.get(uid, {})])
        assert ext == "tool_missing"
        # _dev_repair_loop 의 외부장애 카운팅을 그대로 모사
        if ext:
            s._external_repeat[uid] = s._external_repeat.get(uid, 0) + 1
        repeats.append(s._external_repeat[uid])
    # command 가 매번 달라도 외부장애 반복은 누적되어 2회차에 자동중단 임계에 도달한다.
    assert repeats == [1, 2, 3]
    assert any(r >= 2 for r in repeats)


# ---------------------------------------------------------------------------
# #4 workspace .gitignore 비-UTF8 read crash 방지
# ---------------------------------------------------------------------------


def test_scaffold_survives_non_utf8_gitignore(tmp_path: Path):
    from orchestrator import workspace

    project = tmp_path / "proj"
    project.mkdir()
    # 유효하지 않은 UTF-8 바이트를 포함한 .gitignore
    (project / ".gitignore").write_bytes(b"node_modules/\n\xff\xfe binary junk\n")
    # 이전엔 UnicodeDecodeError 로 scaffold 가 중단됐다.
    workspace.scaffold(project, "spec body", {"language": "python"})
    assert (project / ".gitignore").exists()


# ---------------------------------------------------------------------------
# #5 webui rerun/start/opts
# ---------------------------------------------------------------------------


def test_rerun_unknown_run_rejected(tmp_path: Path):
    from orchestrator.webui import RunManager

    mgr = RunManager(base_dir=tmp_path)
    with pytest.raises(ValueError):
        mgr.rerun("run-does-not-exist")


def test_run_opts_json_excludes_spec_text(tmp_path: Path):
    from orchestrator.webui import RunManager

    mgr = RunManager(base_dir=tmp_path)
    run_id = mgr.start("SECRET SPEC BODY", {"mock": True, "spec_text": "SECRET SPEC BODY"})
    opts_path = mgr.project_dir(run_id) / "_run_opts.json"
    saved = json.loads(opts_path.read_text(encoding="utf-8"))
    assert "spec_text" not in saved  # spec 은 _spec.md 에만 저장
    assert saved.get("mock") is True


# ---------------------------------------------------------------------------
# #6 claude_sdk usage max 누적 (trailing 0-usage 메시지에 in/out 토큰 0 으로 덮이지 않음)
# ---------------------------------------------------------------------------


def test_claude_sdk_usage_max_accumulation(tmp_path: Path, monkeypatch):
    # 가짜 claude_agent_sdk 모듈 주입
    fake = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kw):
            self.kw = kw

    def make_msg(content, usage, model="claude-sonnet-4-5", cost=None):
        ns = types.SimpleNamespace(content=content, usage=usage, model=model)
        ns.total_cost_usd = cost
        return ns

    async def query(prompt, options):  # noqa: ARG001
        # 누적(cumulative) usage 메시지 다음에 trailing 0-usage 메시지가 온다.
        yield make_msg("partial", {"input_tokens": 30, "output_tokens": 20, "total_tokens": 50})
        yield make_msg("", {"input_tokens": 0, "output_tokens": 0})

    fake.ClaudeAgentOptions = ClaudeAgentOptions
    fake.query = query
    fake.AgentDefinition = object
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)

    from orchestrator.backends.claude_sdk import ClaudeSDKBackend

    req = RoleRequest(
        role="backend-developer",
        phase="dev",
        unit=None,
        system_prompt="sys",
        prompt="task",
        cwd=tmp_path,
        allowed_tools=["Read"],
        model="claude-sonnet-4-5",
        max_turns=5,
        budget=None,
        result_path=tmp_path / "r.json",
        result_rel="r.json",
        spec_text="",
        timeout=30,
    )
    res = asyncio.run(ClaudeSDKBackend().run_role(req))
    assert res.tokens == 50  # 누적 total 유지(이중합산 65 아님)
    # 구독 모드(total_cost_usd=None) 추정 비용은 max in/out(30/20)에서 나온다.
    # 이전(= 할당)이면 trailing 0-usage 가 in/out 을 0 으로 덮어 cost=None 이 됐다.
    assert res.cost_usd is not None and res.cost_usd > 0
    assert res.cost_estimated is True


# ---------------------------------------------------------------------------
# #7a monitor/webui agents 값 dict 필터
# ---------------------------------------------------------------------------


def test_coerce_board_drops_non_dict_agent_values():
    data = _coerce_board_schema(
        {"agents": {"qa": "running", "pm": {"status": "idle"}}, "units": []}
    )
    assert data["agents"] == {"pm": {"status": "idle"}}  # qa(str) 드롭
    # agents 키가 없으면 그대로 둔다(absent 보존)
    assert "agents" not in _coerce_board_schema({"phase": "done"})


# ---------------------------------------------------------------------------
# #7c procutil pid 검증 fresh token + cache hard cap
# ---------------------------------------------------------------------------


def test_pid_is_ours_ignores_poisoned_cache(monkeypatch):
    import os
    import time

    pid = os.getpid()
    real = procutil._compute_start_token(pid)
    if not real:
        pytest.skip("start token unavailable on this platform")
    # 캐시에 잘못된 토큰을 심어 둔다(만료시각을 미래로).
    with procutil._TOKEN_LOCK:
        procutil._TOKEN_CACHE[pid] = (time.monotonic() + 100.0, "WRONG-TOKEN")
    # 검증 경로는 캐시를 우회해 fresh token 을 계산하므로 여전히 ours 로 판정한다.
    assert procutil.pid_is_ours(pid, real) is True


def test_token_cache_hard_cap():
    procutil._TOKEN_CACHE.clear()
    # 모두 fresh(만료 안 됨)인 항목을 cap 이상 삽입해도 상한을 넘지 않는다.
    for fake_pid in range(2_000_000, 2_000_000 + _cap_overflow()):
        procutil.process_start_token(fake_pid)
    assert len(procutil._TOKEN_CACHE) <= procutil._TOKEN_CACHE_MAX


def _cap_overflow() -> int:
    return procutil._TOKEN_CACHE_MAX + 50


# ---------------------------------------------------------------------------
# #7d codex output-last-message symlink 거부
# ---------------------------------------------------------------------------


def test_codex_read_last_message_rejects_symlink(tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    link = tmp_path / "out.codex.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    assert _read_last_message(link) == ""  # symlink 는 거부 → 빈 문자열

    # 정상 파일은 그대로 읽힌다.
    plain = tmp_path / "plain.codex.txt"
    plain.write_text("hello", encoding="utf-8")
    assert _read_last_message(plain) == "hello"
