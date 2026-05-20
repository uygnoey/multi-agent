"""Shared pytest fixtures for the orchestrator test suite.

All tests are deterministic, offline, and require no API keys: only the
`mock` backend is exercised. Project directories always live under
``tmp_path`` so nothing is written into the repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.config import FRAMEWORK_ROOT

# Absolute path to the bundled sample spec used by end-to-end / CLI tests.
SAMPLE_SPEC = FRAMEWORK_ROOT / "examples" / "specs" / "sample-spec.md"


@pytest.fixture
def sample_spec_path() -> Path:
    """Absolute path to examples/specs/sample-spec.md (must exist)."""
    assert SAMPLE_SPEC.exists(), f"sample spec missing: {SAMPLE_SPEC}"
    return SAMPLE_SPEC


@pytest.fixture
def spec_text(sample_spec_path: Path) -> str:
    return sample_spec_path.read_text(encoding="utf-8")
