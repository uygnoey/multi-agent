"""Compose a task prompt from role + unit + directives + result path.

Shared protocol (coding conventions, stack, result-file contract) already lives in
the target's CLAUDE.md/AGENTS.md, so here we only compose the task instruction.
This text is injected into every role call, so keep it concise and in English.
"""

from __future__ import annotations

from .config import normalize_completion_level

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

_MAX_UNIT_TITLE_CHARS = 200
_MAX_UNIT_DESCRIPTION_CHARS = 1500
_MAX_UNIT_DEPS_CHARS = 500
_MAX_REPAIR_CONTEXT_CHARS = 2500


def _clip(value, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…(truncated)"


def compose_prompt(
    *,
    role: str,
    phase: str,
    unit: dict | None,
    directives: str,
    result_rel: str,
    spec_excerpt: str,
    recent_events: str = "",
    completion_level: str = "mvp",
) -> str:
    directives = directives or ""
    spec_excerpt = spec_excerpt or ""
    recent_events = recent_events or ""
    parts: list[str] = [f"# Role: {role}"]
    parts.append(
        "Follow the shared protocol, coding conventions, and tech stack in this directory's "
        "CLAUDE.md / AGENTS.md."
    )
    # (#audit9-10) 완료 수준 정규화는 config.normalize_completion_level 단일 소스를 재사용한다.
    level = normalize_completion_level(completion_level)
    if level == "production":
        parts.append(
            "## Completion target\n"
            "Production: implement complete, integrated behavior for the requested scope. "
            "Do not mark work done just because a narrow happy path passes; include error states, "
            "persistence/integration details, configuration, and declared build/test verification "
            "where feasible."
        )
    else:
        parts.append(
            "## Completion target\n"
            "MVP: implement the requested user-visible behavior end to end with a runnable, "
            "coherent slice. Avoid unrelated hardening, but do not bypass missing core behavior."
        )

    if directives.strip():
        parts.append("## PM/PL directives (latest)\n" + directives.strip()[-2000:])

    # (#audit9-9) dict 인 unit 만 "Target work unit" 으로 처리한다. 예전엔 `if unit:` 가
    # truthy non-dict(list/str)도 받아들여 uid="?"/title="" 같은 빈 블록을 내보냈다.
    # dict 가 아니면 unit 이 없는 것으로 보고 "entire spec" 경로로 떨어진다.
    if unit and isinstance(unit, dict):
        uid = unit.get("id", "?")
        title = _clip(unit.get("title", ""), _MAX_UNIT_TITLE_CHARS)
        description = _clip(unit.get("description", ""), _MAX_UNIT_DESCRIPTION_CHARS)
        deps = _clip(unit.get("deps", []), _MAX_UNIT_DEPS_CHARS)
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
        # directives([-2000:])·spec_excerpt([:1500]) 와 마찬가지로 길이 상한을 둔다.
        # 이벤트 로그는 무한정 커질 수 있어 캡이 없으면 프롬프트가 비대해진다.
        # 최근 ~2000자만 싣는다.
        parts.append("## Recent events\n" + recent_events[-2000:])

    repair_context = unit.get("repair_context") if isinstance(unit, dict) else None
    if repair_context:
        parts.append(
            "## Previous verification failure to fix\n"
            + _clip(repair_context, _MAX_REPAIR_CONTEXT_CHARS)
            + "\n\n"
            "Use this QA/test feedback as the primary repair target. If it says the failure is "
            "in the test harness or configuration, fix the test/config instead of changing "
            "working production code just to satisfy a broken test."
        )

    parts.append("## Instruction\n" + _ROLE_INSTRUCTION.get(role, "Perform your role."))

    if role == "qa":
        build_rule = (
            "- For production completion, run the declared production build/check command once "
            "when feasible; do not start long-running servers.\n"
            if level == "production"
            else "- Do NOT build production bundles or start long-running servers.\n"
        )
        parts.append(
            "## Verification discipline\n"
            "- You MAY run the existing test suite to verify; "
            "install test deps only if missing, once.\n"
            f"{build_rule}"
            "- Avoid repeating successful checks unless source, tests, or configuration changed.\n"
            "- When failing, classify the root cause in the result JSON using "
            "`failure_kind` (`source_bug`, `test_harness`, `test_config`, `dependency_env`, "
            "or `unknown`), `repair_owner` (role that should fix it), and "
            "`repair_instruction` (concrete next edit)."
        )
    elif role == "test-engineer":
        parts.append(
            "## Test repair guidance\n"
            "- If this is a repair pass, first fix broken tests/test configuration reported by QA "
            "before adding new tests.\n"
            "- Tests must be runnable from the project commands they declare. Avoid brittle timing "
            "assertions and framework forward-reference patterns that fail independently of source "
            "behavior.\n"
            "- Run the relevant tests when feasible and report the exact command/result."
        )
    elif role not in ("project-manager", "project-leader"):
        build_rule = (
            "- Production completion may require build/deploy/config changes; keep commands "
            "bounded and leave long-running verification to QA.\n"
            if level == "production"
            else "- Do NOT create virtualenvs, install dependencies (pip/npm install), build "
            "production bundles, or start servers — QA handles verification.\n"
        )
        parts.append(
            "## Development discipline\n"
            f"{build_rule}"
            "- Prefer targeted edits and narrow validation over broad, repeated checks.\n"
            "- Write and edit source files only; QA runs the tests."
        )

    # 감독자(project-manager/project-leader)는 읽기전용(RO_TOOLS, Write 없음)이고 러너도 이들의
    # 결과 파일을 요구하지 않는다(출력 자체를 보드 지시사항으로 캡처). 따라서 결과 JSON 작성을
    # 지시하면 모순이 되므로, 감독자에게는 파일을 쓰지 말고 지침/지시사항을 산문으로
    # 응답하라고 안내한다.
    if role in ("project-manager", "project-leader"):
        parts.append(
            "## Output\n"
            "You are a read-only supervisor: do NOT write any files (no result JSON, no "
            f"`{result_rel}`). Respond directly with your guidance/directive as prose — "
            "it is captured as a board directive for the team."
        )
    else:
        parts.append(
            "## Completion report (required)\n"
            f"When done, write your result as JSON to `{result_rel}`. Schema:\n"
            "```json\n"
            '{"status": "done", "artifacts": ["relative/path", ...], "notes": ["..."], '
            '"blockers": [], "units": [], '
            '"failure_kind": null, "repair_owner": null, "repair_instruction": null}\n'
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
