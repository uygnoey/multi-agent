"""경로/기본값, 역할→(페이즈, 툴셋) 매핑, 실행 설정."""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


def _coerce_int(raw, default: int) -> int:
    """정수로 변환하되 비-정수/None/이상값은 default 로 안전화 (raw ValueError 차단; #37)."""
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _coerce_optional_positive_int(raw) -> int | None:
    """None/0/음수는 무제한, malformed 값은 안전하게 1개로 제한."""
    if raw is None:
        return None
    try:
        if isinstance(raw, bool):
            return 1
        if isinstance(raw, int):
            iv = raw
        elif isinstance(raw, float):
            if not math.isfinite(raw) or not raw.is_integer():
                return 1
            iv = int(raw)
        elif isinstance(raw, str):
            s = raw.strip()
            if not re.fullmatch(r"[+-]?\d+", s):
                return 1
            iv = int(s)
        else:
            return 1
    except Exception:
        return 1
    return iv if iv > 0 else None


# 프레임워크 저장소 루트 (이 파일 = orchestrator/config.py)
# 후방 호환을 위해 그대로 유지: 편집/소스/Docker 설치에서는 이게 곧 저장소 루트다.
FRAMEWORK_ROOT = Path(__file__).resolve().parent.parent

# 휠(wheel) 설치 시 .claude/agents 와 templates 는 data-files 로
# <prefix>/share/dev-crew-orchestrator/ 아래에 깔린다(#5). 저장소 루트만 보던
# 기존 로더는 휠 설치에서 이를 못 찾아 역할 로딩/스캐폴딩이 깨졌다. 그래서
# "저장소 루트 우선 → 설치 data 위치 폴백" 순서로 첫 존재 디렉터리를 고르는
# 리졸버를 둔다. 어디서도 못 찾으면 저장소 루트 경로를 기본값으로 돌려주어
# 소스 실행 시 동작/테스트가 그대로 유지되게 한다.

# data-files 가 깔리는 상대 경로(설치 prefix 기준). pyproject 의
# [tool.setuptools.data-files] 키와 1:1 로 맞춘다.
_DATA_AGENTS_REL = Path("share") / "dev-crew-orchestrator" / ".claude" / "agents"
_DATA_TEMPLATES_REL = Path("share") / "dev-crew-orchestrator" / "templates"


def _site_packages_roots() -> list[Path]:
    """현재 import 된 orchestrator 패키지의 상위 디렉터리(들) 후보 (<site-packages> 류)."""
    roots: list[Path] = []
    pkg_root = Path(__file__).resolve().parent.parent  # orchestrator 의 부모
    roots.append(pkg_root)
    # importlib 로 패키지가 실제 깔린 위치도 후보에 추가(편집/네임스페이스 대응).
    try:
        import importlib.util

        spec = importlib.util.find_spec("orchestrator")
        if spec is not None and spec.origin:
            roots.append(Path(spec.origin).resolve().parent.parent)
    except Exception:
        pass
    return roots


def _resolve_runtime_dir(repo_rel: Path, data_rel: Path) -> Path:
    """런타임 데이터 디렉터리를 후보 순서대로 찾아 첫 존재 경로를 반환.

    우선순위:
      1) 저장소 루트(FRAMEWORK_ROOT/repo_rel) — 편집/소스/Docker 설치
      2) sys.prefix/data_rel — 일반 휠 설치(data-files)
      3) <site-packages 류>/data_rel — importlib 기반 후보 포함

    하나도 없으면 저장소 루트 경로를 기본값으로 반환 → 소스 실행 시 기존 동작/테스트 유지.
    """
    repo_candidate = FRAMEWORK_ROOT / repo_rel
    candidates: list[Path] = [repo_candidate]
    # sys.prefix / venv 루트(있으면) 아래의 data-files 위치.
    for prefix in (sys.prefix, getattr(sys, "base_prefix", sys.prefix)):
        candidates.append(Path(prefix) / data_rel)
    # <site-packages> 류 루트 아래의 data-files 위치.
    for root in _site_packages_roots():
        candidates.append(root / data_rel)

    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        if cand.is_dir():
            return cand
    # 폴백: 저장소 루트 경로(존재하지 않더라도) — 후방 호환.
    return repo_candidate


