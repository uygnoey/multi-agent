"""경로/기본값, 역할→(페이즈, 툴셋) 매핑, 실행 설정."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


def _coerce_int(raw, default: int) -> int:
    """정수로 변환하되 비-정수/None/이상값은 default 로 안전화 (raw ValueError 차단; #37)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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
PHASE_DOCS = "docs"

DEV_TOOLS = ("Read", "Write", "Edit", "Bash")
# 감독(PM/PL)은 코드/환경을 건드리지 않는 읽기 전용 역할 → Bash 제외, Read 만 허용 (#49/#50)
RO_TOOLS = ("Read",)


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
    "docs-writer": RoleSpec("docs-writer", PHASE_DOCS, DEV_TOOLS),
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
    "database-admin": "dba",
    "db-admin": "dba",
    "dba-engineer": "dba",
    "devops": "cicd",
    "devops-engineer": "cicd",
    "docs": "docs-writer",
    "doc": "docs-writer",
    "documentation": "docs-writer",
    "writer": "docs-writer",
    "tech-writer": "docs-writer",
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
    # 공백/언더스코어/슬래시 등 구분자를 하이픈으로 통일 → "backend developer"/"backend_developer"/
    # "front end" 같은 흔한 변형도 매칭. (예: "front end" → "front-end" 별칭 → frontend-developer)
    n = str(name).strip().lower()
    n = re.sub(r"[\s_/]+", "-", n)
    n = re.sub(r"-{2,}", "-", n).strip("-")
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
    PHASE_DOCS: 20,
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
    # True 면 미핀 역할을 풀에 번갈아(교차) 배정 → 두 모델이 섞여 상호 검증. distribute 보다 우선.
    cross_check: bool = False

    def __post_init__(self):
        # 숫자 옵션을 안전 범위로 정규화 (CLI/웹 어디서 와도 crash/hang/오작동 방지; 웹과 동작 일치)
        # 라이브러리 호출부가 잘못된 값을 줘도 raw ValueError 가 아니라 기본값으로 안전화 (#37)
        self.concurrency = max(1, _coerce_int(self.concurrency, 3))
        self.max_attempts = max(1, _coerce_int(self.max_attempts, 2))
        self.retries = max(0, _coerce_int(self.retries, 1))
        if self.max_units is not None:
            mu = _coerce_int(self.max_units, 0)
            self.max_units = mu if mu > 0 else None  # 0/음수/이상값 → 제한 없음

    def backends_for(self, role: str) -> list[str]:
        """역할에 대한 백엔드 후보를 우선순위 순서로 반환 (폴오버용)."""
        if self.mock:
            return ["mock"]
        if self.role_priority.get(role):
            return list(self.role_priority[role])
        if role in self.role_backend:
            return [self.role_backend[role]]
        base = list(self.backend_priority) if self.backend_priority else [self.default_backend]
        if self.cross_check:
            # 풀을 역할별 선택값까지 합쳐 추론 (예: 기본 claude + QA=codex → {claude, codex})
            pool = self._cross_pool(base)
            if len(pool) >= 2:
                # 핀(role_priority/role_backend)은 위에서 early-return. 여기 오는 역할은
                # 미핀이므로 미핀 역할들을 ROLES 순서로 번갈아(교차) 배정한다. (그룹 하드코딩 없음)
                # role_backend 핀도 핀으로 간주해 미핀 목록에서 제외 → 오프셋 왜곡 방지 (#138)
                unpinned = [r for r in ROLES if not self._is_pinned(r)]
                idx = unpinned.index(role) if role in unpinned else 0
                primary = pool[idx % len(pool)]
                return [primary, *[b for b in pool if b != primary]]
        if self.distribute and len(base) > 1:
            try:
                idx = list(ROLES).index(role) % len(base)
            except ValueError:
                idx = 0
            base = base[idx:] + base[:idx]
        return base

    def _is_pinned(self, role: str) -> bool:
        """역할이 명시적으로 핀되었는지 여부 (role_priority 또는 레거시 role_backend; #138)."""
        return bool(self.role_priority.get(role)) or role in self.role_backend

    def _cross_pool(self, base: list[str]) -> list[str]:
        """교차 배치용 백엔드 풀 = base + 역할별 명시 선택값(중복 제거, 순서 유지).

        단일 백엔드 + 일부 역할만 지정한 경우에도 교차가 성립하도록 풀을 넓힌다.
        role_priority 뿐 아니라 레거시 role_backend(역할별 단일 핀)도 풀에 포함해
        프로그래매틱 호출부(role_backend)와 CLI/웹(role_priority)의 교차 분포를 일치시킨다 (#137).
        """
        pool = list(base)
        for picks in self.role_priority.values():
            for p in picks:
                if p not in pool:
                    pool.append(p)
        for backend in self.role_backend.values():
            if backend not in pool:
                pool.append(backend)
        return pool

    def backend_for(self, role: str) -> str:
        return self.backends_for(role)[0]

    def model_for(self, backend: str) -> str | None:
        # 명시 모델만 전달. 미지정 시 각 백엔드 기본값을 쓰도록 None.
        return self.model
