"""Regression tests for audit19 (Claude↔Codex 교차검증 합의 — F1~F4, P1~P4, C1/C2/C3/C5).

이 세션의 feature-mode/4언어 추가에서 도입·노출된 결함(F/C 계열) + 기존 잔존 결함(P 계열).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from orchestrator import workspace
from orchestrator.backends import mock as mock_mod
from orchestrator.backends.base import RoleRequest
from orchestrator.board import Board
from orchestrator.config import RunConfig
from orchestrator.gitcheckpoints import GitCheckpointer
from orchestrator.monitor import _read_pid
from orchestrator.scheduler import Scheduler
from orchestrator.webui import RunManager, build_command, sanitize_run_opts


# ---------------------------------------------------------------------------
# F1 — feature spec must not self-ingest the prior composed .orchestrator/spec.md
# ---------------------------------------------------------------------------
def test_f1_feature_spec_no_self_ingest(tmp_path):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "x.py").write_text("print(1)\n", encoding="utf-8")
    cfg = RunConfig(
        spec_path=proj / ".orchestrator" / "spec.md",
        project_dir=proj,
        mock=True,
        feature="add feature Z",
    )
    s = Scheduler(cfg)
    spec1 = s._compose_feature_spec()
    # simulate run()/scaffold writing the composed spec to the placeholder path
    (proj / ".orchestrator").mkdir(parents=True, exist_ok=True)
    (proj / ".orchestrator" / "spec.md").write_text(spec1, encoding="utf-8")
    spec2 = s._compose_feature_spec()
    # 두 번째 합성이 이전 합성 spec 을 흡수하지 않는다(마커 1회, "Additional spec" 없음).
    assert spec2.count("INCREMENTAL FEATURE REQUEST") == 1
    assert "Additional spec / context" not in spec2
    assert len(spec2) <= len(spec1) + 50  # 안정(자기흡수로 2배 커지지 않음)


# ---------------------------------------------------------------------------
# F2 — max_running cap scan must ignore symlinked base_dir entries
# ---------------------------------------------------------------------------
def test_f2_max_running_ignores_symlink(tmp_path):
    ext = tmp_path / "ext" / ".orchestrator"
    ext.mkdir(parents=True)
    import orchestrator.procutil as procutil

    (ext / "run.pid").write_text(procutil.format_pidfile(os.getpid()), encoding="utf-8")
    base = tmp_path / "base"
    base.mkdir()
    try:
        (base / "sneaky").symlink_to(tmp_path / "ext", target_is_directory=True)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlinks not supported")

    spawned = {"n": 0}

    def fake_spawn(cmd, log_path):
        spawned["n"] += 1

        class _P:
            def poll(self):
                return None

        return _P()

    mgr = RunManager(base_dir=base, spawn=fake_spawn, max_running=1)
    # 심볼릭 외부 run 은 cap 에 안 잡혀야 한다 → cap(1) 미도달 → start 성공.
    mgr.start("spec", {"mock": True})
    assert spawned["n"] == 1


# ---------------------------------------------------------------------------
# F3 — gather_repo_context caps key-file excerpt (no full-file load)
# ---------------------------------------------------------------------------
def test_f3_gather_caps_key_file(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "package.json").write_text("x" * 200_000, encoding="utf-8")
    ctx = workspace.gather_repo_context(proj)
    assert "package.json" in ctx
    assert len(ctx) < 50_000  # 발췌가 캡되어 200KB 통째로 들어가지 않음


# ---------------------------------------------------------------------------
# F4 — gitcheckpoints._run neutralizes GIT_CONFIG_GLOBAL + strips GIT_DIR
# ---------------------------------------------------------------------------
def test_f4_git_run_neutralizes_env(tmp_path, monkeypatch):
    import orchestrator.gitcheckpoints as gc

    captured = {}

    def fake_run(cmd, *a, **kw):
        captured["env"] = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setenv("GIT_DIR", "/some/other/.git")
    monkeypatch.setattr(gc.subprocess, "run", fake_run)
    GitCheckpointer(tmp_path / "p")._run("status")
    env = captured["env"]
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GIT_DIR" not in env
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"


# ---------------------------------------------------------------------------
# P1 — test/config repair test-engineer call is inside the _test_sem block
# ---------------------------------------------------------------------------
def test_p1_repair_te_under_test_sem():
    import orchestrator.scheduler as sched_mod

    src = Path(sched_mod.__file__).read_text(encoding="utf-8")
    i = src.index("test/config repair attempt")
    seg = src[i : i + 600]
    # repair 분기의 test-engineer 호출이 _test_sem 블록 안에 있어야 한다.
    sem_pos = seg.find("async with self._test_sem")
    te_pos = seg.find('run_role("test-engineer", target)')
    assert sem_pos != -1 and te_pos != -1 and sem_pos < te_pos


# ---------------------------------------------------------------------------
# P2 — monitor._read_pid rejects out-of-C-long PIDs
# ---------------------------------------------------------------------------
def test_p2_read_pid_bounds(tmp_path):
    pf = tmp_path / "run.pid"
    for bad in ("99999999999999999999", "2147483648", "0", "-1"):
        pf.write_text(bad + "\n", encoding="utf-8")
        assert _read_pid(pf) is None, bad
    pf.write_text("4321\n", encoding="utf-8")
    assert _read_pid(pf) == 4321


# ---------------------------------------------------------------------------
# P3 — sanitize_run_opts validates/canonicalizes backend (rejects "--mock")
# ---------------------------------------------------------------------------
def test_p3_sanitize_validates_backend():
    out = sanitize_run_opts({"backend": "--mock"})
    assert out["backend"] == "mock"  # invalid → falls back, never reaches argv as "--mock"
    out2 = sanitize_run_opts({"backend": "claude-code"})  # alias resolves to canonical
    assert out2["backend"] in {"claude-cli", "claude-team", "claude-sdk", "codex", "mock"}
    out3 = sanitize_run_opts({"backends": ["--evil", "mock", 123]})
    assert out3["backends"] == ["mock"]  # invalid/non-str dropped


# ---------------------------------------------------------------------------
# P4 — add_units: distinct raw ids that sanitize-collide are both kept (renamed)
# ---------------------------------------------------------------------------
def test_p4_add_units_collision_keeps_both(tmp_path):
    async def scenario():
        b = Board(tmp_path / "p")
        await b.init("spec", {})
        await b.add_units([{"id": "a/b", "title": "one"}, {"id": "a-b", "title": "two"}])
        return [(u["id"], u["title"]) for u in b.units()]

    units = asyncio.run(scenario())
    ids = {uid for uid, _ in units}
    assert "a-b" in ids and "a-b-2" in ids  # 둘 다 보존(순서의존 유실 없음)
    assert len(units) == 2


# ---------------------------------------------------------------------------
# C1 — bundled docs-writer.md instructs 4 languages
# ---------------------------------------------------------------------------
def test_c1_docs_writer_def_four_languages():
    txt = (workspace.AGENTS_DIR / "docs-writer.md").read_text(encoding="utf-8")
    for lang in ("Korean", "English", "Japanese", "Spanish"):
        assert lang in txt
    for suf in (".ko.md", ".ja.md", ".es.md"):
        assert suf in txt


# ---------------------------------------------------------------------------
# C2 — expose_team_agents refreshes marked copies, preserves user-authored
# ---------------------------------------------------------------------------
def test_c2_expose_refreshes_marked_preserves_user(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    workspace.expose_team_agents(proj)
    dw = proj / ".claude" / "agents" / "docs-writer.md"
    body = dw.read_text(encoding="utf-8")
    assert workspace._AGENT_GEN_MARKER in body
    assert "Japanese" in body  # C1 4언어 정의가 전파됨

    # 사용자 작성본(마커 없음)은 보존
    custom = "# my custom docs-writer\n"
    dw.write_text(custom, encoding="utf-8")
    workspace.expose_team_agents(proj)
    assert dw.read_text(encoding="utf-8") == custom

    # 마커 있는 stale 복사본은 최신 정의로 갱신
    dw.write_text("# old stale\n\n" + workspace._AGENT_GEN_MARKER + "\n", encoding="utf-8")
    workspace.expose_team_agents(proj)
    assert "Japanese" in dw.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# C3 — mock docs-writer emits 4 languages (.md/.ko/.ja/.es)
# ---------------------------------------------------------------------------
def test_c3_mock_docs_four_languages(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    req = RoleRequest(
        role="docs-writer",
        phase="docs",
        unit=None,
        system_prompt="",
        prompt="docs",
        cwd=proj,
        allowed_tools=["Write"],
        model=None,
        max_turns=5,
        budget=None,
        result_path=proj / ".orchestrator" / "results" / "docs.json",
        result_rel=".orchestrator/results/docs.json",
        spec_text="",
        timeout=30,
    )
    asyncio.run(mock_mod.MockBackend().run_role(req))
    docs = proj / "docs"
    assert (docs / "RUN_GUIDE.ja.md").exists()
    assert (docs / "API.es.md").exists()
    assert (docs / "ARCHITECTURE.ko.md").exists()


# ---------------------------------------------------------------------------
# C5 — build_command wires --feature for web/API feature mode
# ---------------------------------------------------------------------------
def test_c5_build_command_includes_feature():
    cmd = build_command(
        "py", Path("/tmp/s"), Path("/tmp/p"), {"backend": "mock", "feature": "add live LLM"}
    )
    assert "--feature=add live LLM" in cmd
    # feature 없으면 안 들어감
    cmd2 = build_command("py", Path("/tmp/s"), Path("/tmp/p"), {"backend": "mock"})
    assert not any(c.startswith("--feature") for c in cmd2)
