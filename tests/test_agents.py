"""Tests for orchestrator.agents.load_agent and the frontmatter parser."""

from __future__ import annotations

import pytest

from orchestrator.agents import AgentDef, load_agent
from orchestrator.config import ROLES


@pytest.mark.parametrize("role", sorted(ROLES))
def test_load_each_real_role(role: str):
    agent = load_agent(role)
    assert isinstance(agent, AgentDef)
    assert agent.name == role
    # Real role files carry a substantive system prompt and a tools list.
    assert agent.system_prompt.strip() != ""
    assert isinstance(agent.tools, list)
    assert len(agent.tools) >= 1


def test_load_nonexistent_role_returns_graceful_fallback():
    agent = load_agent("does-not-exist-role")
    assert isinstance(agent, AgentDef)
    assert agent.name == "does-not-exist-role"
    assert agent.tools  # falls back to DEV_TOOLS
    assert "does-not-exist-role" in agent.system_prompt
    # No frontmatter model in the fallback.
    assert agent.model is None


def test_tools_parsed_from_comma_list():
    # backend-developer.md declares: tools: Read, Write, Edit, Bash
    agent = load_agent("backend-developer")
    assert agent.tools == ["Read", "Write", "Edit", "Bash"]


def test_description_is_populated_for_real_role():
    agent = load_agent("architecture-engineer")
    assert agent.description.strip() != ""
