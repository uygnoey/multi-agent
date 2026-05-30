"""audit24-A 회귀 테스트 — board predictable .tmp symlink 선점 공격 차단.

수정 항목:
  board._flush / board._atomic_write_text 가 이전에 predictable ``self.path.with_suffix
  (".json.tmp")`` / ``path.with_name(path.name + ".tmp")`` 를 썼다. 공격자가 사전에 그
  위치에 outside 가리키는 symlink 를 심어두면 open("w") 가 따라가 outside 파일을
  덮어쓰는 우회가 가능했다(audit23-amend 의 Codex 보안 재현과 동일 클래스 약점).
  fsutil.atomic_write_text 의 mkstemp 기반 random tmp 패턴으로 통합 → predictable
  symlink 선점 공격 원천 차단. flush+fsync+os.replace+디렉터리 fsync 정책 동일.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest


def test_board_flush_resists_tmp_symlink_to_outside() -> None:
    """board.json.tmp predictable 위치에 outside 가리키는 symlink 가 있어도
    outside 파일이 손상되지 않아야 한다."""
    from orchestrator.board import Board

    async def run() -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            outside = td_path / "outside.txt"
            outside.write_text("outside-original")

            proj = td_path / "proj"
            board = Board(proj)
            # init 으로 .orchestrator/ 생성 + 1차 _flush
            await board.init("spec", {})

            # 공격자가 사전에 predictable .tmp 자리에 outside 가리키는 symlink 심기
            bjson_tmp = board.path.with_suffix(".json.tmp")
            try:
                bjson_tmp.symlink_to(outside)
            except OSError:
                pytest.skip("symlink unsupported on this filesystem")

            # 다음 mutate → _flush 트리거. mkstemp random tmp 패턴이라 우회 불가.
            await board.add_warning("test-warning")

            assert outside.read_text() == "outside-original", (
                "predictable board.json.tmp symlink 로 outside 파일이 덮어쓰여졌다"
            )
            # board.json 본체는 정상
            assert board.path.exists()
            content = board.path.read_text()
            assert "test-warning" in content

    asyncio.run(run())


def test_board_atomic_write_text_resists_tmp_symlink_to_outside(tmp_path) -> None:
    """Board._atomic_write_text(report/deliverables) 도 같은 약점 보유했었음.
    fsutil 위임으로 동일 보안 강화."""
    from orchestrator.board import Board

    outside = tmp_path / "outside.txt"
    outside.write_text("outside-original")
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "report.md"
    try:
        (proj / "report.md.tmp").symlink_to(outside)
    except OSError:
        pytest.skip("symlink unsupported on this filesystem")

    Board._atomic_write_text(target, "new-report-content")

    assert outside.read_text() == "outside-original"
    assert target.read_text() == "new-report-content"
    assert not target.is_symlink()


def test_board_flush_still_atomic_on_normal_path(tmp_path) -> None:
    """기존 정상 흐름(crash 없음)에서도 board.json 이 정상 작성되고 .tmp 잔존 없음."""
    from orchestrator.board import Board

    async def run() -> None:
        board = Board(tmp_path / "proj")
        await board.init("spec body", {"lang": "py"})
        await board.add_warning("w1")
        await board.add_cost(0.05)

        # board.json 정상 + JSON 파싱 가능
        import json

        data = json.loads(board.path.read_text())
        assert data.get("spec_excerpt", "").startswith("spec body")
        assert "w1" in data.get("warnings", [])
        assert abs(data.get("total_cost_usd", 0) - 0.05) < 1e-9

        # random tmp 파일 잔존 없음 (mkstemp 가 만든 게 모두 replace 되었거나 정리됐어야)
        leftovers = [
            p
            for p in board.path.parent.iterdir()
            if p.name.startswith("board.json.") and p.name.endswith(".tmp")
        ]
        assert leftovers == [], f"tmp files leaked: {[p.name for p in leftovers]}"

    asyncio.run(run())
