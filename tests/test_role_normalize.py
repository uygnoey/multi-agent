"""unit.roles 단축명 정규화 (교차검증 데모에서 드러난 버그 회귀 방지)."""

from __future__ import annotations

import asyncio

from orchestrator.board import Board
from orchestrator.config import DEV_ROLES, normalize_role


def test_normalize_role_maps_short_names():
    assert normalize_role("backend") == "backend-developer"
    assert normalize_role("frontend") == "frontend-developer"
    assert normalize_role("db") == "dba"
    assert normalize_role("architect") == "architecture-engineer"
    assert normalize_role("PM") == "project-manager"
    assert normalize_role("dba") == "dba"  # 정식명 그대로
    assert normalize_role("unknown-x") == "unknown-x"  # 모르는 건 그대로


def test_add_units_normalizes_roles(tmp_path):
    board = Board(tmp_path / "p")
    asyncio.run(board.init("spec", {}))
    # 아키텍트가 단축명으로 적은 경우 (codex 데모에서 실제로 발생)
    asyncio.run(
        board.add_units([{"id": "U1", "title": "t", "roles": ["backend", "frontend", "dba", "qa"]}])
    )
    roles = board.units()[0]["roles"]
    dev = [r for r in roles if r in DEV_ROLES]
    # 정규화 덕분에 스케줄러의 dev 필터가 3종을 모두 잡는다 (이전엔 dba만 잡혔음)
    assert set(dev) == {"frontend-developer", "backend-developer", "dba"}
