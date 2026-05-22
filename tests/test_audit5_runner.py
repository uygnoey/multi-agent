"""감사 5차(2026-05-22) 회귀 테스트: 결과 JSON 읽기 크기 가드 (#22).

폭주/악성 역할 세션이 거대한 결과 JSON 을 쓰면 orchestrator 가 read_text() 로 통째 메모리에
올리다 죽을 수 있다. _read_result 는 read 전에 파일 크기를 검사해 상한 초과 시 읽지 않고
계약 위반(실패)으로 처리해야 한다. 상한은 monkeypatch 로 작게 줄여 빠르게 검증한다.
"""

from __future__ import annotations

import json

from orchestrator import runner as runner_mod
from orchestrator.backends.base import RoleResult
from orchestrator.runner import Runner


def test_oversized_result_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "_MAX_RESULT_BYTES", 100)
    p = tmp_path / "result.json"
    # 유효한 JSON 이지만 상한(100B)을 초과 → 읽지 않고 실패 처리되어야 한다.
    p.write_text(json.dumps({"status": "done", "notes": ["x" * 500]}), encoding="utf-8")
    assert p.stat().st_size > 100
    out = Runner._read_result(p, RoleResult(ok=True))
    assert out["_ok"] is False
    assert out["status"] == "failed"
    assert any("too large" in b for b in out["blockers"])


def test_under_cap_result_parsed_normally(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "_MAX_RESULT_BYTES", 100_000)
    p = tmp_path / "result.json"
    p.write_text(json.dumps({"status": "done", "artifacts": ["a.py"]}), encoding="utf-8")
    out = Runner._read_result(p, RoleResult(ok=True), phase=None)
    # 정상 크기는 그대로 파싱·통과한다.
    assert out["_ok"] is True
    assert out["status"] == "done"
    assert "a.py" in out["artifacts"]


def test_default_cap_is_five_mib():
    assert runner_mod._MAX_RESULT_BYTES == 5 * 1024 * 1024


def test_oversized_check_runs_before_parse(tmp_path, monkeypatch):
    # 상한 초과면 내용이 깨진 JSON 이어도 '파싱 실패'가 아니라 'too large' 로 거부(읽기 전 차단).
    monkeypatch.setattr(runner_mod, "_MAX_RESULT_BYTES", 50)
    p = tmp_path / "result.json"
    p.write_text("{not valid json " + "y" * 200, encoding="utf-8")
    out = Runner._read_result(p, RoleResult(ok=True))
    assert out["_ok"] is False
    assert any("too large" in b for b in out["blockers"])
