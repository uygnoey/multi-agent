"""교차검증(2026-05-23) 확정 HIGH: agents.load_agent fail-open 권한.

정의 파일(.claude/agents/<role>.md)이 없을 때 무조건 DEV_TOOLS 를 주면 read-only 여야 할
supervisor(PM/PL)가 Write/Edit/Bash 권한을 얻는 권한 상승이 된다. 폴백은 역할 선언 도구
(ROLES[role].tools)를 따라야 한다. AGENTS_DIR 을 빈 tmp 로 monkeypatch 해 '파일 없음' 경로를 강제.
"""

from __future__ import annotations

import orchestrator.agents as agents_mod
from orchestrator.config import DEV_TOOLS, RO_TOOLS, ROLES


def test_missing_supervisor_md_does_not_grant_dev_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)  # 빈 디렉터리 → 모든 .md 없음
    for sup in ("project-manager", "project-leader"):
        a = agents_mod.load_agent(sup)
        assert a.tools == list(RO_TOOLS), sup
        assert "Bash" not in a.tools and "Write" not in a.tools and "Edit" not in a.tools


def test_missing_dev_role_md_keeps_dev_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)
    a = agents_mod.load_agent("backend-developer")
    assert a.tools == list(DEV_TOOLS)


def test_missing_md_falls_back_to_declared_role_tools(tmp_path, monkeypatch):
    # 모든 알려진 역할: 파일 없을 때 폴백 tools == 그 역할이 선언한 tools.
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)
    for role, spec in ROLES.items():
        a = agents_mod.load_agent(role)
        assert a.tools == list(spec.tools), role


def test_unknown_role_missing_md_defaults_dev_tools(tmp_path, monkeypatch):
    # ROLES 에 없는 역할은 (정상 흐름엔 없지만) 기존처럼 DEV_TOOLS 폴백.
    monkeypatch.setattr(agents_mod, "AGENTS_DIR", tmp_path)
    a = agents_mod.load_agent("totally-unknown-role")
    assert a.tools == list(DEV_TOOLS)
