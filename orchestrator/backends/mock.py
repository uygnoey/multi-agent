"""무비용 mock 백엔드.

LLM 호출 없이 역할별로 결정적 산출물 + 결과 JSON 을 직접 생성한다.
오케스트레이션 배선(스캐폴딩→설계→동시개발→테스트 트리거→CI/CD)을
API 키 없이 end-to-end 로 검증하는 용도.
"""

from __future__ import annotations

import json
import re

from .base import Backend, RoleRequest, RoleResult


def _ident(raw: str, *, prefix: str = "u") -> str:
    """unit id 를 유효한 코드 식별자(JS 함수명/Python 함수명/SQL 테이블명)로 변환.

    #126/#127/#128: 비단어 문자는 '_' 로 치환하고, 숫자로 시작하면 prefix 를 붙여
    JS/Python/SQL 어디서나 유효한 식별자가 되게 한다. 빈 값은 prefix 로 대체.
    """
    s = re.sub(r"\W", "_", str(raw))
    if not s:
        s = prefix
    if s[0].isdigit():
        s = f"{prefix}_{s}"
    return s


def _safe_id_text(raw: str) -> str:
    """#12(audit9): id 를 '텍스트로' 안전하게 임베드한다(식별자 아님 — 가독성 유지).

    개행/제어문자/따옴표가 들어있는 id 를 주석·마크다운 리스트·파이썬 리터럴에 그대로 박으면
    생성된 mock 산출물이 깨진다(라인 분리, 따옴표 escape 깨짐). 개행·제어문자는 공백으로,
    따옴표는 제거하고 양끝 공백을 정리해 한 줄 텍스트로 만든다. 빈 값은 'unit'.
    """
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(raw))  # 개행 포함 제어문자 → 공백
    s = s.replace('"', "").replace("'", "")  # 따옴표 제거(리터럴/마크다운 깨짐 방지)
    s = re.sub(r"\s+", " ", s).strip()
    return s or "unit"


