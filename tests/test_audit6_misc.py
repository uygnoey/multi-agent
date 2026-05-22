"""audit6 — 잡다 수정 회귀 테스트.

대상:
- #21 : workspace.scaffold 의 .gitignore 시드가 기존 패턴을 중복 추가하지 않는다(dedupe).
- #4/#5/#6 : 문서(PLAN.md/MANIFEST.in) 의 stale 표기 수정 검증(가벼우면서 견고하게).

오프라인·결정적. 외부 백엔드를 호출하지 않는다.
"""

from __future__ import annotations

from pathlib import Path

from orchestrator.workspace import scaffold

REPO_ROOT = Path(__file__).resolve().parent.parent
PLAN_MD = REPO_ROOT / "docs" / "PLAN.md"
MANIFEST = REPO_ROOT / "MANIFEST.in"


# ---------------------------------------------------------------------------
# #21 — .gitignore 시드 dedupe
# ---------------------------------------------------------------------------


def test_gitignore_dedupes_existing_pattern(tmp_path):
    # 기존 .gitignore 에 node_modules/ 가 이미 있으면, 스캐폴딩 후에도 중복되지 않아야 한다.
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n# 사용자 주석\n", encoding="utf-8")

    scaffold(tmp_path, spec_text="demo spec", stack={"backend": "fastapi"})

    lines = [ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()]
    # node_modules/ 는 정확히 한 번만 등장
    assert lines.count("node_modules/") == 1
    # .orchestrator/ 는 결국 ignore 된다
    assert ".orchestrator/" in lines
    # 사용자 주석은 보존
    assert "# 사용자 주석" in lines


def test_gitignore_created_when_absent(tmp_path):
    # .gitignore 가 없으면 새로 생성되고 .orchestrator/ 가 포함된다.
    gi = tmp_path / ".gitignore"
    assert not gi.exists()

    scaffold(tmp_path, spec_text="demo spec", stack={"backend": "fastapi"})

    assert gi.exists()
    lines = [ln.strip() for ln in gi.read_text(encoding="utf-8").splitlines()]
    assert ".orchestrator/" in lines
    assert lines.count("node_modules/") == 1


def test_gitignore_appends_nothing_when_all_present(tmp_path):
    # 모든 시드 패턴이 이미 있으면 아무것도 덧붙이지 않는다(중복 0).
    seed_lines = [".orchestrator/", "__pycache__/", "node_modules/", ".venv/", "*.db"]
    gi = tmp_path / ".gitignore"
    gi.write_text("\n".join(seed_lines) + "\n", encoding="utf-8")
    before = gi.read_text(encoding="utf-8")

    scaffold(tmp_path, spec_text="demo spec", stack={"backend": "fastapi"})

    after = gi.read_text(encoding="utf-8")
    assert after == before
    for pat in seed_lines:
        assert [ln.strip() for ln in after.splitlines()].count(pat) == 1


# ---------------------------------------------------------------------------
# #5/#6/#4 — 문서 stale 표기 수정
# ---------------------------------------------------------------------------


def test_plan_uses_stream_json_for_claude_cli():
    text = PLAN_MD.read_text(encoding="utf-8")
    # stream-json 로 갱신되어 있어야 한다
    assert "stream-json" in text
    # Claude CLI 라인은 더 이상 단일 json blob(--output-format json)을 명시하지 않는다.
    # (stream-json 이 아닌) --output-format json 표기가 남아 있으면 안 된다.
    for line in text.splitlines():
        if "--output-format" in line:
            assert "stream-json" in line
            assert "--output-format json" not in line


def test_plan_does_not_list_write_result_tool():
    text = PLAN_MD.read_text(encoding="utf-8")
    # write_result 는 (read_file/write_file/.../run_bash) 형태의 슬래시 구분 툴 목록에서
    # 제거되어야 한다. (전용 툴이 아님을 설명하는 산문 주석은 허용한다.)
    assert "/write_result" not in text
    assert "write_result/" not in text
    # 실제 5개 툴은 슬래시 목록으로 명시되어 있어야 한다.
    assert "read_file/write_file/edit_file/list_dir/run_bash" in text


def test_manifest_does_not_mention_force_include():
    text = MANIFEST.read_text(encoding="utf-8")
    assert "force-include" not in text
    # data-files 로 갱신되어 있어야 한다.
    assert "data-files" in text
