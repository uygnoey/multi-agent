"""백엔드 레지스트리: 이름 → 인스턴스, + 가용성 조회."""

from __future__ import annotations

from .base import Backend, RoleRequest, RoleResult
from .claude_cli import ClaudeCLIBackend
from .claude_sdk import ClaudeSDKBackend
from .claude_team import ClaudeTeamBackend
from .codex_cli import CodexCLIBackend
from .mock import MockBackend
from .openai_agents import OpenAIAgentsBackend

_REGISTRY: dict[str, Backend] = {
    b.name: b
    for b in [
        MockBackend(),
        ClaudeSDKBackend(),
        ClaudeCLIBackend(),
        ClaudeTeamBackend(),
        OpenAIAgentsBackend(),
        CodexCLIBackend(),
    ]
}


# 공식 명칭 별칭 → 정식 이름 (둘 다 허용)
ALIASES = {
    # Claude Code (Anthropic 공식 CLI)
    "claude-code": "claude-cli",
    "claude-code-cli": "claude-cli",
    # Claude Agent SDK (Anthropic 공식 Python SDK)
    "claude-agent-sdk": "claude-sdk",
    # OpenAI Agents SDK (OpenAI 공식 Python SDK)
    "openai-sdk": "openai-agents",
    "openai-agents-sdk": "openai-agents",
    "openai": "openai-agents",
    # OpenAI Codex CLI (OpenAI 공식 CLI)
    "codex-cli": "codex",
    "openai-codex": "codex",
}


def resolve(name: str) -> str:
    return ALIASES.get(name, name)


def get_backend(name: str) -> Backend:
    canonical = resolve(name)
    if canonical not in _REGISTRY:
        raise ValueError(f"unknown backend: {name} (valid: {', '.join(_REGISTRY)})")
    return _REGISTRY[canonical]


def all_backends() -> dict[str, Backend]:
    return dict(_REGISTRY)


def backend_status() -> list[dict]:
    """각 백엔드의 가용성 [{name, ok, reason}] (--check / web / TUI 공용)."""
    out = []
    for name, b in _REGISTRY.items():
        # #41: 한 백엔드의 available() 예외가 --check / /api/check / TUI 전체를 깨지 않도록 격리.
        try:
            ok, reason = b.available()
        except Exception as e:  # noqa: BLE001
            ok, reason = False, f"availability check failed: {e}"
        out.append({"name": name, "ok": ok, "reason": reason})
    return out


__all__ = [
    "Backend",
    "RoleRequest",
    "RoleResult",
    "get_backend",
    "all_backends",
    "backend_status",
    "resolve",
    "ALIASES",
]
