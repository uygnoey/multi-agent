"""Tests for orchestrator.config: ROLES, RunConfig, backend lists, role groups."""

from __future__ import annotations

from pathlib import Path

from orchestrator import config
from orchestrator.config import (
    DESIGN_ROLES,
    DEV_ROLES,
    ROLES,
    SUPERVISOR_ROLES,
    VALID_BACKENDS,
    RoleSpec,
    RunConfig,
)


def _make_cfg(**kw) -> RunConfig:
    base = dict(spec_path=Path("spec.md"), project_dir=Path("proj"))
    base.update(kw)
    return RunConfig(**base)


def test_roles_is_dict_of_rolespecs():
    assert isinstance(ROLES, dict)
    assert len(ROLES) == 10
    for name, spec in ROLES.items():
        assert isinstance(spec, RoleSpec)
        assert spec.name == name
        assert spec.phase
        assert isinstance(spec.tools, tuple) and spec.tools


def test_runconfig_defaults():
    cfg = _make_cfg()
    assert cfg.default_backend == "mock"
    assert cfg.role_backend == {}
    assert cfg.max_units is None
    assert cfg.concurrency == 3
    assert cfg.budget is None
    assert cfg.model is None
    assert cfg.poll_interval == 20.0
    assert cfg.mock is False
    assert cfg.delegate is False
    assert cfg.max_attempts == 2
    assert cfg.retries == 1


def test_backend_for_mock_overrides_everything():
    cfg = _make_cfg(
        mock=True,
        default_backend="claude-cli",
        role_backend={"backend-developer": "codex"},
    )
    # mock=True wins regardless of role or overrides.
    assert cfg.backend_for("backend-developer") == "mock"
    assert cfg.backend_for("project-manager") == "mock"
    assert cfg.backend_for("anything") == "mock"


def test_backend_for_role_override():
    cfg = _make_cfg(
        default_backend="claude-cli",
        role_backend={"backend-developer": "codex"},
    )
    assert cfg.backend_for("backend-developer") == "codex"
    # No override -> default backend.
    assert cfg.backend_for("frontend-developer") == "claude-cli"


def test_backend_for_falls_back_to_default():
    cfg = _make_cfg(default_backend="claude-sdk")
    assert cfg.backend_for("dba") == "claude-sdk"


def test_valid_backends_contents():
    assert set(VALID_BACKENDS) == {
        "claude-sdk",
        "claude-cli",
        "claude-team",
        "openai-agents",
        "codex",
        "mock",
    }
    assert "claude-team" in VALID_BACKENDS
    assert len(VALID_BACKENDS) == 6


def test_role_group_contents():
    assert SUPERVISOR_ROLES == ["project-manager", "project-leader"]
    # The architect MUST be first so the scheduler reads units from results[0].
    assert DESIGN_ROLES[0] == "architecture-engineer"
    assert DESIGN_ROLES == ["architecture-engineer", "testsheet-creator"]
    assert DEV_ROLES == ["frontend-developer", "backend-developer", "dba"]


def test_default_backend_constant():
    assert config.DEFAULT_BACKEND == "mock"


def test_model_for_returns_configured_model():
    cfg = _make_cfg(model="some-model")
    assert cfg.model_for("mock") == "some-model"
    assert _make_cfg().model_for("mock") is None
