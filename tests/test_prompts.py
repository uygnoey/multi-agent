"""Tests for orchestrator.prompts.compose_prompt."""

from __future__ import annotations

from orchestrator.prompts import compose_prompt


def test_includes_result_rel_path():
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit=None,
        directives="",
        result_rel=".orchestrator/results/backend-developer__U1.json",
        spec_excerpt="some spec",
    )
    assert ".orchestrator/results/backend-developer__U1.json" in out
    assert out.startswith("# Role: backend-developer")


def test_includes_unit_id_and_title_when_unit_given():
    unit = {"id": "U2", "title": "Task CRUD", "description": "create/read", "deps": ["U1"]}
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit=unit,
        directives="",
        result_rel="res.json",
        spec_excerpt="ignored when unit present",
    )
    assert "## Target work unit" in out
    assert "U2" in out
    assert "Task CRUD" in out
    assert "['U1']" in out  # deps rendered
    # When a unit is targeted there is no whole-spec scope/excerpt block.
    assert "## Scope" not in out
    assert "Spec excerpt" not in out


def test_unit_fields_are_capped():
    unit = {
        "id": "U2",
        "title": "T" * 500,
        "description": "D" * 5000,
        "deps": ["U" + str(i) for i in range(500)],
    }
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit=unit,
        directives="",
        result_rel="res.json",
        spec_excerpt="ignored when unit present",
    )

    target = out.split("## Target work unit\n", 1)[1].split("## Instruction", 1)[0]
    assert "…(truncated)" in target
    assert target.count("T") == 200
    assert target.count("D") == 1500
    assert len(target) < 2500


def test_repair_context_is_included_for_targeted_fix():
    unit = {
        "id": "U2",
        "title": "Backend",
        "repair_context": "failure_kind: test_harness\nrepair_instruction: move Payload model",
    }
    out = compose_prompt(
        role="test-engineer",
        phase="test",
        unit=unit,
        directives="",
        result_rel="res.json",
        spec_excerpt="ignored when unit present",
    )

    assert "## Previous verification failure to fix" in out
    assert "failure_kind: test_harness" in out
    assert "move Payload model" in out
    assert "fix broken tests/test configuration" in out


def test_spec_excerpt_and_scope_when_unit_none():
    out = compose_prompt(
        role="architecture-engineer",
        phase="design",
        unit=None,
        directives="",
        result_rel="res.json",
        spec_excerpt="This is the project spec body.",
    )
    assert "## Scope" in out
    assert "The entire spec." in out
    assert "## Spec excerpt" in out
    assert "This is the project spec body." in out


def test_architect_instruction_mentions_units():
    out = compose_prompt(
        role="architecture-engineer",
        phase="design",
        unit=None,
        directives="",
        result_rel="res.json",
        spec_excerpt="spec",
    )
    assert "## Instruction" in out
    # architect instruction decomposes the spec into work units.
    assert "units" in out.lower()


def test_directives_block_included_and_truncated():
    long_dir = "X" * 5000
    out = compose_prompt(
        role="project-manager",
        phase="supervisor",
        unit=None,
        directives=long_dir,
        result_rel="res.json",
        spec_excerpt="",
    )
    assert "## PM/PL directives (latest)" in out
    # directives are truncated to the last 2000 chars.
    assert out.count("X") == 2000


def test_recent_events_block_optional():
    base_kwargs = dict(
        role="backend-developer",
        phase="dev",
        unit=None,
        directives="",
        result_rel="res.json",
        spec_excerpt="spec",
    )
    without = compose_prompt(recent_events="", **base_kwargs)
    assert "## Recent events" not in without
    with_events = compose_prompt(recent_events="09:00 [board] initialized", **base_kwargs)
    assert "## Recent events" in with_events
    assert "initialized" in with_events


def test_completion_level_changes_verification_guidance():
    mvp = compose_prompt(
        role="qa",
        phase="test",
        unit={"id": "U1", "title": "t"},
        directives="",
        result_rel="res.json",
        spec_excerpt="",
        completion_level="mvp",
    )
    prod = compose_prompt(
        role="qa",
        phase="test",
        unit={"id": "U1", "title": "t"},
        directives="",
        result_rel="res.json",
        spec_excerpt="",
        completion_level="production",
    )
    assert "MVP: implement" in mvp
    assert "Do NOT build production bundles" in mvp
    assert "Production: implement complete" in prod
    assert "production build/check command once" in prod
