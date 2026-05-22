"""Packaging/config guards for the fixes in issue.20260522.02 (#2, #10, #42).

These tests only read repo-root config/docs files (pyproject.toml, MANIFEST.in, Dockerfile,
.github/workflows/ci.yml). They touch no other modules, so they stay self-contained.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_pyproject() -> dict:
    # #19: tomllib(3.11+) 우선, 없으면 tomli 폴백 → Python 3.10 에서도 pyproject 검증이
    #      skip 되지 않게 한다. 둘 다 없을 때만 skip(커버리지 구멍 최소화).
    try:
        import tomllib as _toml
    except ModuleNotFoundError:
        try:
            import tomli as _toml
        except ModuleNotFoundError:
            pytest.skip("need tomllib (Python 3.11+) or tomli")
    return _toml.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_is_valid_toml():
    data = _load_pyproject()
    assert data["project"]["name"] == "dev-crew-orchestrator"


def test_pyproject_bundles_runtime_dirs_for_wheel_and_sdist():
    # #2: the wheel must ship .claude/agents + templates somewhere (data-files).
    data = _load_pyproject()
    data_files = data["tool"]["setuptools"].get("data-files", {})
    joined = (
        " ".join(k for k in data_files)
        + " "
        + " ".join(v for vals in data_files.values() for v in vals)
    )
    assert ".claude/agents" in joined
    assert "templates" in joined
    # force-include is NOT valid in the declarative pyproject table; guard against regressing to it.
    assert "force-include" not in data["tool"]["setuptools"]
    # setuptools requirement must be new enough for the packaging features used.
    requires = " ".join(data["build-system"]["requires"])
    assert "setuptools" in requires


def test_manifest_includes_runtime_dirs():
    # #2: the sdist manifest must include both runtime dirs.
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert ".claude/agents" in manifest
    assert "templates" in manifest


def test_dockerfile_has_require_all_backends_arg():
    # #10: opt-in hard failure for [all] install + a prominent WARNING in the soft path.
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG REQUIRE_ALL_BACKENDS" in dockerfile
    assert "WARNING" in dockerfile
    # #17: keep the unauthenticated-UI / 0.0.0.0 security warning present.
    assert "0.0.0.0" in dockerfile
    assert "127.0.0.1:8765:8765" in dockerfile


def test_ci_uses_module_pytest():
    # #42: CI must invoke `python -m pytest` (not bare pytest) and run ruff lint + format.
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "python -m pytest" in ci
    assert "ruff check ." in ci
    assert "ruff format --check ." in ci
