"""Compose a task prompt from role + unit + directives + result path.

Shared protocol (coding conventions, stack, result-file contract) already lives in
the target's CLAUDE.md/AGENTS.md, so here we only compose the task instruction.
This text is injected into every role call, so keep it concise and in English.
"""

from __future__ import annotations

_ROLE_INSTRUCTION = {
    "architecture-engineer": (
        "Read the spec and design the system. Write architecture, API contract, and data-model "
        "docs under docs/design/, and decompose the spec into independently buildable work units "
        "(units)."
    ),
    "testsheet-creator": (
        "Write a spec-based End-to-End test sheet at docs/test/e2e-sheet.md "
        "(scenario, preconditions, steps, expected results)."
    ),
    "frontend-developer": "Implement this unit's frontend (UI/state/routing) under frontend/.",
    "backend-developer": "Implement this unit's backend API/domain logic under backend/.",
    "dba": "Write this unit's DB schema/migrations/indexes under db/.",
    "test-engineer": "Write automated tests for this unit under tests/.",
    "qa": "Run this unit's tests and verify results. Report pass/fail with evidence.",
    "cicd": "Set up the build/test/deploy pipeline (.github/workflows/).",
    "docs-writer": (
        "From the ACTUAL code/design, write the full human-readable deliverable set in EN and KO "
        "(docs/<NAME>.md + <NAME>.ko.md): index, ERD (mermaid erDiagram), "
        "SEQUENCE (mermaid sequenceDiagram), DB_TABLES, API, USER_MANUAL, DEPLOY, RUN_GUIDE, "
        "ARCHITECTURE. Use mermaid diagrams and tables; match real tables/endpoints/commands."
    ),
    "project-manager": (
        "Review the current board and recent events, then write a short, actionable directive "
        "on progress, risk, and priorities."
    ),
    "project-leader": (
        "Review the current board and recent events, then write a short directive on technical "
        "coherence, cross-unit coordination, and code quality."
    ),
}


def compose_prompt(
    *,
    role: str,
    phase: str,
    unit: dict | None,
    directives: str,
    result_rel: str,
    spec_excerpt: str,
    recent_events: str = "",
) -> str:
    directives = directives or ""
    spec_excerpt = spec_excerpt or ""
    recent_events = recent_events or ""
    parts: list[str] = [f"# Role: {role}"]
    parts.append(
        "Follow the shared protocol, coding conventions, and tech stack in this directory's "
        "CLAUDE.md / AGENTS.md."
    )

    if directives.strip():
        parts.append("## PM/PL directives (latest)\n" + directives.strip()[-2000:])

    if unit:
        uid = unit.get("id", "?") if isinstance(unit, dict) else "?"
        title = unit.get("title", "") if isinstance(unit, dict) else ""
        description = unit.get("description", "") if isinstance(unit, dict) else ""
        deps = unit.get("deps", []) if isinstance(unit, dict) else []
        parts.append(
            "## Target work unit\n"
            f"- id: {uid}\n"
            f"- title: {title}\n"
            f"- description: {description}\n"
            f"- deps: {deps}"
        )
    else:
        parts.append("## Scope\nThe entire spec.")
        if spec_excerpt:
            parts.append("## Spec excerpt\n" + spec_excerpt[:1500])

    if recent_events:
        parts.append("## Recent events\n" + recent_events)

    parts.append("## Instruction\n" + _ROLE_INSTRUCTION.get(role, "Perform your role."))

    if role == "qa":
        parts.append(
            "## Constraints (cost & environment)\n"
            "- You MAY run the existing test suite to verify; "
            "install test deps only if missing, once.\n"
            "- Do NOT build production bundles or start long-running servers."
        )
    elif role not in ("project-manager", "project-leader"):
        parts.append(
            "## Constraints (cost & environment)\n"
            "- Do NOT create virtualenvs, install dependencies (pip/npm install), build production "
            "bundles, or start servers — CI handles install/build.\n"
            "- Write and edit source files only; QA runs the tests."
        )

    parts.append(
        "## Completion report (required)\n"
        f"When done, write your result as JSON to `{result_rel}`. Schema:\n"
        "```json\n"
        '{"status": "done", "artifacts": ["relative/path", ...], "notes": ["..."], '
        '"blockers": [], "units": []}\n'
        "```\n"
        "- `status` MUST be one of: `done`, `failed`, `blocked` "
        "(use `failed`/`blocked` when the work could not be completed, and list reasons "
        "in `blockers`).\n"
        "- `artifacts` MUST be project-relative paths (e.g. `backend/app/api.py`). "
        "Do NOT use absolute paths (no leading `/`, no `C:\\`) or `..` parent-traversal; "
        "list only files you actually created or edited.\n"
        "- Architect only: include a `units` array of "
        '[{"id","title","description","deps":[],"roles":[]}]. '
        "Each `id` MUST be a simple slug (letters, digits, `-`/`_`; no spaces, slashes, "
        "or `..`), and `deps` MUST reference such ids."
    )
    return "\n\n".join(parts)
