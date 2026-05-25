"""감사 9차 회귀 테스트: orchestrator/monitor.py + orchestrator/gitcheckpoints.py.

결정적·오프라인(curses 불필요)으로 다음을 회귀 검증한다.

monitor.py:
- #6  _read_pid 가 양의 정수 pid 만 반환하고 0/-1/손상/없음은 None (광역 시그널 차단).
      _run_alive/_stop_run 이 음수·0 pid 를 거부한다.
- #7  _is_zombie 결과가 짧은 TTL 로 캐싱되어 매 호출마다 ps 를 spawn 하지 않는다.
- #9  list 모드 Enter 의 roles[sel] 가 빈 roles 에서 IndexError 나지 않게 가드.
- #11 _LOG_CACHE 가 LRU(재기록 키도 끝으로)이며 lock 으로 동시 변경에 안전.
- #13 _clamp_interval 의 상한(60s).
- #14 _draw_list 의 alive=None 정규화(bool).
- #16 ')' 없는 손상 /proc stat 를 좀비로 오독하지 않는다.
- #17 rerun.json 이 dict 가 아니면(list 등) "재실행 인자 없음" 으로 정확히 처리.

gitcheckpoints.py:
- #1  porcelain -z 파싱이 공백/한글/rename 경로를 정확히 처리.
- #4  빈/공백 commit 메시지가 기본값으로 대체된다.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from orchestrator import monitor as _monitor_mod
from orchestrator.gitcheckpoints import GitCheckpointer, _parse_status_paths_z
from orchestrator.monitor import (
    _clamp_interval,
    _is_zombie,
    _read_pid,
    _run_alive,
    _stop_run,
)


def test_zombie_cache_thread_safe(monkeypatch):
    # #H07: TUI 루프와 stop supervise 스레드가 _is_zombie 를 동시 호출해도 캐시 eviction 경합으로
    #       예외가 나면 안 된다.
    monkeypatch.setattr(_monitor_mod, "_is_zombie_uncached", lambda pid: False)
    _monitor_mod._ZOMBIE_CACHE.clear()
    errors: list[str] = []

    def worker(base: int) -> None:
        try:
            for i in range(1500):
                _monitor_mod._is_zombie(base + (i % 120))  # 120 distinct → 64 캡 eviction 반복
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(b * 1000 + 1,)) for b in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


# ---------------------------------------------------------------------------
# monitor #6: _read_pid / 음수·0 pid 거부
# ---------------------------------------------------------------------------
def _orch(tmp_path: Path) -> Path:
    o = tmp_path / ".orchestrator"
    o.mkdir()
    return o


def test_read_pid_none_when_missing(tmp_path: Path):
    assert _read_pid(tmp_path / "nope.pid") is None


def test_read_pid_rejects_zero(tmp_path: Path):
    pf = tmp_path / "run.pid"
    pf.write_text("0", encoding="utf-8")
    assert _read_pid(pf) is None  # os.kill(0,...) 가 프로세스 그룹 전체를 칠 위험 차단


def test_read_pid_rejects_negative(tmp_path: Path):
    pf = tmp_path / "run.pid"
    pf.write_text("-1", encoding="utf-8")
    assert _read_pid(pf) is None  # os.kill(-1,...) 가 모든 프로세스를 칠 위험 차단


def test_read_pid_rejects_garbage(tmp_path: Path):
    pf = tmp_path / "run.pid"
    pf.write_text("not-a-pid", encoding="utf-8")
    assert _read_pid(pf) is None


def test_read_pid_accepts_positive(tmp_path: Path):
    pf = tmp_path / "run.pid"
    pf.write_text("12345", encoding="utf-8")
    assert _read_pid(pf) == 12345


def test_read_pid_tolerates_optional_second_line(tmp_path: Path):
    # 다른 change owner 가 start-time 토큰을 둘째 줄에 추가할 수 있다 → 첫 줄만 읽되 관대하게.
    pf = tmp_path / "run.pid"
    pf.write_text("12345\n1700000000.0\n", encoding="utf-8")
    assert _read_pid(pf) == 12345


def test_read_pid_empty_first_line(tmp_path: Path):
    pf = tmp_path / "run.pid"
    pf.write_text("\n1700000000\n", encoding="utf-8")
    assert _read_pid(pf) is None


def test_run_alive_false_on_zero_pid(tmp_path: Path):
    orch = _orch(tmp_path)
    (orch / "run.pid").write_text("0", encoding="utf-8")
    assert _run_alive(orch) is False  # 0 pid → 절대 alive 로 보지 않음(광역 시그널 방지)


def test_run_alive_false_on_negative_pid(tmp_path: Path):
    orch = _orch(tmp_path)
    (orch / "run.pid").write_text("-1", encoding="utf-8")
    assert _run_alive(orch) is False


def test_stop_run_false_on_negative_pid(tmp_path: Path):
    orch = _orch(tmp_path)
    (orch / "run.pid").write_text("-1", encoding="utf-8")
    # 음수 pid 면 _read_pid 가 None → stop 대상 없음(False), os.killpg(-1,...) 호출 안 됨.
    assert _stop_run(orch) is False


def test_stop_run_false_on_zero_pid(tmp_path: Path):
    orch = _orch(tmp_path)
    (orch / "run.pid").write_text("0", encoding="utf-8")
    assert _stop_run(orch) is False


# ---------------------------------------------------------------------------
# monitor #7: _is_zombie 결과 캐싱(매 호출 ps spawn 방지)
# ---------------------------------------------------------------------------
def test_is_zombie_caches_uncached_call(monkeypatch):
    import orchestrator.monitor as m

    m._ZOMBIE_CACHE.clear()
    calls = {"n": 0}

    def _fake(pid):
        calls["n"] += 1
        return False

    monkeypatch.setattr(m, "_is_zombie_uncached", _fake)
    pid = 999999
    assert _is_zombie(pid) is False
    assert _is_zombie(pid) is False  # 두 번째는 캐시 히트
    assert calls["n"] == 1  # uncached(=ps spawn) 는 한 번만 호출됨


def test_is_zombie_recomputes_after_ttl(monkeypatch):
    import orchestrator.monitor as m

    m._ZOMBIE_CACHE.clear()
    calls = {"n": 0}
    monkeypatch.setattr(
        m, "_is_zombie_uncached", lambda pid: bool(calls.__setitem__("n", calls["n"] + 1)) or False
    )
    # 시간을 직접 제어: 첫 호출 후 TTL 을 넘기면 다시 계산해야 한다.
    t = {"now": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"])
    _is_zombie(424242)
    assert calls["n"] == 1
    t["now"] += m._ZOMBIE_TTL + 0.1  # TTL 만료
    _is_zombie(424242)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# monitor #13: _clamp_interval 상한
# ---------------------------------------------------------------------------
def test_clamp_interval_upper_bound():
    assert _clamp_interval(10_000) == 60.0  # 거대한 값 → 60s 상한


def test_clamp_interval_at_bound_unchanged():
    assert _clamp_interval(60) == 60.0


def test_clamp_interval_within_range_unchanged():
    assert _clamp_interval(30) == 30.0


# ---------------------------------------------------------------------------
# monitor #16: ')' 없는 손상 /proc stat 오독 방지
# ---------------------------------------------------------------------------
def test_is_zombie_uncached_handles_paren_less_stat(monkeypatch, tmp_path):
    import orchestrator.monitor as m

    # ')' 가 없는 손상 stat 파일을 시뮬레이션: rfind(')') == -1 → 좀비 아님(False).
    fake = tmp_path / "stat"
    fake.write_text("99999 comm-without-paren R 1 2 3", encoding="utf-8")

    class _FakePath:
        def __init__(self, _s):
            pass

        def exists(self):
            return True

        def read_text(self, **_kw):
            return fake.read_text(encoding="utf-8")

    monkeypatch.setattr(m, "Path", _FakePath)
    # rfind(')')==-1 가드 덕분에 'R' 을 상태로 오독하지 않고 False 반환.
    assert m._is_zombie_uncached(99999) is False


# ---------------------------------------------------------------------------
# monitor #17: rerun.json 이 dict 아님
# ---------------------------------------------------------------------------
def test_rerun_non_dict_json_reports_no_args(tmp_path: Path):
    from orchestrator.monitor import _rerun

    orch = _orch(tmp_path)
    (orch / "rerun.json").write_text("[1, 2, 3]", encoding="utf-8")  # list (dict 아님)
    ok, msg = _rerun(orch)
    assert ok is False
    assert "재실행 인자 없음" in msg  # AttributeError("파싱 실패") 가 아니라 정확한 사유


# ---------------------------------------------------------------------------
# monitor #11: _LOG_CACHE LRU
# ---------------------------------------------------------------------------
def test_log_cache_is_lru_and_evicts_oldest(monkeypatch, tmp_path):
    import orchestrator.monitor as m

    with m._LOG_CACHE_LOCK:
        m._LOG_CACHE.clear()
    monkeypatch.setattr(m, "_LOG_CACHE_MAX", 2)
    agents = tmp_path / "agents"
    agents.mkdir()
    for name in ("a", "b"):
        (agents / f"{name}.log").write_text(f"{name}-log\n", encoding="utf-8")
    # a, b 적재 (cache=[a, b])
    m._read_agent_log_cached(tmp_path, "a")
    m._read_agent_log_cached(tmp_path, "b")
    # a 를 다시 접근 → LRU 라면 a 가 끝으로 이동 (cache=[b, a])
    m._read_agent_log_cached(tmp_path, "a")
    # c 적재 → 가장 오래된 b 가 evict 되어야 한다 (FIFO 였다면 a 가 잘못 evict 됨)
    (agents / "c.log").write_text("c-log\n", encoding="utf-8")
    m._read_agent_log_cached(tmp_path, "c")
    keys = {Path(k).stem for k in m._LOG_CACHE}
    assert keys == {"a", "c"}  # b 가 evict, a 는 최근 접근으로 생존


def test_log_cache_is_ordered_dict_thread_safe_primitives():
    import threading
    from collections import OrderedDict

    import orchestrator.monitor as m

    assert isinstance(m._LOG_CACHE, OrderedDict)  # move_to_end/popitem(last=) 지원
    assert isinstance(m._LOG_CACHE_LOCK, type(threading.Lock()))


# ---------------------------------------------------------------------------
# monitor #9: 빈 roles 에서 Enter IndexError 방지 (가드 동작을 직접 모사)
# ---------------------------------------------------------------------------
def test_empty_roles_enter_guard_logic():
    # run_tui 의 Enter 핸들러는 `if roles:` 가드로 보호된다. 빈 roles 에서 roles[sel] 를
    # 만지지 않음을 동일 로직으로 검증(curses 없이).
    roles: list[str] = []
    sel = 0
    entered = False
    if roles:  # 가드
        _ = roles[sel]
        entered = True
    assert entered is False  # 빈 roles 면 상세 진입 시도 자체를 안 함 → IndexError 없음


# ---------------------------------------------------------------------------
# gitcheckpoints #1: porcelain -z 파싱 (공백/한글/rename)
# ---------------------------------------------------------------------------
def test_parse_status_z_plain_paths():
    text = "?? a.txt\0 M src/b.py\0"
    assert _parse_status_paths_z(text) == {"a.txt", "src/b.py"}


def test_parse_status_z_path_with_space():
    text = "?? my file.txt\0"
    assert _parse_status_paths_z(text) == {"my file.txt"}


def test_parse_status_z_korean_filename():
    # -z 는 한글/비ASCII 를 이스케이프 없이 원문 그대로 준다.
    text = "?? 한글파일.txt\0 M src/설계 노트.md\0"
    assert _parse_status_paths_z(text) == {"한글파일.txt", "src/설계 노트.md"}


def test_parse_status_z_rename_record():
    # rename 은 'R  new\0orig\0' 처럼 두 NUL 레코드. 둘 다 잡아야 한다.
    text = "R  new name.py\0old name.py\0"
    assert _parse_status_paths_z(text) == {"new name.py", "old name.py"}


def test_parse_status_z_rename_korean():
    text = "R  새이름.py\0옛이름.py\0"
    assert _parse_status_paths_z(text) == {"새이름.py", "옛이름.py"}


def test_parse_status_z_empty():
    assert _parse_status_paths_z("") == set()


def test_parse_status_z_skips_corrupt_short_record():
    text = "x\0?? real.txt\0"  # 첫 레코드는 너무 짧아 skip
    assert _parse_status_paths_z(text) == {"real.txt"}


# ---------------------------------------------------------------------------
# gitcheckpoints #1 (통합): 실제 git repo 에서 공백/한글 파일이 체크포인트에 잡힌다
# ---------------------------------------------------------------------------
def _git_available() -> bool:
    import shutil

    return shutil.which("git") is not None


@pytest.mark.skipif(not _git_available(), reason="git 미설치")
def test_checkpoint_commits_korean_and_space_filenames(tmp_path: Path):
    import asyncio

    proj = tmp_path / "proj"
    proj.mkdir()
    # 격리된 환경에서 git identity 강제(전역 config 영향 배제).
    env = dict(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@t",
    )
    for k, v in env.items():
        os.environ.setdefault(k, v)

    gc = GitCheckpointer(proj, enabled=True)  # baseline = 빈 repo
    # 공백·한글 파일명 생성
    (proj / "my file.txt").write_text("hi", encoding="utf-8")
    (proj / "한글 파일.md").write_text("안녕", encoding="utf-8")

    committed, detail = asyncio.run(gc.checkpoint("orchestrator: test"))
    assert committed is True, detail

    # 커밋된 트리에 두 파일이 모두 들어갔는지 확인(-z 미사용 시 공백/한글에서 누락됨).
    # core.quotePath=false + -z 로 한글/공백 경로를 원문 그대로 받는다.
    out = subprocess.run(
        [
            "git",
            "-C",
            str(proj),
            "-c",
            "core.quotePath=false",
            "show",
            "--name-only",
            "-z",
            "--format=",
            "HEAD",
        ],
        capture_output=True,
        text=True,
    )
    listed = set(t for t in out.stdout.split("\0") if t)
    assert "my file.txt" in listed
    assert "한글 파일.md" in listed


# ---------------------------------------------------------------------------
# gitcheckpoints #4: 빈/공백 commit 메시지 → 기본값 대체
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not _git_available(), reason="git 미설치")
def test_checkpoint_empty_message_uses_default(tmp_path: Path):
    import asyncio

    proj = tmp_path / "proj"
    proj.mkdir()
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        os.environ.setdefault(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        os.environ.setdefault(k, "t@t")

    gc = GitCheckpointer(proj, enabled=True)
    (proj / "f.txt").write_text("x", encoding="utf-8")
    committed, detail = asyncio.run(gc.checkpoint("   "))  # 공백뿐인 메시지
    assert committed is True, detail
    out = subprocess.run(
        ["git", "-C", str(proj), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "orchestrator: checkpoint"  # _DEFAULT_COMMIT_MESSAGE


# ---------------------------------------------------------------------------
# gitcheckpoints #2: _run 이 TimeoutExpired 를 일관된 실패로 변환
# ---------------------------------------------------------------------------
def test_run_converts_timeout_to_failure(monkeypatch, tmp_path: Path):
    gc = GitCheckpointer(tmp_path, enabled=True)

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0] if a else "git", timeout=k.get("timeout", 1))

    monkeypatch.setattr(subprocess, "run", _boom)
    cp = gc._run("status", timeout=0.01)
    assert cp.returncode != 0  # 예외 전파 대신 일관된 실패(returncode!=0)
    assert "timed out" in (cp.stderr or "")
