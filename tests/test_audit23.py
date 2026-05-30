"""audit23 회귀 테스트 — 묶음 3 (workspace/OpenAI file write atomic 화).

수정 항목(합의된 최종표):
  #1 (MED)  CR-5 workspace 비원자 쓰기 — spec.md / CLAUDE.md / AGENTS.md / .gitignore /
            agent prompt 가 직접 write_text 또는 binary append 였다. 크래시/ENOSPC 시
            부분 파일이 남아 다음 run 의 백엔드 호출이 깨진 시스템 프롬프트로 실패하거나
            .gitignore 부분 라인이 git 동작을 어지럽힐 수 있었다.
  #2 (LOW)  CR-17 OpenAI _write_file_bytes_under_root 가 O_TRUNC 후 직접 write — 크래시 시
            부분 파일. 에이전트가 만든 산출물(코드/설정 파일)이 절단 상태로 남을 수 있었다.
  #3 (인프라) orchestrator.fsutil 공용 모듈 — atomic_write_text/atomic_write_bytes/
            atomic_append_text. board._atomic_write_text 와 같은 정책(temp → fsync →
            os.replace + 디렉터리 fsync best-effort).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# #3 — fsutil 공용 헬퍼 동작 검증
# ---------------------------------------------------------------------------
def test_atomic_write_text_basic() -> None:
    from orchestrator.fsutil import atomic_write_text

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "a.txt"
        atomic_write_text(p, "hello\nworld\n")
        assert p.read_text() == "hello\nworld\n"
        # 덮어쓰기
        atomic_write_text(p, "replaced")
        assert p.read_text() == "replaced"
        # tmp 잔존 없음
        assert not (Path(td) / "a.txt.tmp").exists()


def test_atomic_write_bytes_preserves_arbitrary_bytes() -> None:
    """비-UTF8 바이트(예: 사용자 .gitignore 의 latin-1)도 손상 없이 보존."""
    from orchestrator.fsutil import atomic_write_bytes

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "b.bin"
        data = b"\x00\x01\xff\xfe\xfd\n\xc3(\x80"  # non-UTF-8 sequence
        atomic_write_bytes(p, data)
        assert p.read_bytes() == data


def test_atomic_append_text_creates_if_missing() -> None:
    from orchestrator.fsutil import atomic_append_text

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.txt"
        # 처음 호출: FileNotFoundError → 빈 시작
        atomic_append_text(p, "first\n")
        atomic_append_text(p, "second\n")
        assert p.read_text() == "first\nsecond\n"


def test_atomic_write_cleans_stale_tmp_on_exception(monkeypatch) -> None:
    """write 도중 예외가 나면 stale .tmp 가 정리되어야 한다."""
    from orchestrator import fsutil

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "x.txt"

        # os.replace 직전에 강제 실패시켜 tmp 정리 경로 검증
        real_replace = os.replace

        def fail_replace(src, dst):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(fsutil.os, "replace", fail_replace)
        with pytest.raises(OSError):
            fsutil.atomic_write_text(p, "data")
        # stale tmp 가 정리됐는지
        assert not (Path(td) / "x.txt.tmp").exists()
        # 본체는 미생성 (replace 가 실패했으므로)
        assert not p.exists()
        monkeypatch.setattr(fsutil.os, "replace", real_replace)


# ---------------------------------------------------------------------------
# #1 — CR-5: workspace.scaffold 의 spec.md / CLAUDE.md / AGENTS.md / .gitignore 가
#            모두 원자적으로 기록되고, tmp 잔존이 없어야 한다
# ---------------------------------------------------------------------------
def test_workspace_scaffold_uses_atomic_writes(tmp_path) -> None:
    from orchestrator.workspace import scaffold

    project_dir = tmp_path / "proj"
    scaffold(project_dir, spec_text="# Test Spec\nbody body body\n", stack={})

    # spec.md 작성 + tmp 없음
    spec_path = project_dir / ".orchestrator" / "spec.md"
    assert spec_path.exists()
    assert spec_path.read_text().startswith("# Test Spec")
    assert not (spec_path.parent / "spec.md.tmp").exists()

    # CLAUDE.md / AGENTS.md
    for fname in ("CLAUDE.md", "AGENTS.md"):
        f = project_dir / fname
        assert f.exists()
        assert not (project_dir / f"{fname}.tmp").exists()

    # .gitignore — atomic_write_text 경로
    gi = project_dir / ".gitignore"
    assert gi.exists()
    assert ".orchestrator" in gi.read_text()
    assert not (project_dir / ".gitignore.tmp").exists()


def test_workspace_scaffold_idempotent_gitignore_append(tmp_path) -> None:
    """이미 존재하는 .gitignore 에 누락 시드만 추가 — 원본 바이트 보존 + 원자 교체."""
    from orchestrator.workspace import scaffold

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # 사용자가 비-UTF8 .gitignore 를 갖고 있는 경우(latin-1 주석 등)
    user_gi = project_dir / ".gitignore"
    user_bytes = b"# user comment with latin-1: \xe9\xe8\nnode_modules/\n"
    user_gi.write_bytes(user_bytes)

    scaffold(project_dir, spec_text="x", stack={})

    raw_after = user_gi.read_bytes()
    # 원본 사용자 바이트가 손상 없이 보존 (audit23: 디코드 재기록 금지)
    assert raw_after.startswith(user_bytes), "사용자 비-UTF8 바이트 손상됨"
    # .orchestrator 시드는 추가됐어야
    assert b".orchestrator" in raw_after
    # tmp 잔존 없음
    assert not (project_dir / ".gitignore.tmp").exists()


# ---------------------------------------------------------------------------
# #2 — CR-17: openai_agents._write_file_bytes_under_root 가 원자적으로 쓰고
#             크래시 시 부분 파일이 안 남아야 한다
# ---------------------------------------------------------------------------
def test_openai_write_file_bytes_is_atomic(tmp_path) -> None:
    from orchestrator.backends.openai_agents import _write_file_bytes_under_root

    root = tmp_path / "proj"
    root.mkdir()
    target = root / "out.txt"

    _write_file_bytes_under_root(target, root, b"hello world")
    assert target.read_bytes() == b"hello world"
    # tmp 잔존 없음
    assert not (root / "out.txt.tmp").exists()

    # 덮어쓰기
    _write_file_bytes_under_root(target, root, b"replaced")
    assert target.read_bytes() == b"replaced"
    assert not (root / "out.txt.tmp").exists()


def test_openai_write_file_bytes_cleans_tmp_on_failure(tmp_path, monkeypatch) -> None:
    """rename 실패 시 stale .tmp 가 정리되고 본체는 보존(이전 내용)되어야 한다."""
    from orchestrator.backends import openai_agents

    root = tmp_path / "proj"
    root.mkdir()
    target = root / "out.txt"
    target.write_bytes(b"original")  # 기존 본체

    # os.replace 실패 시뮬레이션
    real_replace = os.replace

    def fail_replace(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(openai_agents.os, "replace", fail_replace)
    with pytest.raises(OSError):
        openai_agents._write_file_bytes_under_root(target, root, b"new data")

    # 본체는 원본 그대로 (atomic 보장)
    assert target.read_bytes() == b"original"
    # stale tmp 정리됨
    assert not (root / "out.txt.tmp").exists()
    monkeypatch.setattr(openai_agents.os, "replace", real_replace)


def test_fsutil_atomic_write_resists_tmp_symlink_to_outside(tmp_path) -> None:
    """#audit23-amend (Codex 보안 검증): predictable .tmp 위치에 outside 가리키는
    symlink 가 사전에 심어져 있어도 outside 파일이 손상되지 않아야 한다.

    이전 ``path.with_name(path.name + '.tmp')`` 는 predictable 경로라 공격자가
    심어둔 symlink 를 따라가 outside 를 덮어쓰는 우회가 가능했다. mkstemp 의
    random name + O_EXCL|O_NOFOLLOW 효과로 차단된다.
    """
    from orchestrator.fsutil import atomic_write_text

    outside = tmp_path / "outside.txt"
    outside.write_text("outside-original")
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "x.txt"
    # 공격자가 사전에 심어둔 predictable .tmp symlink
    try:
        (root / "x.txt.tmp").symlink_to(outside)
    except OSError:
        pytest.skip("symlink unsupported on this filesystem")

    atomic_write_text(target, "new-content")
    # 핵심: outside 가 손상되지 않아야 한다
    assert outside.read_text() == "outside-original", (
        "predictable .tmp symlink 로 outside 파일이 덮어쓰여졌다 (보안 회귀)"
    )
    # 실제 target 은 정상 작성됨
    assert target.read_text() == "new-content"
    assert not target.is_symlink()


def test_openai_write_resists_tmp_symlink_to_inside_victim(tmp_path) -> None:
    """#audit23-amend: predictable .tmp 가 root 안 victim 가리키는 symlink 면
    이전엔 _open_inside 의 ELOOP redirect 가 victim 을 덮어쓰고 최종 target 을
    symlink 로 만들었다. mkstemp random name 으로 차단."""
    from orchestrator.backends.openai_agents import _write_file_bytes_under_root

    root = tmp_path / "proj"
    root.mkdir()
    victim = root / "victim.txt"
    victim.write_bytes(b"victim-original")
    target = root / "out.txt"
    try:
        (root / "out.txt.tmp").symlink_to(victim)
    except OSError:
        pytest.skip("symlink unsupported on this filesystem")

    _write_file_bytes_under_root(target, root, b"new-data")
    # victim 보존
    assert victim.read_bytes() == b"victim-original"
    # target 은 일반 파일로 정상 생성
    assert target.exists()
    assert not target.is_symlink()
    assert target.read_bytes() == b"new-data"


def test_openai_write_rejects_target_parent_outside_root(tmp_path) -> None:
    """actual.parent 가 root 밖이면 명시 거부 — symlink redirect 가 root 밖으로 새는 차단."""
    from orchestrator.backends.openai_agents import _write_file_bytes_under_root

    root = tmp_path / "proj"
    root.mkdir()
    # 정상: root 안 경로는 통과
    _write_file_bytes_under_root(root / "ok.txt", root, b"hi")

    # 비정상: 절대경로로 root 밖을 지정해도 _resolve_under_root 검증으로 거부
    outside_target = tmp_path / "elsewhere.txt"
    with pytest.raises(OSError):
        _write_file_bytes_under_root(outside_target, root, b"injected")
    assert not outside_target.exists()


def test_openai_write_file_rejects_symlink_to_outside(tmp_path) -> None:
    """traversal/symlink 방어가 atomic 화 후에도 유지되어야 한다 (_open_inside 보호 보존)."""

    from orchestrator.backends.openai_agents import _write_file_bytes_under_root

    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("evil")

    # root 안에 outside 를 가리키는 symlink 만들기
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink unsupported on this filesystem")

    # _open_inside 가 O_NOFOLLOW 로 거부하거나, _symlink_target_under_root 가 root 밖 target
    # 을 거부 → OSError 또는 None 처리
    with pytest.raises((OSError, ValueError)):
        _write_file_bytes_under_root(link, root, b"injected")

    # outside 는 손상되지 않음
    assert outside.read_text() == "evil"