class MockBackend(Backend):
    name = "mock"

    def available(self) -> tuple[bool, str]:
        return True, "always available (no cost)"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        cwd = req.cwd
        role = req.role
        unit = req.unit
        artifacts: list[str] = []
        units: list[dict] = []
        status = "done"

        def write(rel: str, content: str) -> None:
            p = cwd / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            artifacts.append(rel)

        if role in ("project-manager", "project-leader"):
            # supervisor: 디렉티브만 (결과파일 없음)
            return RoleResult(
                ok=True, final_message=f"[{role}] mock review: on track.", cost_usd=0.0
            )

        if role == "architecture-engineer":
            units = _derive_units(req.spec_text)
            write(
                "docs/design/architecture.md",
                "# Architecture (mock)\n\n"
                "Stack: FastAPI + React/Vite + SQLite\n\n## Units\n"
                # #12(audit9): id/title 에 개행/따옴표가 있어도 마크다운 리스트가 깨지지 않게 sanitize.
                + "\n".join(
                    f"- {_safe_id_text(u['id'])}: {_safe_id_text(u.get('title', ''))}"
                    for u in units
                ),
            )
            status = "designed"
        elif role == "testsheet-creator":
            write("docs/test/e2e-sheet.md", _mock_e2e(req.spec_text))
        elif role == "frontend-developer" and unit:
            # #126: JSX 컴포넌트명은 PascalCase 식별자여야 한다 → 식별자 sanitize.
            comp = _ident(unit["id"], prefix="Comp")
            write(
                f"frontend/src/{_ident(unit['id'])}.jsx",
                # #12(audit9): 주석에 박히는 id/title 의 개행/따옴표로 JS 가 깨지지 않게 sanitize.
                f"// mock frontend for {_safe_id_text(unit['id'])}: "
                f"{_safe_id_text(unit.get('title', ''))}\n"
                f"export default function {comp}() {{ return null; }}\n",
            )
            status = "dev_done"
        elif role == "backend-developer" and unit:
            # #12(audit9): 주석·파이썬 문자열 리터럴에 박히는 id 는 개행/따옴표가 들어가면
            # 생성 코드가 깨지므로 텍스트 sanitize 한다(파일명은 _ident 로 식별자화).
            safe_id = _safe_id_text(unit["id"])
            safe_title = _safe_id_text(unit.get("title", ""))
            write(
                f"backend/app/{_ident(unit['id'])}.py",
                f"# mock backend for {safe_id}: {safe_title}\n"
                f"def handler():\n    return {{'unit': {safe_id!r}}}\n",
            )
            status = "dev_done"
        elif role == "dba" and unit:
            # #128: SQL 테이블명도 유효 식별자로 sanitize (따옴표 없이도 안전).
            table = _ident(unit["id"], prefix="t").lower()
            write(
                f"db/migrations/{_ident(unit['id'])}.sql",
                # #12(audit9): SQL 주석에 박히는 id/title 의 개행으로 마이그레이션이 깨지지 않게 sanitize.
                f"-- mock migration for {_safe_id_text(unit['id'])}: "
                f"{_safe_id_text(unit.get('title', ''))}\n"
                f"CREATE TABLE IF NOT EXISTS t_{table} (id INTEGER PRIMARY KEY);\n",
            )
            status = "dev_done"
        elif role == "test-engineer" and unit:
            # #127: pytest 함수명도 유효 식별자로 sanitize.
            tname = _ident(unit["id"], prefix="t").lower()
            write(
                f"tests/test_{tname}.py",
                # #12(audit9): 주석에 박히는 title 의 개행/따옴표로 테스트 파일이 깨지지 않게 sanitize.
                f"def test_{tname}():\n    assert True  "
                f"# mock test for {_safe_id_text(unit.get('title', ''))}\n",
            )
            status = "tested"
        elif role == "qa" and unit:
            qid = _ident(unit["id"], prefix="qa")
            write(f".orchestrator/qa/{qid}.log", f"QA mock: {unit['id']} PASS\n")
            status = "tested"
        elif role == "cicd":
            write(
                ".github/workflows/ci.yml",
                "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo mock-ci\n",
            )
        elif role == "docs-writer":
            # #audit19(C3): mock 도 사람 문서를 4개 언어(EN/KO/JA/ES)로 낸다 — 새 4언어 정책을
            # mock/CI 가 검증할 수 있게. mock 은 무비용 샘플이므로 ja/es 는 영어 본문에 언어
            # 표기를 덧붙인 placeholder 로 둔다(실 백엔드는 docs-writer.md 지시대로 실제 번역).
            for name, (en, ko) in _mock_doc_set().items():
                write(f"docs/{name}.md", en)
                write(f"docs/{name}.ko.md", ko)
                write(f"docs/{name}.ja.md", f"<!-- 日本語 (mock placeholder) -->\n{en}")
                write(f"docs/{name}.es.md", f"<!-- Español (mock placeholder) -->\n{en}")

        result = {
            "status": status,
            "artifacts": artifacts,
            "notes": [f"mock {role}"],
            "blockers": [],
        }
        if units:
            result["units"] = units
        req.result_path.parent.mkdir(parents=True, exist_ok=True)
        req.result_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return RoleResult(
            ok=True,
            final_message=f"[mock:{role}] wrote {len(artifacts)} artifact(s)",
            cost_usd=0.0,
            raw=result,
        )


def _derive_units(spec_text: str) -> list[dict]:
    """spec 의 불릿/번호 항목을 최대 3개까지 작업단위로 (독립 — deps 없음)."""
    feats: list[str] = []
    for line in spec_text.splitlines():
        s = line.strip()
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.+)", s)
        if m and len(feats) < 3:
            feats.append(m.group(1).strip())
    if not feats:
        feats = ["사용자 인증", "핵심 기능"]
    return [
        {
            "id": f"U{i}",
            "title": f[:60],
            "description": f,
            "deps": [],
            "roles": ["frontend-developer", "backend-developer", "dba"],
        }
        for i, f in enumerate(feats, 1)
    ]


