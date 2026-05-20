"""무비용 mock 백엔드.

LLM 호출 없이 역할별로 결정적 산출물 + 결과 JSON 을 직접 생성한다.
오케스트레이션 배선(스캐폴딩→설계→동시개발→테스트 트리거→CI/CD)을
API 키 없이 end-to-end 로 검증하는 용도.
"""
from __future__ import annotations

import json
import re

from .base import Backend, RoleRequest, RoleResult


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
                + "\n".join(f"- {u['id']}: {u['title']}" for u in units),
            )
            status = "designed"
        elif role == "testsheet-creator":
            write("docs/test/e2e-sheet.md", _mock_e2e(req.spec_text))
        elif role == "frontend-developer" and unit:
            write(
                f"frontend/src/{unit['id']}.jsx",
                f"// mock frontend for {unit['id']}: {unit.get('title','')}\n"
                f"export default function {unit['id']}() {{ return null; }}\n",
            )
            status = "dev_done"
        elif role == "backend-developer" and unit:
            write(
                f"backend/app/{unit['id']}.py",
                f"# mock backend for {unit['id']}: {unit.get('title','')}\n"
                f"def handler():\n    return {{'unit': '{unit['id']}'}}\n",
            )
            status = "dev_done"
        elif role == "dba" and unit:
            write(
                f"db/migrations/{unit['id']}.sql",
                f"-- mock migration for {unit['id']}: {unit.get('title','')}\n"
                f"CREATE TABLE IF NOT EXISTS t_{unit['id'].lower()} (id INTEGER PRIMARY KEY);\n",
            )
            status = "dev_done"
        elif role == "test-engineer" and unit:
            write(
                f"tests/test_{unit['id'].lower()}.py",
                f"def test_{unit['id'].lower()}():\n"
                f"    assert True  # mock test for {unit.get('title','')}\n",
            )
            status = "tested"
        elif role == "qa" and unit:
            write(f".orchestrator/qa/{unit['id']}.log", f"QA mock: {unit['id']} PASS\n")
            status = "tested"
        elif role == "cicd":
            write(
                ".github/workflows/ci.yml",
                "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - run: echo mock-ci\n",
            )

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


def _mock_e2e(spec_text: str) -> str:
    return (
        "# E2E Test Sheet (mock)\n\n"
        "## 시나리오 1 — 스모크\n"
        "- 전제: 앱이 기동되어 있다\n"
        "- 단계: 메인 페이지에 접속한다\n"
        "- 기대결과: HTTP 200 응답과 핵심 UI 노출\n"
    )
