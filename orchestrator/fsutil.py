"""파일 시스템 원자성 헬퍼 — temp → fsync → os.replace 패턴 통일.

#audit23: workspace 의 spec.md / CLAUDE.md / AGENTS.md / .gitignore 와 OpenAI 백엔드의
write_file/edit_file 가 직접 write_text 또는 O_TRUNC 후 write 했다 → 크래시/ENOSPC
시 부분 파일이 남는 위험. board._flush / Board._atomic_write_text 가 이미 검증된
원자적 쓰기 패턴을 갖고 있으므로 같은 정책을 한 곳에 통합한다.

설계 원칙:
  - tmp = path + ".tmp" 같은 디렉터리에 쓰기 → os.replace 로 원자적 교체 (같은
    파일시스템 보장).
  - 쓰기 도중 예외(ENOSPC/EIO 등)면 stale tmp 를 정리 후 재던짐.
  - flush + fsync 로 본문을 디스크에 강제 반영한 뒤 replace.
  - 디렉터리 fsync 는 rename 메타데이터 영속화 (best-effort, 미지원 플랫폼은 skip).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _make_secure_tmp(path: Path) -> tuple[int, Path]:
    """path 와 같은 디렉터리에 random temp 파일을 만들어 (fd, Path) 반환.

    #audit23-amend (Codex 보안 검증 보정): 이전 ``path.with_name(path.name + ".tmp")`` 의
    predictable 경로는 공격자가 사전에 그 위치에 outside 가리키는 symlink 를 심어두면
    ``open("w")`` 가 symlink 를 따라 outside 파일을 덮어쓰는 우회가 가능했다(Codex 재현).
    ``tempfile.mkstemp`` 은 ``O_CREAT|O_EXCL|O_NOFOLLOW`` 동등 효과로 random name 을 직접
    생성하므로 predictable symlink attack 이 원천 불가능하다. EEXIST 면 random suffix 가
    다른 이름으로 재시도.
    """
    return tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """text 를 path 에 원자적으로 기록 (random tmp → fsync → os.replace).

    plain write_text 는 비-원자적이라 크래시/정전 시 부분 파일이 남는다. 이 함수는
    같은 디렉터리에 mkstemp 로 random temp 파일을 만들고 flush + fsync 후 os.replace 로
    교체해 부분 파일을 영구히 방지한다. random tmp 이름이라 predictable .tmp symlink
    선점 공격(audit23-amend, Codex 재현) 도 차단된다.

    Args:
        path: 최종 파일 경로. 부모 디렉터리는 미리 존재해야 한다.
        text: 기록할 텍스트.
        encoding: 텍스트 인코딩 (기본 utf-8).
    """
    fd, tmp_str = _make_secure_tmp(path)
    tmp = Path(tmp_str)
    try:
        # mkstemp 은 권한을 0o600 으로 생성한다. atomic write 의 최종 파일이 다른 사용자에게
        # 읽혀야 한다면 호출자가 final path 에 chmod 해야 한다(이 도구는 단일 사용자 전제라
        # 기본 0o600 으로 충분 — 오히려 더 안전).
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        # 어떤 예외든 stale tmp 정리 후 재던짐 (board._flush 와 동일 정책)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    _fsync_dir_best_effort(path.parent)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """data 를 path 에 원자적으로 기록 (binary 버전, random tmp + symlink-safe)."""
    fd, tmp_str = _make_secure_tmp(path)
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    _fsync_dir_best_effort(path.parent)


def atomic_append_text(path: Path, addition: str, *, encoding: str = "utf-8") -> None:
    """원자적 append — 기존 내용 + addition 을 한 번의 원자 교체로 쓴다.

    POSIX 의 O_APPEND 가 원자적 append 를 보장하긴 하지만, 크래시 시점에 따라
    부분 라인이 남을 수 있다. 작은 텍스트 파일(.gitignore 등) 은 read-modify-write
    가 더 안전 — 부분 파일 위험 없이 일관 상태 보장.
    """
    try:
        existing = path.read_text(encoding=encoding, errors="replace")
    except FileNotFoundError:
        existing = ""
    except OSError:
        # 읽기 실패는 빈 시작으로 폴백 (best-effort — 호출자가 의도한 동작)
        existing = ""
    atomic_write_text(path, existing + addition, encoding=encoding)


def _fsync_dir_best_effort(directory: Path) -> None:
    """디렉터리 fsync — rename 메타데이터 영속화. 미지원 플랫폼은 조용히 skip."""
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # 일부 플랫폼(Windows 등) 이나 디렉터리는 fd-fsync 미지원 — atomic rename
        # 자체는 유지되므로 무해.
        pass
