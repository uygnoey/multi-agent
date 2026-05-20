"""백엔드 레지스트리: 이름 → 인스턴스, + 가용성 조회."""
from __future__ import annotations

from .base import Backend, RoleRequest, RoleResult
from .claude_cli import ClaudeCLIBackend
from .claude_sdk import ClaudeSDKBackend
from .codex_cli import CodexCLIBackend
from .mock import MockBackend
from .openai_agents import OpenAIAgentsBackend

_REGISTRY: dict[str, Backend] = {
    b.name: b
    for b in [
        MockBackend(),
        ClaudeSDKBackend(),
        ClaudeCLIBackend(),
        OpenAIAgentsBackend(),
        CodexCLIBackend(),
    ]
}


def get_backend(name: str) -> Backend:
    if name not in _REGISTRY:
        raise ValueError(f"unknown backend: {name} (valid: {', '.join(_REGISTRY)})")
    return _REGISTRY[name]


def all_backends() -> dict[str, Backend]:
    return dict(_REGISTRY)


__all__ = ["Backend", "RoleRequest", "RoleResult", "get_backend", "all_backends"]
