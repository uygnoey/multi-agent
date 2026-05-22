"""audit2 — codex_cli.py 백엔드 회귀 테스트 (#143, #144 신규 + 기존 수정 검증).

오프라인·결정적. mock 외 백엔드는 호출하지 않으며 ~/.codex 도 monkeypatch 로 격리한다.
"""

from __future__ import annotations

import asyncio
import shutil

import orchestrator.backends.codex_cli as codex_cli
from orchestrator.backends.base import RoleRequest
from orchestrator.backends.codex_cli import (
    CodexCLIBackend,
    _codex_default_model,
    _price_for,
    _root_model_from_text,
    _sanitize_key,
    codex_cost,
)

# ---------------------------------------------------------------------------
# #143 — _codex_default_model: root table 의 model 만 읽는다
# ---------------------------------------------------------------------------


def _write_codex_config(tmp_path, monkeypatch, text: str):
    """가짜 HOME/.codex/config.toml 을 만들고 Path.home() 를 monkeypatch."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.setattr(codex_cli.Path, "home", classmethod(lambda cls: home))
    return home


def test_default_model_reads_root_table(tmp_path, monkeypatch):
    _write_codex_config(tmp_path, monkeypatch, 'model = "gpt-5.4"\n')
    assert _codex_default_model() == "gpt-5.4"


def test_default_model_ignores_profile_section(tmp_path, monkeypatch):
    # 핵심 버그: 무관한 [profiles.xxx] 섹션의 model 을 전역 기본값으로 오인하면 안 됨
    cfg = (
        "# 전역에는 model 이 없음\n"
        'approval_policy = "never"\n'
        "\n"
        "[profiles.fast]\n"
        'model = "gpt-5.4-nano"\n'
    )
    _write_codex_config(tmp_path, monkeypatch, cfg)
    assert _codex_default_model() == "gpt-5.5"  # 섹션 model 무시 → 기본값


def test_default_model_root_wins_over_later_section(tmp_path, monkeypatch):
    cfg = 'model = "gpt-5.5-pro"\n[profiles.fast]\nmodel = "gpt-5.4-nano"\n'
    _write_codex_config(tmp_path, monkeypatch, cfg)
    assert _codex_default_model() == "gpt-5.5-pro"


def test_default_model_missing_config(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(codex_cli.Path, "home", classmethod(lambda cls: home))
    assert _codex_default_model() == "gpt-5.5"


def test_default_model_ignores_model_lookalike_keys(tmp_path, monkeypatch):
    # model_provider / model_reasoning_effort 같은 키는 model 이 아님
    cfg = 'model_provider = "openai"\nmodel_reasoning_effort = "high"\nmodel = "gpt-5.4"\n'
    _write_codex_config(tmp_path, monkeypatch, cfg)
    assert _codex_default_model() == "gpt-5.4"


def test_root_model_from_text_fallback_directly():
    # tomllib 미가용 경로(3.10) 의 핵심 헬퍼를 직접 검증
    assert _root_model_from_text('model = "gpt-5.4"\n') == "gpt-5.4"
    assert _root_model_from_text('[p.x]\nmodel = "gpt-5.4-nano"\n') is None
    assert _root_model_from_text('model_provider = "x"\n') is None


# ---------------------------------------------------------------------------
# #144 — _price_for: 정확 매칭 우선, 날짜 접미사만 base 로 매핑, 그 외 None
# ---------------------------------------------------------------------------


def test_price_exact_match():
    assert _price_for("gpt-5.5") == (5.0, 0.5, 30.0)
    assert _price_for("gpt-5.5-pro") == (30.0, 30.0, 180.0)


def test_price_dated_snapshot_maps_to_base():
    # gpt-5.5-2026... 는 base gpt-5.5 단가로 (date 접미사만 붙은 경우)
    assert _price_for("gpt-5.5-2026-05-21") == (5.0, 0.5, 30.0)
    # pro 의 dated 스냅샷은 pro 단가로 (긴 키 우선 → gpt-5.5 로 새지 않음)
    assert _price_for("gpt-5.5-pro-2026") == (30.0, 30.0, 180.0)


def test_price_unknown_longer_variant_is_none():
    # 핵심 버그: 알 수 없는 더 긴 변형이 짧은 키로 조용히 과금되면 안 됨
    assert _price_for("gpt-5.5-turbo") is None
    assert _price_for("gpt-5.5-pro-max") is None
    assert _price_for("gpt-5.5x") is None


def test_price_unknown_model_is_none():
    assert _price_for("totally-unknown") is None
    assert _price_for("") is None
    assert _price_for(None) is None


def test_codex_cost_consistency_with_pricing():
    # 기존 test_hardening 의 기대치와 동일해야 한다
    assert codex_cost("gpt-5.5", 100_000, 20_000, 10_000) == 0.71
    assert codex_cost("gpt-5.5-pro-2026", 1000, 0, 1000) is not None  # dated → 매핑
    assert codex_cost("gpt-5.5-turbo", 1000, 0, 1000) is None  # 미지정 → None


# ---------------------------------------------------------------------------
# 기존 수정 검증 (#43, #108, #111, #115, #119) — 회귀 방지
# ---------------------------------------------------------------------------


def test_already_fixed_108_sanitize_key():
    # #108: out_path 키 경로 탈출 방지
    for bad in ["../../etc/passwd", "a/b", "..", "/abs", "a\\b"]:
        s = _sanitize_key(bad)
        assert "/" not in s and "\\" not in s
        assert not s.startswith(".") and not s.startswith("-")
    assert _sanitize_key("") == "unit"


def test_already_fixed_111_available_auth_not_verified(monkeypatch):
    # #111: 바이너리만 있어도 auth 는 검증 안 함을 정직하게 명시
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    ok, reason = CodexCLIBackend().available()
    assert ok is True
    assert "auth NOT verified" in reason


def test_already_fixed_111_available_missing_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    ok, reason = CodexCLIBackend().available()
    assert ok is False
    assert "codex" in reason.lower()


def test_already_fixed_43_stderr_keeps_tail_not_head(tmp_path, monkeypatch):
    # #43/#24: 실패한 codex 의 stderr 는 진단의 '끝부분'이 중요하므로 head 가 아니라 tail(마지막
    # 4000자, err[-4000:])을 보존해야 한다. head≠tail 인 입력으로 실제 동작을 검증한다.
    head = "HEAD_MARKER_" + ("h" * 5000)  # 앞쪽 5000자 영역
    tail = ("t" * 5000) + "_TAIL_MARKER"  # 뒤쪽 5000자 영역
    long_err = (head + tail).encode()
    assert len(long_err) > 4000

    async def fake_run_subprocess(cmd, cwd, timeout, live_log_path):
        return 1, b"", long_err, False  # rc!=0, timed_out=False

    monkeypatch.setattr(codex_cli, "run_subprocess", fake_run_subprocess)

    req = RoleRequest(
        role="backend-developer",
        phase="dev",
        unit={"id": "u1"},
        system_prompt="role",
        prompt="task",
        cwd=tmp_path,
        allowed_tools=["Read", "Write", "Edit", "Bash"],
        model=None,
        max_turns=8,
        budget=None,
        result_path=tmp_path / ".orchestrator" / "results" / "r.json",
        result_rel=".orchestrator/results/r.json",
        spec_text="spec",
    )
    res = asyncio.run(CodexCLIBackend().run_role(req))

    assert res.ok is False
    expected_tail = long_err.decode(errors="replace")[-4000:]
    # tail 절단이어야 한다: 결과는 마지막 4000자와 정확히 일치한다.
    assert res.error == expected_tail
    # 끝부분 마커는 살아남고, 앞부분 마커는 잘려나가야 한다(head 절단이 아님을 보장).
    assert res.error.endswith("_TAIL_MARKER")
    assert "HEAD_MARKER_" not in res.error
    assert len(res.error) == 4000


def test_codex_budget_and_max_turns_are_not_forwarded(tmp_path, monkeypatch):
    captured = {}

    async def fake_run(cmd, cwd, timeout, log_path=None):
        captured["cmd"] = cmd
        return 0, b'{"type":"turn.completed","usage":{}}\n', b"", False

    monkeypatch.setattr(codex_cli, "run_subprocess", fake_run)
    req = RoleRequest(
        role="backend-developer",
        phase="dev",
        unit={"id": "U1"},
        system_prompt="sys",
        prompt="prompt",
        cwd=tmp_path,
        allowed_tools=["Read", "Write"],
        model=None,
        max_turns=8,
        budget=4.2,
        result_path=tmp_path / ".orchestrator" / "results" / "r.json",
        result_rel=".orchestrator/results/r.json",
        spec_text="spec",
    )
    res = asyncio.run(CodexCLIBackend().run_role(req))

    assert res.ok is True
    assert "--max-turns" not in captured["cmd"]
    assert "--max-budget-usd" not in captured["cmd"]
    assert "--budget" not in captured["cmd"]
