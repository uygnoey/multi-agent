"""Regression tests for audit18 (Claude↔Codex 2라운드 교차검증 합의 — A1~A8).

각 테스트는 수정 전 동작(버그)을 재현하던 시나리오가 이제 올바른지 검증한다.
A1 은 audit17 R1 이 노출시킨 회귀(dev/verify 외부장애 카운터 분리), A2~A8 은 신규 확정 결함.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

import orchestrator.procutil as procutil
from orchestrator.backends import claude_team
from orchestrator.backends.base import RoleRequest
from orchestrator.backends.claude_sdk import _estimate_anthropic_cost
from orchestrator.board import Board
from orchestrator.config import RunConfig
from orchestrator.gitcheckpoints import GitCheckpointer
from orchestrator.scheduler import Scheduler


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args], text=True, capture_output=True, check=False
    )


def _cfg(tmp_path: Path, sample_spec_path: Path, **kw) -> RunConfig:
    base = dict(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        default_backend="mock",
        max_attempts=0,
        auto_commit=False,
    )
    base.update(kw)
    return RunConfig(**base)


# ---------------------------------------------------------------------------
# A1 — dev/verify 외부장애 카운터 분리: dev-repair 성공이 verify 카운터로 누수 안 됨
# ---------------------------------------------------------------------------


def test_a1_dev_repair_external_does_not_leak_into_verify(tmp_path, sample_spec_path):
    async def scenario():
        s = Scheduler(_cfg(tmp_path, sample_spec_path))
        await s.board.init("spec", {})
        await s.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])
        unit = s.board.units()[0]
        # dev repair 진입: 직전 dev 실패가 외부장애(tool_missing)
        s._last_dev_failure["U1"] = {
            "_ok": False,
            "status": "failed",
            "failure_kind": "tool_missing",
            "notes": [],
            "blockers": [],
        }

        async def fake(role, unit=None):
            return {"_ok": True, "status": "done", "artifacts": []}

        s.runner.run_role = fake
        developed = await s._dev_repair_loop(unit, asyncio.Semaphore(1), 2, dict(unit))
        return (
            developed is not None,
            s._external_repeat.get("U1"),
            s._dev_external_repeat.get("U1"),
        )

    ok, verify_ctr, dev_ctr = asyncio.run(scenario())
    assert ok is True
    # 수정 전엔 verify 카운터(_external_repeat)에 1 이 남아 첫 verify 외부장애에서 조기 BLOCK.
    assert verify_ctr is None  # verify 카운터는 dev 수리에 의해 건드려지지 않는다
    assert dev_ctr is None  # dev 카운터도 성공 시 정리됨


# ---------------------------------------------------------------------------
# A2 — gitcheckpoints._run 이 GIT_DIR/GIT_WORK_TREE 를 제거 (무관 repo 오염 방지)
# ---------------------------------------------------------------------------


def test_a2_run_strips_git_env(tmp_path, monkeypatch):
    import shutil

    if not shutil.which("git"):
        pytest.skip("git not available")
    project = tmp_path / "project"
    project.mkdir()
    assert _git(project, "init").returncode == 0
    other = tmp_path / "other"
    other.mkdir()
    assert _git(other, "init").returncode == 0
    # 호출 환경에 stray GIT_DIR/GIT_WORK_TREE 가 other 를 가리키도록 설정
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    cp = GitCheckpointer(project)
    res = cp._run("rev-parse", "--show-toplevel")
    # 수정 전엔 other 가 나왔다. 이제 project 에서 동작해야 한다.
    assert res.returncode == 0
    assert Path(res.stdout.strip()).resolve() == project.resolve()


# ---------------------------------------------------------------------------
# A3 — procutil ps 호출이 LC_ALL=C 로 로케일 고정
# ---------------------------------------------------------------------------


def test_a3_ps_uses_c_locale(monkeypatch):
    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="Tue May 26 00:00:00 2026\n", stderr="")

    monkeypatch.setattr(procutil.subprocess, "run", fake_run)
    procutil._compute_start_token(os.getpid())
    cmd = captured.get("cmd")
    if cmd and cmd[:2] == ["ps", "-o"]:  # macOS/BSD ps 경로를 탔을 때만 검증
        assert captured["env"] is not None
        assert captured["env"].get("LC_ALL") == "C"
    # Linux(/proc) 경로면 ps 미호출 → 로케일 버그 N/A (테스트 통과)


# ---------------------------------------------------------------------------
# A4 — 비-UTF8 .gitignore 의 원본 바이트가 보존됨 (디코드 재기록으로 손상 안 됨)
# ---------------------------------------------------------------------------


def test_a4_non_utf8_gitignore_bytes_preserved(tmp_path):
    from orchestrator import workspace

    project = tmp_path / "proj"
    project.mkdir()
    gi = project / ".gitignore"
    gi.write_bytes(b"latin1-\xff\n")  # 유효하지 않은 UTF-8 바이트 0xff
    workspace.scaffold(project, "spec body", {"language": "python"})
    raw = gi.read_bytes()
    # 원본 0xff 바이트가 그대로 살아 있어야 한다(U+FFFD=EF BF BD 로 치환되면 손상).
    assert b"\xff" in raw
    assert b"\xef\xbf\xbd" not in raw
    # 시드 패턴은 (binary append 로) 추가돼 있어야 한다.
    assert b".orchestrator/" in raw


# ---------------------------------------------------------------------------
# A5 — max_running 이 외부 live pidfile run 을 cap 에 포함
# ---------------------------------------------------------------------------


def test_a5_max_running_counts_external_live_run(tmp_path):
    from orchestrator.webui import RunManager

    # 외부 run 디렉터리: 살아있는 pid(현재 프로세스) + 토큰을 run.pid 에 기록
    ext = tmp_path / "extrun" / ".orchestrator"
    ext.mkdir(parents=True)
    (ext / "run.pid").write_text(procutil.format_pidfile(os.getpid()), encoding="utf-8")

    spawned = {"n": 0}

    def fake_spawn(spec_path, project, opts):
        spawned["n"] += 1

        class _P:
            def poll(self):
                return None

        return _P()

    mgr = RunManager(base_dir=tmp_path, spawn=fake_spawn, max_running=1)
    with pytest.raises(RuntimeError):
        mgr.start("spec", {"mock": True})
    assert spawned["n"] == 0  # cap 에서 막혀 spawn 까지 가지 않음


# ---------------------------------------------------------------------------
# A6 — claude_team 이 --append-system-prompt 로 역할 system_prompt 를 전달
# ---------------------------------------------------------------------------


def test_a6_claude_team_passes_system_prompt(tmp_path, monkeypatch):
    captured = {}

    async def fake_run_subprocess(cmd, *a, **kw):
        captured["cmd"] = cmd
        return (0, b"", b"", False)

    monkeypatch.setattr(claude_team, "run_subprocess", fake_run_subprocess)
    req = RoleRequest(
        role="backend-developer",
        phase="dev",
        unit=None,
        system_prompt="ROLE-SYSTEM-RULES-XYZ",
        prompt="task",
        cwd=tmp_path,
        allowed_tools=["Read"],
        model=None,
        max_turns=5,
        budget=None,
        result_path=tmp_path / "r.json",
        result_rel="r.json",
        spec_text="",
        timeout=30,
    )
    asyncio.run(claude_team.ClaudeTeamBackend().run_role(req))
    cmd = captured["cmd"]
    assert "--append-system-prompt" in cmd
    assert "ROLE-SYSTEM-RULES-XYZ" in cmd


# ---------------------------------------------------------------------------
# A7 — claude_sdk 가 현재 모델(-7) 비용을 추정 (정규식 -\d+ 확장)
# ---------------------------------------------------------------------------


def test_a7_sdk_prices_current_model_minor():
    assert (_estimate_anthropic_cost("claude-opus-4-7", 1000, 500) or 0) > 0
    assert (_estimate_anthropic_cost("claude-sonnet-4-7", 1000, 500) or 0) > 0
    # 진짜 미지/비-숫자 변형은 여전히 None(허위 비용 날조 금지).
    assert _estimate_anthropic_cost("claude-opus-4-abc", 1000, 500) is None


# ---------------------------------------------------------------------------
# A8 — add_cost 가 bool 을 거부 ($1.00 거짓 누적 방지)
# ---------------------------------------------------------------------------


def test_a8_add_cost_rejects_bool(tmp_path):
    async def scenario():
        b = Board(tmp_path / "p")
        await b.init("spec", {})
        await b.add_cost(True)  # 수정 전엔 $1.00 누적
        after_true = b.snapshot().get("total_cost_usd")
        await b.add_cost(1.5)  # 정상 비용은 누적
        after_real = b.snapshot().get("total_cost_usd")
        await b.add_cost(-3.0)  # 음수는 무시
        after_neg = b.snapshot().get("total_cost_usd")
        return after_true, after_real, after_neg

    after_true, after_real, after_neg = asyncio.run(scenario())
    assert after_true == 0.0
    assert after_real == 1.5
    assert after_neg == 1.5
