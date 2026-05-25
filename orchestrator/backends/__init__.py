"""백엔드 레지스트리: 이름 → 인스턴스, + 가용성 조회."""

from __future__ import annotations

from .base import Backend, RoleRequest, RoleResult
from .claude_cli import ClaudeCLIBackend
from .claude_sdk import ClaudeSDKBackend
from .claude_team import ClaudeTeamBackend
from .codex_cli import CodexCLIBackend
from .mock import MockBackend
from .openai_agents import OpenAIAgentsBackend


def _build_registry(backends: list[Backend]) -> dict[str, Backend]:
    """이름 → 인스턴스 매핑을 만들되, b.name 중복을 조용히 덮어쓰지 않고 즉시 에러로 막는다.

    #8(audit9): dict 컴프리헨션은 같은 b.name 을 가진 백엔드가 둘이면 뒤엣것이 앞엣것을
    조용히 덮어써 한 백엔드가 사라진다. 등록 시점에 충돌을 감지해 ValueError 로 표면화한다.
    """
    registry: dict[str, Backend] = {}
    for b in backends:
        if b.name in registry:
            raise ValueError(f"duplicate backend name in registry: {b.name!r}")
        registry[b.name] = b
    return registry


_REGISTRY: dict[str, Backend] = _build_registry(
    [
        MockBackend(),
        ClaudeSDKBackend(),
        ClaudeCLIBackend(),
        ClaudeTeamBackend(),
        OpenAIAgentsBackend(),
        CodexCLIBackend(),
    ]
)


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
        # #8(audit9): 정식 이름뿐 아니라 허용되는 별칭(ALIASES)도 함께 안내해, 사용자가
        # 'openai'/'claude-code' 같은 유효 별칭을 모른 채 헤매지 않게 한다.
        valid_names = ", ".join(_REGISTRY)
        valid_aliases = ", ".join(sorted(ALIASES))
        raise ValueError(
            f"unknown backend: {name} (valid: {valid_names}; aliases: {valid_aliases})"
        )
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
