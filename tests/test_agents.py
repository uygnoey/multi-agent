"""Tests for orchestrator.agents.load_agent and the frontmatter parser."""

from __future__ import annotations

import pytest

from orchestrator import agents as agents_mod
from orchestrator.agents import AgentDef, load_agent
from orchestrator.config import DEV_TOOLS, RO_TOOLS, ROLES


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


# #RA-tools: 변조/과대 .md frontmatter 가 supervisor(PM/PL=RO_TOOLS)에게 Write/Bash 를 추가하지
# 못한다(교집합으로 제거). dev 역할은 자신의 전체 tool 셋을 그대로 유지한다.
def _write_agent_md(tmp_path, role, tools):
    (tmp_path / f"{role}.md").write_text(
        f"---\nname: {role}\ndescription: x\ntools: {tools}\n---\nbody\n",
        encoding="utf-8",
    )


def test_supervisor_md_cannot_grant_write_bash(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)
    # 변조된 .md: supervisor 인데 Write/Bash 를 선언.
    _write_agent_md(tmp_path, "project-manager", "Read, Write, Edit, Bash")
    agent = load_agent("project-manager")
    assert agent.tools == list(RO_TOOLS)
    assert "Write" not in agent.tools
    assert "Bash" not in agent.tools
    assert "Edit" not in agent.tools


def test_dev_md_keeps_full_tool_set(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)
    _write_agent_md(tmp_path, "backend-developer", "Read, Write, Edit, Bash")
    agent = load_agent("backend-developer")
    assert agent.tools == list(DEV_TOOLS)


def test_md_cannot_add_tools_beyond_role_for_dev(tmp_path, monkeypatch):
    # dev .md 가 알 수 없는 추가 tool 을 선언해도 교집합으로 제거된다(역할 선언 범위 밖 불가).
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)
    _write_agent_md(tmp_path, "qa", "Read, Write, Edit, Bash, NetAccess")
    agent = load_agent("qa")
    assert "NetAccess" not in agent.tools
    assert agent.tools == list(DEV_TOOLS)


# #RA-agread: 비-UTF8/손상 .md 도 errors="replace" 로 UnicodeDecodeError 없이 로드된다.
def test_load_agent_tolerates_non_utf8_md(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)
    # 유효 frontmatter 뒤 본문에 비-UTF8 바이트(0xFF)를 섞는다.
    (tmp_path / "backend-developer.md").write_bytes(
        b"---\nname: backend-developer\ntools: Read, Write, Edit, Bash\n---\nbody \xff\xfe end\n"
    )
    agent = load_agent("backend-developer")  # raise 하지 않아야 한다
    assert agent.name == "backend-developer"
    assert agent.tools == list(DEV_TOOLS)
