"""감사 15차 회귀 테스트: 교차검증 후속 합의 수정.

- scheduler: 외부장애(Tier A) 반복 카운터를 escalation 시그니처(source 포함)와 분리.
  test-engineer ↔ qa 로 같은 외부장애가 번갈아 나도 2회째 BLOCKED(무한 수리 방지).
- scheduler: write_deliverables 예외가 성공한 run 을 failed 로 만들지 않고 warning 으로 degrade.
- webui: 응답에 보안 헤더(nosniff/X-Frame-Options/CSP/Referrer-Policy) 추가.
- openai-agents: run_bash env 에서 비밀(*_API_KEY 등) 제거, 기본 env 유지.

모두 결정적·오프라인.
"""

from __future__ import annotations

import asyncio
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from orchestrator import webui
from orchestrator.board import BLOCKED, DONE
from orchestrator.config import RunConfig
from orchestrator.scheduler import Scheduler


def _cfg(tmp_path: Path, sample_spec_path: Path, **kw) -> RunConfig:
    base = dict(
        spec_path=sample_spec_path.resolve(),
        project_dir=tmp_path / "p",
        mock=True,
        poll_interval=600.0,
    )
    base.update(kw)
    return RunConfig(**base)


# ---- scheduler: 교대 외부장애가 자동 BLOCKED 되는가 (audit14 회귀 수정) ----
def test_alternating_external_blocker_auto_blocks(tmp_path, sample_spec_path):
    """te→qa 로 같은 tool_missing 외부장애가 번갈아 나도 외부장애 카운터가 2에 도달해 BLOCKED.

    audit14 에서 source 를 시그니처에 넣으면서 교대 시 count 가 리셋돼 BLOCKED 가 안 되던 회귀.
    """

    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path, max_attempts=0))
        await sched.board.init("spec", {})
        await sched.board.add_units([{"id": "U1", "title": "t", "roles": ["frontend-developer"]}])
        te_calls = 0
        qa_calls = 0

        async def fake(role, unit=None):
            nonlocal te_calls, qa_calls
            if role == "test-engineer":
                te_calls += 1
                # 홀수 호출에서 외부장애로 실패(번갈아 te 실패 유도), 백스톱으로 11 초과 시 통과
                if te_calls % 2 == 1 and te_calls <= 11:
                    return {
                        "_ok": False,
                        "status": "failed",
                        "artifacts": [],
                        "failure_kind": "tool_missing",
                    }
                return {"_ok": True, "status": "done", "artifacts": []}
            if role == "qa":
                qa_calls += 1
                # 도달 시 외부장애로 실패(백스톱: 8 초과 시 통과해 무한루프/행 방지)
                if qa_calls <= 8:
                    return {
                        "_ok": False,
                        "status": "failed",
                        "artifacts": [],
                        "failure_kind": "tool_missing",
                    }
                return {"_ok": True, "status": "done", "artifacts": []}
            return {"_ok": True, "status": "done", "artifacts": []}  # dev 는 성공

        sched.runner.run_role = fake
        await sched._test_unit(sched.board.units()[0], asyncio.Semaphore(1), 1)
        return sched.board.snapshot()

    snap = asyncio.run(scenario())
    # 외부장애가 2회째 감지되어 자동 중단(BLOCKED). 백스톱 통과(DONE)에 도달하기 전에 막혀야 한다.
    assert snap["units"][0]["status"] == BLOCKED
    assert any("external" in str(w).lower() for w in snap.get("warnings", []))


# ---- scheduler: write_deliverables 예외가 run 을 죽이지 않음 ----
def test_write_deliverables_failure_degrades_to_warning(tmp_path, sample_spec_path):
    async def scenario():
        sched = Scheduler(_cfg(tmp_path, sample_spec_path))

        def boom():
            raise ValueError("docs deliverables path is a symlink: /x")

        # docs 산출물 작성이 실패해도(예: docs/ symlink) 전체 run 이 failed 되면 안 된다.
        sched.board.write_deliverables = boom  # type: ignore[method-assign]
        return await sched.run()

    snap = asyncio.run(scenario())
    assert snap["phase"] == "done"  # 성공 빌드가 docs 예외로 죽지 않음
    assert all(u["status"] == DONE for u in snap["units"])
    assert any("DELIVERABLES" in str(w) for w in snap.get("warnings", []))


# ---- webui: 보안 헤더 ----
def _make_server(tmp_path):
    manager = webui.RunManager(tmp_path / "runs")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager, None))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, f"http://127.0.0.1:{port}"


def test_responses_carry_security_headers(tmp_path):
    httpd, base = _make_server(tmp_path)
    try:
        req = urllib.request.Request(base + "/", method="GET")
        try:
            with urllib.request.urlopen(req) as r:
                headers = r.headers
        except urllib.error.HTTPError as e:
            headers = e.headers
        assert headers.get("X-Content-Type-Options") == "nosniff"
        assert headers.get("X-Frame-Options") == "DENY"
        assert headers.get("Referrer-Policy") == "no-referrer"
        assert "frame-ancestors 'none'" in (headers.get("Content-Security-Policy") or "")
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---- openai-agents: bash env 비밀 스크럽 ----
def test_scrubbed_bash_env_removes_secrets_keeps_basics(monkeypatch):
    from orchestrator.backends.openai_agents import _scrubbed_bash_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/x")

    env = _scrubbed_bash_env()
    for secret in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MY_SERVICE_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
    ):
        assert secret not in env, f"{secret} should be scrubbed"
    # 기본 동작에 필요한 변수는 유지
    assert env.get("PATH") == "/usr/bin:/bin"
    assert env.get("HOME") == "/home/x"
