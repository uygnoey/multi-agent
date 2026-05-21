"""백엔드 상태 조회 + 별칭 해석 테스트."""

from __future__ import annotations

from orchestrator.backends import ALIASES, all_backends, backend_status, get_backend, resolve


def test_backend_status_lists_all_and_mock_ok():
    rows = backend_status()
    assert len(rows) == len(all_backends())
    by = {r["name"]: r for r in rows}
    assert by["mock"]["ok"] is True
    for r in rows:
        assert set(r) >= {"name", "ok", "reason"}


def test_aliases_resolve_to_canonical():
    assert resolve("claude-code") == "claude-cli"
    assert resolve("openai-sdk") == "openai-agents"
    assert resolve("mock") == "mock"  # non-alias passes through
    assert get_backend("claude-code").name == "claude-cli"
    assert get_backend("openai-sdk").name == "openai-agents"
    assert "claude-code" in ALIASES and "openai-sdk" in ALIASES
