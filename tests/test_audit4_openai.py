"""감사 4차(2026-05-22) openai_agents 백엔드 수정 회귀 테스트.

대상 파일: backends/openai_agents.py.
모두 오프라인·결정적이며 agents SDK 없이도 모듈 레벨 순수 헬퍼만으로 검증한다
(SDK 미설치 환경에서도 import 가능 — function_tool 데코레이터는 건드리지 않는다).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from orchestrator.backends import openai_agents as oa

# ---------------------------------------------------------------------------
# #13: edit_file 은 부분 치환(유일성 요구)이다 — write_file 전체 덮어쓰기와 구분.
# ---------------------------------------------------------------------------


def test_edit_file_text_replaces_unique_occurrence():
    src = "alpha\nbeta\ngamma\n"
    out = oa._edit_file_text(src, "beta", "BETA")
    assert out == "alpha\nBETA\ngamma\n"


def test_edit_file_text_missing_old_string_raises():
    with pytest.raises(ValueError, match="not found"):
        oa._edit_file_text("hello world", "absent", "x")


def test_edit_file_text_non_unique_raises():
    # 2회 이상 등장하면 모호하므로 거부한다 (잘못된 위치 치환 방지).
    with pytest.raises(ValueError, match="not unique"):
        oa._edit_file_text("dup\ndup\n", "dup", "X")


def test_edit_file_text_empty_old_string_raises():
    # 빈 old_string 은 생성/덮어쓰기로 오인되기 쉬워 거부 (Write 사용 유도).
    with pytest.raises(ValueError, match="empty"):
        oa._edit_file_text("anything", "", "x")


def test_edit_file_text_replaces_only_first_logical_match_when_unique():
    # 유일하게 등장하는 다행 블록도 정확히 치환된다.
    src = "def f():\n    return 1\n\n# tail\n"
    out = oa._edit_file_text(src, "    return 1", "    return 2")
    assert "return 2" in out and "return 1" not in out


# ---------------------------------------------------------------------------
# #2/#36: _BashCapture 는 보관량을 상한으로 묶고(드롭 표시) 계속 소비할 수 있다.
# ---------------------------------------------------------------------------


def test_bash_capture_caps_and_marks_truncated():
    cap = oa._BashCapture(max_bytes=100)
    cap.feed(b"A" * 60)
    cap.feed(b"B" * 60)  # 합 120 > 100 → 상한까지만 보관
    cap.feed(b"C" * 10)  # 이미 가득 → 전부 드롭
    text = cap.text()
    assert len(text) == 100
    assert cap.truncated is True
    assert text.startswith("A" * 60)
    assert "C" not in text  # 상한 초과분은 버려짐


def test_bash_capture_no_truncate_when_small():
    cap = oa._BashCapture(max_bytes=1000)
    cap.feed(b"hello ")
    cap.feed(b"world")
    assert cap.text() == "hello world"
    assert cap.truncated is False


# ---------------------------------------------------------------------------
# #2: silent 명령도 wall-clock 타임아웃된다 (출력 없어도 데드라인 강제).
# ---------------------------------------------------------------------------


def test_run_bash_silent_command_times_out():
    start = time.monotonic()
    out = oa._run_bash_command("sleep 30", ".", timeout=1.0, max_capture=64 * 1024)
    elapsed = time.monotonic() - start
    assert out.startswith("[timeout]")
    assert elapsed < 10  # 30초 sleep 을 기다리지 않고 ~1초 + 유예 안에 끊긴다


def test_run_bash_normal_command_returns_exit_and_output():
    out = oa._run_bash_command("echo hello", ".", timeout=10.0, max_capture=64 * 1024)
    assert out.startswith("[exit 0]")
    assert "hello" in out


def test_run_bash_nonzero_exit_code_surfaced():
    out = oa._run_bash_command("exit 7", ".", timeout=10.0, max_capture=64 * 1024)
    assert out.startswith("[exit 7]")


def test_run_bash_large_output_is_bounded():
    # 거대 출력도 max_capture 까지만 보관해 메모리를 묶는다 (#36).
    out = oa._run_bash_command(
        f"{sys.executable} -c \"print('X' * 5_000_000)\"",
        ".",
        timeout=30.0,
        max_capture=64 * 1024,
    )
    assert out.startswith("[exit 0]")
    body = out.split("\n", 1)[1]
    # 반환은 4000자로 절단되며 5MB 전체가 아니다.
    assert len(body) <= 4000 + len("\n<... output truncated>")


# ---------------------------------------------------------------------------
# #3: 타임아웃 시 셸이 spawn 한 자식까지 프로세스 그룹째 종료된다 (고아 방지).
# ---------------------------------------------------------------------------


def test_run_bash_kills_child_process_group_on_timeout(tmp_path):
    # 셸이 백그라운드로 자식(sleep)을 띄우고, 그 PID 를 파일로 남긴다. 타임아웃 후
    # 그 자식이 살아있지 않아야 한다 (단일 proc.kill 이면 자식이 고아로 남는다).
    pidfile = tmp_path / "child.pid"
    # 부모 셸은 자식을 background 로 띄운 뒤 자신은 잠깐 살아있다 → 타임아웃 유발.
    cmd = f"sleep 30 & echo $! > {pidfile}; sleep 30"
    out = oa._run_bash_command(cmd, ".", timeout=1.0, max_capture=64 * 1024)
    assert out.startswith("[timeout]")
    # 그룹 종료가 전파될 시간을 약간 준다.
    time.sleep(0.5)
    assert pidfile.exists()
    child_pid = int(pidfile.read_text().strip())
    # 자식이 종료되었는지 확인 (signal 0 으로 존재 여부 검사 → 살아있으면 정리 실패).
    alive = True
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True  # 존재하지만 권한 없음 (살아있음)
    if alive:
        # 테스트가 좀비를 남기지 않도록 정리 시도.
        try:
            os.kill(child_pid, 9)
        except Exception:
            pass
    assert alive is False, f"child {child_pid} survived the process-group kill"


def test_kill_process_group_safe_on_none():
    # proc 이 None 이어도 예외 없이 무시한다.
    oa._kill_process_group(None)


def test_kill_process_group_terminates_running_proc():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    oa._kill_process_group(proc, grace=2.0)
    # 종료 후 returncode 가 채워진다 (None 이면 아직 살아있음).
    assert proc.poll() is not None


# ---------------------------------------------------------------------------
# #1: ORCH_OPENAI_ALLOW_BASH 옵트아웃 (기본 활성, 0/false/no/off 면 비활성).
# ---------------------------------------------------------------------------


def test_bash_enabled_default_when_unset(monkeypatch):
    monkeypatch.delenv("ORCH_OPENAI_ALLOW_BASH", raising=False)
    assert oa._bash_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "FALSE", "no", "off", "Off", ""])
def test_bash_enabled_off_values(monkeypatch, val):
    monkeypatch.setenv("ORCH_OPENAI_ALLOW_BASH", val)
    assert oa._bash_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_bash_enabled_on_values(monkeypatch, val):
    monkeypatch.setenv("ORCH_OPENAI_ALLOW_BASH", val)
    assert oa._bash_enabled() is True
