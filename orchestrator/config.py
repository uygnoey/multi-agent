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

# 사람이 읽을 백엔드 설명 (UI 라벨 · --check 출력 공용)
BACKEND_INFO = {
    "mock": "무비용 더미 (검증용)",
    "claude-cli": "Claude Code CLI · 구독/API키",
    "claude-team": "Claude Code 네이티브 서브에이전트(Team)",
    "claude-sdk": "Claude Agent SDK · API키",
    "openai-agents": "OpenAI Agents SDK · API키",
    "codex": "OpenAI Codex CLI · 구독/API키",
}

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
    # 우선순위 풀: 앞에서부터 가용한 백엔드를 쓰고, 실패 시 다음 백엔드로 폴오버.
    backend_priority: list[str] = field(default_factory=list)
    # 역할별 우선순위 override (role -> [backend, ...]).
    role_priority: dict[str, list[str]] = field(default_factory=dict)
    # True 면 역할들을 우선순위 목록에 라운드로빈으로 분산(모든 백엔드 동시 가동).
    distribute: bool = False

    def backends_for(self, role: str) -> list[str]:
        """역할에 대한 백엔드 후보를 우선순위 순서로 반환 (폴오버용)."""
        if self.mock:
            return ["mock"]
        if self.role_priority.get(role):
            return list(self.role_priority[role])
        if role in self.role_backend:
            return [self.role_backend[role]]
        base = list(self.backend_priority) if self.backend_priority else [self.default_backend]
        if self.distribute and len(base) > 1:
            try:
                idx = list(ROLES).index(role) % len(base)
            except ValueError:
                idx = 0
            base = base[idx:] + base[:idx]
        return base

    def backend_for(self, role: str) -> str:
        return self.backends_for(role)[0]

    def model_for(self, backend: str) -> str | None:
        # 명시 모델만 전달. 미지정 시 각 백엔드 기본값을 쓰도록 None.
        return self.model