def _mock_doc_set() -> dict[str, tuple[str, str]]:
    """무비용 mock 의 전체 문서 세트 샘플 (EN, KO). 실 백엔드는 실제 코드 기준으로 작성."""
    erd = (
        "```mermaid\nerDiagram\n  USERS ||--o{ TASKS : owns\n"
        "  USERS { int id PK\n string email\n string password_hash }\n"
        "  TASKS { int id PK\n int user_id FK\n string title\n string status }\n```\n"
    )
    seq = (
        "```mermaid\nsequenceDiagram\n  participant U as User\n  participant F as Frontend\n"
        "  participant B as Backend\n  U->>F: login(email, pw)\n  F->>B: POST /api/auth/login\n"
        "  B-->>F: token\n  F-->>U: 로그인 완료\n```\n"
    )
    arch = "```mermaid\nflowchart LR\n  FE[React/Vite] -->|REST| BE[FastAPI]\n  BE --> DB[(SQLite)]\n```\n"
    tables = (
        "| column | type | null | key | description |\n|---|---|---|---|---|\n"
        "| id | INTEGER | NO | PK | 고유 id |\n| email | TEXT | NO | UNIQUE | 사용자 이메일 |\n"
        "| password_hash | TEXT | NO |  | 해시된 비밀번호 |\n"
    )
    api = (
        "| method | path | auth | request | response |\n|---|---|---|---|---|\n"
        "| POST | /api/auth/login | - | {email,password} | {token} |\n"
        "| GET | /api/tasks | Bearer | - | [Task] |\n"
    )
    return {
        "index": (
            "# Deliverables (mock sample)\n\n- [ERD](ERD.md) · [SEQUENCE](SEQUENCE.md) · "
            "[DB_TABLES](DB_TABLES.md) · [API](API.md)\n- [USER_MANUAL](USER_MANUAL.md) · "
            "[DEPLOY](DEPLOY.md) · [RUN_GUIDE](RUN_GUIDE.md) · [ARCHITECTURE](ARCHITECTURE.md)\n",
            "# 산출물 문서 (mock 샘플)\n\n- [ERD](ERD.ko.md) · [시퀀스](SEQUENCE.ko.md) · "
            "[DB 테이블 정의서](DB_TABLES.ko.md) · [API 정의서](API.ko.md)\n- "
            "[사용자 매뉴얼](USER_MANUAL.ko.md) · [배포 가이드](DEPLOY.ko.md) · "
            "[실행 가이드](RUN_GUIDE.ko.md) · [아키텍처](ARCHITECTURE.ko.md)\n",
        ),
        "ERD": (f"# ERD\n\n{erd}", f"# ERD (개체-관계도)\n\n{erd}"),
        "SEQUENCE": (
            f"# Sequence Diagrams\n\n## Login\n{seq}",
            f"# 시퀀스 다이어그램\n\n## 로그인\n{seq}",
        ),
        "DB_TABLES": (
            f"# DB Tables\n\n## users\n{tables}",
            f"# DB 테이블 정의서\n\n## users\n{tables}",
        ),
        "API": (f"# API\n\n{api}", f"# API 정의서\n\n{api}"),
        "USER_MANUAL": (
            "# User Manual\n\n1. Sign up / log in.\n2. Create tasks.\n3. Move tasks on the board.\n",
            "# 사용자 매뉴얼\n\n1. 회원가입 / 로그인.\n2. 태스크 생성.\n3. 보드에서 상태 이동.\n",
        ),
        "DEPLOY": (
            "# Deploy Guide\n\n- Env vars: `DB_PATH`, secrets via env.\n- CI: `.github/workflows/ci.yml`.\n"
            "- Build & deploy: container or host.\n",
            "# 배포 가이드\n\n- 환경변수: `DB_PATH`, 비밀값은 env.\n- CI: `.github/workflows/ci.yml`.\n"
            "- 빌드·배포: 컨테이너 또는 호스트.\n",
        ),
        "RUN_GUIDE": (
            "# Run Guide\n\n```bash\npip install -r backend/requirements.txt\n"
            "uvicorn app.main:app --app-dir backend --port 8000\n```\n`pytest tests/`\n",
            "# 실행 가이드\n\n```bash\npip install -r backend/requirements.txt\n"
            "uvicorn app.main:app --app-dir backend --port 8000\n```\n`pytest tests/`\n",
        ),
        "ARCHITECTURE": (f"# Architecture\n\n{arch}", f"# 아키텍처\n\n{arch}"),
    }


def _mock_e2e(spec_text: str) -> str:
    # #129: 고정 시나리오만 내지 않고 spec 에서 파생한 기능 불릿을 포함해 입력을 반영한다.
    # 결정적으로 유지하기 위해 spec 의 불릿/번호 항목을 순서대로 최대 3개만 사용한다.
    feats: list[str] = []
    for line in (spec_text or "").splitlines():
        s = line.strip()
        m = re.match(r"^(?:[-*]|\d+\.)\s+(.+)", s)
        if m and len(feats) < 3:
            feats.append(m.group(1).strip())
    out = [
        "# E2E Test Sheet (mock)\n",
        "## 시나리오 1 — 스모크",
        "- 전제: 앱이 기동되어 있다",
        "- 단계: 메인 페이지에 접속한다",
        "- 기대결과: HTTP 200 응답과 핵심 UI 노출\n",
    ]
    if feats:
        out.append("## 시나리오 2 — spec 기반 기능 점검")
        out.append("- 전제: 사용자가 로그인되어 있다")
        for i, f in enumerate(feats, 1):
            out.append(f"- 단계 {i}: {f}")
        out.append("- 기대결과: 각 기능이 정상 동작한다\n")
    return "\n".join(out)
