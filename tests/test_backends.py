"""Tests for orchestrator.backends registry and the mock backend."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from orchestrator.backends import all_backends, get_backend
from orchestrator.backends.base import RoleRequest
from orchestrator.backends.mock import MockBackend
from orchestrator.config import VALID_BACKENDS


def test_get_backend_mock_is_available():
    backend = get_backend("mock")
    assert isinstance(backend, MockBackend)
    ok, reason = backend.available()
    assert ok is True
    assert isinstance(reason, str) and reason


def test_all_backends_has_all_six():
    backends = all_backends()
    assert set(backends) == set(VALID_BACKENDS)
    assert len(backends) == 6


def test_get_backend_unknown_raises():
    with pytest.raises(ValueError):
        get_backend("nope")


def _make_request(tmp_path: Path, role: str, unit: dict | None) -> RoleRequest:
    key = unit["id"] if unit else "global"
    result_rel = f".orchestrator/results/{role}__{key}.json"
    return RoleRequest(
        role=role,
        phase="dev",
        unit=unit,
        system_prompt="you are a test agent",
        prompt="do the thing",
        cwd=tmp_path,
        allowed_tools=["Read", "Write"],
        model=None,
        max_turns=20,
        budget=None,
        result_path=tmp_path / result_rel,
        result_rel=result_rel,
        spec_text="- feature one\n- feature two\n",
    )


def test_role_request_defaults():
    # delegate / teammates have sensible defaults.
    req = _make_request(Path("/tmp"), "backend-developer", {"id": "U1", "title": "x"})
    assert req.delegate is False
    assert req.teammates == []


def test_mock_run_role_backend_developer_writes_result_and_artifact(tmp_path: Path):
    unit = {"id": "U1", "title": "Auth", "description": "user auth", "deps": []}
    req = _make_request(tmp_path, "backend-developer", unit)
    backend = get_backend("mock")

    res = asyncio.run(backend.run_role(req))

    assert res.ok is True
    assert res.cost_usd == 0.0

    # Result JSON written and valid.
    assert req.result_path.exists()
    data = json.loads(req.result_path.read_text(encoding="utf-8"))
    assert data["status"] == "dev_done"
    assert data["artifacts"]

    # An artifact file was created under the target cwd.
    artifact_rel = data["artifacts"][0]
    artifact_path = tmp_path / artifact_rel
    assert artifact_path.exists()
    assert "U1" in artifact_path.read_text(encoding="utf-8")
    # backend-developer writes under backend/.
    assert artifact_rel.startswith("backend/")


def test_mock_architect_emits_units(tmp_path: Path):
    req = _make_request(tmp_path, "architecture-engineer", None)
    res = asyncio.run(get_backend("mock").run_role(req))
    assert res.ok
    data = json.loads(req.result_path.read_text(encoding="utf-8"))
    assert data["status"] == "designed"
    assert isinstance(data.get("units"), list) and data["units"]
    assert data["units"][0]["id"] == "U1"


def test_mock_supervisor_writes_no_result_file(tmp_path: Path):
    req = _make_request(tmp_path, "project-manager", None)
    res = asyncio.run(get_backend("mock").run_role(req))
    assert res.ok
    # supervisors only produce a directive message, no result JSON.
    assert not req.result_path.exists()
