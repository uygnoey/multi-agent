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

# 아키텍트가 unit.roles 에 단축명/변형을 써도 정식 역할명으로 매핑 (백엔드 무관 견고성).
ROLE_NAME_ALIASES = {
    "architect": "architecture-engineer",
    "architecture": "architecture-engineer",
    "frontend": "frontend-developer",
    "front-end": "frontend-developer",
    "fe": "frontend-developer",
    "frontend-dev": "frontend-developer",
    "backend": "backend-developer",
    "back-end": "backend-developer",
    "be": "backend-developer",
    "backend-dev": "backend-developer",
    "db": "dba",
    "database": "dba",
    "testsheet": "testsheet-creator",
    "test-sheet": "testsheet-creator",
    "test": "test-engineer",
    "tester": "test-engineer",
    "testing": "test-engineer",
    "quality-assurance": "qa",
    "pm": "project-manager",
    "manager": "project-manager",
    "pl": "project-leader",
    "lead": "project-leader",
    "tech-lead": "project-leader",
    "ci": "cicd",
    "cd": "cicd",
    "ci-cd": "cicd",
    "ci/cd": "cicd",
}


def normalize_role(name: str) -> str:
    n = str(name).strip().lower()
    if n in ROLES:
        return n
    return ROLE_NAME_ALIASES.get(n, n)


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

# 공식 명칭 기반 백엔드 설명 (UI 라벨 · --check 출력 공용)
BACKEND_INFO = {
    "mock": "Mock — 무비용 더미 (검증 전용)",
    "claude-cli": "Claude Code — Anthropic 공식 CLI (구독 또는 API키)",
    "claude-team": "Claude Code Subagents — 네이티브 서브에이전트 디스패치",
    "claude-sdk": "Claude Agent SDK — Anthropic 공식 Python SDK (API키)",
    "openai-agents": "OpenAI Agents SDK — OpenAI 공식 Python SDK (API키)",
    "codex": "OpenAI Codex CLI — OpenAI 공식 CLI (ChatGPT 구독 또는 API키)",
}

# Backends able to dispatch native Claude Code subagents via the Task tool.
DELEGATION_CAPABLE = ("claude-sdk", "claude-cli", "claude-team")
DELEGATION_TOOL = "Task"

# 교차 검증(--cross-check): 생산자(build)와 검증자(verify)를 서로 다른 프로바이더에 배치.
# 풀 [P0, P1] 기준 build→P0, verify→P1 (실패 시 상대 프로바이더로 폴오버).
# → 개발(build)을 한 프로바이더가, 그 결과 검증(verify)을 다른 프로바이더가 맡아 교차 검증.
CROSS_GROUPS: dict[str, str] = {
    "architecture-engineer": "build",
    "frontend-developer": "build",
    "backend-developer": "build",
    "dba": "build",
    "project-manager": "build",
    "testsheet-creator": "verify",
    "test-engineer": "verify",
    "qa": "verify",
    "project-leader": "verify",  # PL은 검토자 → PM과 반대 프로바이더
    "cicd": "verify",
}

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
    session_timeout: float | None = 1200.0  # 역할 호출 1회 최대 시간(초); None=무제한
    # 우선순위 풀: 앞에서부터 가용한 백엔드를 쓰고, 실패 시 다음 백엔드로 폴오버.
    backend_priority: list[str] = field(default_factory=list)
    # 역할별 우선순위 override (role -> [backend, ...]).
    role_priority: dict[str, list[str]] = field(default_factory=dict)
    # True 면 역할들을 우선순위 목록에 라운드로빈으로 분산(모든 백엔드 동시 가동).
    distribute: bool = False
    # True 면 생산자/검증자를 서로 다른 프로바이더에 배치(교차 검증). distribute 보다 우선.
    cross_check: bool = False

    def backends_for(self, role: str) -> list[str]:
        """역할에 대한 백엔드 후보를 우선순위 순서로 반환 (폴오버용)."""
        if self.mock:
            return ["mock"]
        if self.role_priority.get(role):
            return list(self.role_priority[role])
        if role in self.role_backend:
            return [self.role_backend[role]]
        base = list(self.backend_priority) if self.backend_priority else [self.default_backend]
        if self.cross_check and len(base) >= 2:
            build_side, verify_side = self._cross_sides(base)
            primary = build_side if CROSS_GROUPS.get(role, "build") == "build" else verify_side
            return [primary, *[b for b in base if b != primary]]
        if self.distribute and len(base) > 1:
            try:
                idx = list(ROLES).index(role) % len(base)
            except ValueError:
                idx = 0
            base = base[idx:] + base[:idx]
        return base

    def _cross_sides(self, base: list[str]) -> tuple[str, str]:
        """교차 배치의 build/verify 측 프로바이더를 결정.

        유저의 명시적 역할 선택(role_priority)을 시드로 삼는다. 예: PM(build 그룹)을
        claude 로 골랐으면 build=claude, verify=다른 프로바이더. 아무 선택도 없으면
        기본값 build=base[0], verify=base[1].
        """
        build_side = verify_side = None
        for r, picks in self.role_priority.items():
            if not picks or picks[0] not in base:
                continue
            grp = CROSS_GROUPS.get(r, "build")
            if grp == "build" and build_side is None:
                build_side = picks[0]
            elif grp == "verify" and verify_side is None:
                verify_side = picks[0]
        if build_side and not verify_side:
            verify_side = next((b for b in base if b != build_side), base[1])
        if verify_side and not build_side:
            build_side = next((b for b in base if b != verify_side), base[0])
        if not build_side and not verify_side:
            build_side, verify_side = base[0], base[1]
        return build_side, verify_side

    def backend_for(self, role: str) -> str:
        return self.backends_for(role)[0]

    def model_for(self, backend: str) -> str | None:
        # 명시 모델만 전달. 미지정 시 각 백엔드 기본값을 쓰도록 None.
        return self.model
