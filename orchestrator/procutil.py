"""프로세스 식별 유틸 (PID 재사용 방어, #M6).

run.pid 에 pid 와 함께 '시작 시각 토큰'을 저장해, OS 가 같은 pid 를 무관한 새 프로세스에
재할당했을 때(stale pidfile + pid 재사용) 그 프로세스를 우리 run 으로 오인해 stop 시 *엉뚱한*
프로세스에 시그널을 보내는 일을 막는다. 토큰을 못 구하는 환경에서는 빈 문자열을 쓰고, 그 경우
호출부는 pid 생존 확인만으로 폴백한다(하위 호환 — 쓰기 포맷에 둘째 줄이 없어도 동작).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

# 시작시각 토큰은 프로세스가 살아있는 한 변하지 않으므로 캐싱해도 안전하다. 다만 pid 재사용 시
# 죽은 pid 의 옛 토큰을 잘못 재사용하지 않도록 짧은 TTL 을 둔다(비-Linux 의 `ps` spawn 비용도
# 절감 — _run_alive 가 매 refresh 마다 호출될 수 있다). {pid: (만료시각, 토큰)}
_TOKEN_CACHE: dict[int, tuple[float, str]] = {}
_TOKEN_TTL = 1.0  # 초
# #H06: webui 는 ThreadingHTTPServer 라 여러 요청 스레드가 process_start_token 을 동시 호출한다.
# lock 없이 dict 를 get/set/eviction 하면 eviction 의 items() 순회 중 다른 스레드 삽입으로
# "RuntimeError: dictionary changed size during iteration" 가 날 수 있다. 캐시 접근을 lock 으로
# 보호한다(느린 토큰 계산은 lock 밖에서 수행해 스레드를 직렬화하지 않는다).
_TOKEN_LOCK = threading.Lock()


def _compute_start_token(pid: int) -> str:
    """pid 의 시작 시각을 식별하는 안정 토큰(best-effort). 못 구하면 "".

    Linux: /proc/<pid>/stat 의 starttime(부팅 후 clock tick, 22번째 필드).
    macOS/BSD: `ps -o lstart=` (프로세스 절대 시작 시각 문자열).
    """
    if pid <= 0:
        return ""
    try:
        if sys.platform.startswith("linux"):
            txt = Path(f"/proc/{pid}/stat").read_text()
            # comm 필드에 공백/괄호가 있을 수 있으므로 마지막 ')' 뒤(state 필드부터)를 쪼갠다.
            close = txt.rfind(")")
            if close == -1:
                return ""
            fields = txt[close + 2 :].split()
            # state 가 인덱스 0(=3번째 필드)이므로 starttime(22번째)은 인덱스 19.
            return fields[19] if len(fields) > 19 else ""
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        return ""
    return ""


def process_start_token(pid: int) -> str:
    """`_compute_start_token` 의 TTL 캐시 버전."""
    if pid <= 0:
        return ""
    now = time.monotonic()
    with _TOKEN_LOCK:
        hit = _TOKEN_CACHE.get(pid)
        if hit is not None and hit[0] > now:
            return hit[1]
    token = _compute_start_token(pid)  # 느린 I/O 는 lock 밖에서 (스레드 직렬화 방지)
    with _TOKEN_LOCK:
        _TOKEN_CACHE[pid] = (now + _TOKEN_TTL, token)
        # 캐시가 무한정 커지지 않게 만료된 항목을 가끔 청소한다(스냅샷 순회로 동시변경 방어).
        if len(_TOKEN_CACHE) > 256:
            for k in [k for k, v in list(_TOKEN_CACHE.items()) if v[0] <= now]:
                _TOKEN_CACHE.pop(k, None)
    return token


def format_pidfile(pid: int) -> str:
    """run.pid 본문: 1줄=pid, 2줄=시작시각 토큰(구할 수 있으면). 토큰이 없으면 pid 한 줄만."""
    token = process_start_token(pid)
    return f"{pid}\n{token}\n" if token else f"{pid}\n"


def read_pid_token(path: Path) -> str | None:
    """run.pid 둘째 줄의 시작시각 토큰. 없으면 None(하위 호환)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return lines[1].strip() if len(lines) > 1 and lines[1].strip() else None


def pid_is_ours(pid: int, stored_token: str | None) -> bool:
    """pid 가 우리가 기록한 바로 그 프로세스인지 검증한다.

    저장된 토큰이 없거나(구형 pidfile) 현재 토큰을 못 구하면 True 로 폴백한다 — pid 생존
    확인은 호출부가 별도로 하므로, 토큰 검증은 'pid 가 재사용됐을 때만' 추가로 걸러낸다.
    """
    if not stored_token:
        return True
    current = process_start_token(pid)
    if not current:
        return True
    return current == stored_token
