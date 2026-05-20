"""경로/기본값, 역할→(페이즈, 툴셋) 매핑, 실행 설정."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 프레임워크 저장소 루트 (이 파일 = orchestrator/config.py)
FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent
AGENTS_DIR = FRAMEWORK_ROOT / ".claude" / "agents"
TEMPLATES_DIR = FRAMEWORK_ROOT / "templates"

# 페이즈
PHASE_SUPERVISOR = "supervisor"
PHASE_DESIGN = "design"
PHASE_DEV = "dev"
PHASE_TEST = "test"
PHASE_CICD = "cicd"

DEV_TOOLS = ("Read", "Write", "Edit", "Bash")
RO_TOOLS = ("Read", "Bash")


@dataclass(frozen=True)
class RoleSpec:
    name: str
    phase: str
    tools: tuple[str, ...]


# 10개 역할 (.claude/agents/*.md 의 name 과 1:1)
ROLES: dict[str, RoleSpec] = {
    "project-manager": RoleSpec("project-manager", PHASE_SUPERVISOR, RO_TOOLS),
    "project-leader": RoleSpec("project-leader", PHASE_SUPERVISOR, RO_TOOLS),
    "architecture-engineer": RoleSpec("architecture-engineer", PHASE_DESIGN, DEV_TOOLS),
    "testsheet-creator": RoleSpec("testsheet-creator", PHASE_DESIGN, DEV_TOOLS),
    "frontend-developer": RoleSpec("frontend-developer", PHASE_DEV, DEV_TOOLS),
    "backend-developer": RoleSpec("backend-developer", PHASE_DEV, DEV_TOOLS),
    "dba": RoleSpec("dba", PHASE_DEV, DEV_TOOLS),
    "test-engineer": RoleSpec("test-engineer", PHASE_TEST, DEV_TOOLS),
    "qa": RoleSpec("qa", PHASE_TEST, DEV_TOOLS),
    "cicd": RoleSpec("cicd", PHASE_CICD, DEV_TOOLS),
}

SUPERVISOR_ROLES = ["project-manager", "project-leader"]
# DESIGN_ROLES[0] 는 반드시 아키텍트 (스케줄러가 units 를 results[0] 에서 읽음)
DESIGN_ROLES = ["architecture-engineer", "testsheet-creator"]
DEV_ROLES = ["frontend-developer", "backend-developer", "dba"]

# 페이즈별 세션 최대 턴 (안전장치)
MAX_TURNS = {
    PHASE_SUPERVISOR: 8,
    PHASE_DESIGN: 30,
    PHASE_DEV: 40,
    PHASE_TEST: 25,
    PHASE_CICD: 20,
}

DEFAULT_BACKEND = "mock"
VALID_BACKENDS = ("claude-sdk", "claude-cli", "claude-team", "openai-agents", "codex", "mock")

# Backends able to dispatch native Claude Code subagents via the Task tool.
DELEGATION_CAPABLE = ("claude-sdk", "claude-cli", "claude-team")
DELEGATION_TOOL = "Task"

# Which teammates a role may delegate to when --delegate is on (depth-1 only).
DELEGATES: dict[str, tuple[str, ...]] = {
    "backend-developer": ("dba",),
    "frontend-developer": ("backend-developer",),
    "architecture-engineer": ("dba",),
    "test-engineer": ("qa",),
}


@dataclass
class RunConfig:
    spec_path: Path
    project_dir: Path
    default_backend: str = DEFAULT_BACKEND
    role_backend: dict[str, str] = field(default_factory=dict)
    max_units: int | None = None
    concurrency: int = 3
    budget: float | None = None
    model: str | None = None
    poll_interval: float = 20.0
    mock: bool = False
    delegate: bool = False  # allow role sessions to call teammates as subagents
    max_attempts: int = 2  # dev→test→qa rework attempts per unit
    retries: int = 1  # transient backend-failure retries per role call
    retry_backoff: float = 2.0  # seconds, exponential

    def backend_for(self, role: str) -> str:
        if self.mock:
            return "mock"
        return self.role_backend.get(role, self.default_backend)

    def model_for(self, backend: str) -> str | None:
        # 명시 모델만 전달. 미지정 시 각 백엔드 기본값을 쓰도록 None.
        return self.model