AGENTS_DIR = _resolve_runtime_dir(Path(".claude") / "agents", _DATA_AGENTS_REL)
TEMPLATES_DIR = _resolve_runtime_dir(Path("templates"), _DATA_TEMPLATES_REL)

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


# 11개 역할 (.claude/agents/*.md 의 name 과 1:1)
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
    full_access: bool = False  # machine-wide backend access; default is project workspace access
    auto_commit: bool = True  # create checkpoint commits inside the generated project
    max_attempts: int = 0  # dev→test→qa repair attempts per unit; 0 = until fixed/external stop
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
    # 백엔드 정규화(__post_init__) 중 발생한 경고(알 수 없는 이름 드롭/폴백). 보존만, raise 안 함.
    backend_warnings: list[str] = field(default_factory=list)

    def __post_init__(self):
        # 숫자 옵션을 안전 범위로 정규화 (CLI/웹 어디서 와도 crash/hang/오작동 방지; 웹과 동작 일치)
        # 라이브러리 호출부가 잘못된 값을 줘도 raw ValueError 가 아니라 기본값으로 안전화 (#37)
        #
        # 하한뿐 아니라 상한(upper clamp)도 둔다: 프로그래매틱 호출부가 병적으로 큰 값을 주면
        # OOM / 무한정 작업 스폰 / 사실상 hang 으로 이어질 수 있으므로 합리적 상한으로 클램프한다.
        #   - concurrency: 동시 역할 세션 수. 64 면 어떤 머신에서도 과한 상한 → OOM/스레드폭증 방지.
        #   - max_attempts: unit별 dev→test→qa 재작업 횟수. 0 이면 제품 완주 모드(고쳐질 때까지).
        #   - retries: 역할 호출 전이성 실패 재시도 횟수. 20 회 상한으로 폭주 방지.
        # (정상값 concurrency=3 / max_attempts=0 / retries=1 등은 그대로 보존된다.)
        self.concurrency = min(64, max(1, _coerce_int(self.concurrency, 3)))
        self.max_attempts = min(20, max(0, _coerce_int(self.max_attempts, 0)))
        self.retries = min(20, max(0, _coerce_int(self.retries, 1)))
        if self.max_units is not None:
            self.max_units = _coerce_optional_positive_int(self.max_units)
        # poll_interval 을 안전 하한으로 클램프 (#33). 웹은 poll_interval=0 을 허용하는데,
        # 0/음수/비-숫자/이상값이 그대로 _supervise 의 asyncio.wait_for 로 들어가면 PM/PL 감독이
        # tight busy-loop 를 돌며 CPU 를 태우고 비싼 LLM 호출을 반복한다. 0/음수는 안전 바닥(5초)
        # 으로, 그 외 유효값은 최소 1초로 둔다(정상 큰 값은 그대로 유지).
        # (#9) NaN/Inf 방어: Inf 가 그대로 asyncio.wait_for(timeout=) 로 가면 감독 폴링이
        # 사실상 멈추므로(영원히 대기) 비유한값은 기본 20초로 되돌린다.
        try:
            if isinstance(self.poll_interval, bool):
                raise TypeError
            pi = float(self.poll_interval)
        except (TypeError, ValueError):
            pi = 20.0
        if not math.isfinite(pi):
            pi = 20.0
        self.poll_interval = pi if pi > 0 else 5.0
        self.poll_interval = max(1.0, self.poll_interval)
        # 유한하지만 병적으로 큰 값(예: 1e308)은 감독 폴링을 사실상 무력화한다(다음 폴까지 수년).
        # 1시간(3600초) 상한으로 클램프해 PM/PL 감독이 항상 합리적 주기로 동작하게 한다.
        self.poll_interval = min(3600.0, self.poll_interval)
        # (#8) budget: NaN/Inf 는 비교를 무력화한다(예: committed >= nan 은 항상 False 라 예산
        # enforcement 가 조용히 꺼진다). 비유한/비-숫자 예산은 None(예산 없음)으로 정규화해
        # "깨진 예산 = 예산 없음" 을 명시적으로 만든다. 웹 검증과 더불어 방어적 2중 가드.
        if self.budget is not None:
            try:
                if isinstance(self.budget, bool):
                    raise TypeError
                bv = float(self.budget)
            except (TypeError, ValueError):
                bv = None
            else:
                if not math.isfinite(bv):
                    bv = None
            self.budget = bv
        # (#9) session_timeout: NaN/Inf/0/음수는 asyncio.wait_for 를 오작동시키거나 의미가 없다.
        # 비유한/비-숫자/≤0 은 None(역할 호출 무제한)으로 정규화한다(CLI 의 0=무제한 정책과 일치).
        tv = None
        if self.session_timeout is not None:
            try:
                if isinstance(self.session_timeout, bool):
                    raise TypeError
                tv = float(self.session_timeout)
            except (TypeError, ValueError):
                tv = None
            else:
                if not math.isfinite(tv) or tv <= 0:
                    tv = None
        self.session_timeout = tv
        # retry_backoff: 지수 백오프 기준 초(sec). 비유한(NaN/Inf)/비-숫자/음수는 기본 2.0 으로.
        # 러너는 min(retry_backoff * 2**i, 60.0) 으로 캡을 두지만, 기준값 자체도 60초 상한으로
        # 클램프해 (예: 1e9 같은 병적인 큰 값이) 첫 재시도부터 비정상 대기를 만들지 않게 한다.
        try:
            if isinstance(self.retry_backoff, bool):
                raise TypeError
            rb = float(self.retry_backoff)
        except (TypeError, ValueError):
            rb = 2.0
        if not math.isfinite(rb) or rb < 0:
            rb = 2.0
        self.retry_backoff = min(60.0, rb)
        # 백엔드 이름 검증/정규화: 프로그래매틱 RunConfig 구성에서도 CLI 와 동일하게 별칭을
        # 정식 이름으로 풀고, VALID_BACKENDS 에 없는 알 수 없는 이름은 안전화한다. RunConfig 구성은
        # 절대 raise 하지 않으므로(웹/라이브러리 호출부가 죽지 않게) 예외 대신 sanitize 한다.
        #   - 우선순위 리스트(backend_priority / role_priority 값): 알 수 없는 이름 드롭, 경고 보존.
        #   - default_backend / role_backend 값: 알 수 없으면 DEFAULT_BACKEND 로 폴백.
        # mock 및 별칭(claude-code, openai-sdk 등)은 resolve 를 거쳐 그대로 유효하게 유지된다.
        self._sanitize_backends()

    def _sanitize_backends(self) -> None:
        """백엔드 이름 별칭 해소 + 알 수 없는 이름 경고 (raise/드롭/치환 없음).

        알려진 이름은 정식명으로 정규화(별칭 해소)하고, 알 수 없는 이름은 *그대로 두되* 경고만
        남긴다. 드롭/치환하면 (a) fake 백엔드를 주입하는 테스트나 (b) 런타임에 monkeypatch 된
        레지스트리가 깨진다. 알 수 없는 이름은 어차피 runner._candidates/available() 가 안전하게
        skip/실패로 처리하므로, config 는 조기 '경고'만 제공하고 실제 동작은 바꾸지 않는다.
        """
        # backends 패키지는 config 를 import 하므로 최상위 import 는 순환 → 지연 import.
        try:
            from .backends import resolve as _resolve
        except Exception:  # noqa: BLE001

            def _resolve(name: str) -> str:
                return name

        warnings: list[str] = []

        def norm(name):
            """알려진 이름은 정식명으로 정규화, 알 수 없으면 원본 유지 + 경고."""
            try:
                resolved = _resolve(str(name).strip())
            except Exception:  # noqa: BLE001
                resolved = str(name).strip()
            if resolved not in VALID_BACKENDS:
                warnings.append(f"알 수 없는 백엔드(런타임에서 skip/실패로 처리): {name!r}")
            return resolved

        self.default_backend = norm(self.default_backend)
        self.backend_priority = [norm(n) for n in (self.backend_priority or [])]
        self.role_priority = {
            role: [norm(n) for n in names] for role, names in (self.role_priority or {}).items()
        }
        self.role_backend = {role: norm(n) for role, n in (self.role_backend or {}).items()}
        # 정규화 과정에서 발생한 경고를 보존(웹/CLI 에서 노출 가능). raise 하지 않는다.
        self.backend_warnings = warnings

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
