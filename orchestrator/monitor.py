"""실시간 에이전트 모니터 TUI (stdlib curses, 의존성 0).

별도 프로세스로 실행해 `<project-dir>/.orchestrator/` 의 런 상태를 폴링한다.
  - 리스트 뷰: 10개 역할의 상태(●running/○idle)·누적 비용·호출수·현재 unit
  - ↑/↓(또는 j/k) 이동, Enter 로 상세 진입
  - 상세 뷰: 그 에이전트가 실시간으로 무엇을 하는지(활동 로그) + 비용, b/Esc 로 뒤로
  - q 종료

사용:
  python -m orchestrator.monitor --project-dir <타깃>      # 인터랙티브 TUI
  python -m orchestrator.monitor --project-dir <타깃> --once  # 1회 스냅샷(헤드리스)
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from .backends import backend_status
from .config import BACKEND_INFO, ROLES


def _is_zombie(pid: int) -> bool:
    """pid 가 좀비(이미 종료, 부모가 reap 대기 중) 인지 best-effort 로 판별 (#32).

    webui._is_zombie 와 동일한 기준이지만, 무거운 import 사이클을 피하려고 monitor 에
    가벼운 사본을 둔다. Linux 는 /proc, 그 외(macOS 등)는 `ps` 로 상태 코드를 본다.
    판별 불가 시 False (= 좀비 아님으로 보수적 처리).
    """
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            # 형식: pid (comm) STATE ... — comm 에 ')' 가 있을 수 있어 마지막 ')' 기준.
            txt = stat.read_text(encoding="utf-8", errors="replace")
            state = txt[txt.rfind(")") + 1 :].strip().split(" ", 1)[0]
            return state == "Z"
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip().startswith("Z")
    except Exception:
        return False


def _run_alive(orch_dir: Path) -> bool:
    """run.pid 의 프로세스가 살아있는지 (웹 UI 와 동일 기준)."""
    pf = orch_dir / "run.pid"
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)  # signal 0 = 존재/권한 확인 (죽었으면 OSError)
    except OSError:
        return False
    # #32: 좀비(종료됐지만 reap 안 됨)는 os.kill(pid,0) 이 계속 성공한다. 웹 UI 와 동일하게
    #      좀비는 종료로 본다 (그렇지 않으면 끝난 run 이 TUI 에 계속 "running" 으로 남아
    #      stop/rerun 컨트롤·상태 라벨이 웹과 어긋난다).
    return not _is_zombie(pid)


def _stop_run(orch_dir: Path) -> bool:
    """run.pid 프로세스 그룹 종료 (SIGTERM → 확인 → 필요 시 SIGKILL). 웹 stop 과 동일 기준.

    #6: 예전 구현은 SIGTERM 직후 곧바로 run.pid 를 지웠다. 그러면 프로세스가 아직 살아
    run 상태(board.json 등)를 쓰는 중인데도 _run_alive 가 False 가 되어 TUI 가 "stopped"
    로 표시되는 불일치가 생긴다. 웹 UI(webui.RunManager.stop)처럼, SIGTERM 후 실제 종료를
    "확인"한 뒤에만(또는 SIGKILL 폴백 후) pidfile 을 제거한다.

    TUI 루프를 막지 않도록 동기 작업은 SIGTERM 전송 1회뿐이고, 종료 확인·SIGKILL 에스컬레
    이션·pidfile 제거는 데몬 스레드에서 처리한다. stop 이 시작되면 True 를 반환한다.
    """
    pf = orch_dir / "run.pid"
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        pgid = os.getpgid(pid)
    except Exception:
        pgid = None

    def _kill(sig):
        try:
            os.killpg(pgid, sig) if pgid is not None else os.kill(pid, sig)
        except Exception:
            pass

    def _alive() -> bool:
        # os.kill(pid,0) 은 좀비(종료됐지만 reap 안 됨)도 성공하므로 _is_zombie 로 보정한다
        # (#6/#32: _run_alive 와 동일 기준 → stop 후 상태 라벨이 어긋나지 않게).
        try:
            os.kill(pid, 0)  # signal 0 = 존재/권한 확인 (죽었으면 OSError)
        except OSError:
            return False
        return not _is_zombie(pid)

    def _remove_pidfile():
        try:
            pf.unlink()
        except Exception:
            pass

    def _supervise():
        # SIGTERM 후 graceful 종료를 최대 ≈4초 기다린다(0.1초 간격 폴링). 죽으면 즉시 제거.
        for _ in range(40):
            if not _alive():
                _remove_pidfile()
                return
            time.sleep(0.1)
        # SIGTERM 을 트랩(graceful)해 안 죽는 경우 강제 종료. SIGKILL 은 비동기라 즉시 죽지
        # 않을 수 있어 잠깐 사멸을 확인한 뒤(좀비 reap 포함) pidfile 을 제거한다 — 어떤
        # 경우에도 최종적으로 pidfile 은 반드시 제거되어 "running" 잔상이 남지 않게 한다.
        _kill(signal.SIGKILL)
        for _ in range(20):  # ≈1초: SIGKILL 후 커널 teardown 대기
            if not _alive():
                break
            time.sleep(0.05)
        _remove_pidfile()

    _kill(signal.SIGTERM)
    # 동기 unlink 금지 (#6): pidfile 제거는 종료 확인 후 데몬 스레드에서만 수행.
    threading.Thread(target=_supervise, daemon=True).start()
    return True


def _rerun(orch_dir: Path) -> tuple[bool, str]:
    """저장된 rerun.json(argv)으로 같은 project-dir 에서 오케스트레이터를 다시 실행."""
    if _run_alive(orch_dir):  # 실행 중이면 같은 .orchestrator 동시 쓰기 방지
        return False, "이미 실행 중 — 먼저 정지(s) 후 재실행"
    f = orch_dir / "rerun.json"
    if not f.exists():
        return False, "재실행 정보 없음 (rerun.json 없음 — 이 run 은 재실행 불가)"
    try:
        argv = json.loads(f.read_text(encoding="utf-8")).get("argv") or []
    except Exception:
        return False, "rerun.json 파싱 실패"
    if not argv:
        return False, "재실행 인자 없음"
    ok, why = _validate_rerun_argv(argv)
    if not ok:  # rerun.json 이 손상/조작되면 임의 프로그램 실행 금지 (#90)
        return False, why
    try:
        subprocess.Popen(
            [sys.executable, "-m", "orchestrator", *argv],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return True, "재실행 시작됨 — 곧 갱신됩니다"
    except Exception as e:
        return False, f"재실행 실패: {e}"


# 재실행에 허용되는 오케스트레이터 플래그 화이트리스트 (#15).
# rerun.json 이 조작돼도 알려진 플래그만 통과시켜 예상치 못한 플래그 주입을 막는다.
# argparse 가 자동 추가하는 --help/-h 도 정상 토큰이므로 포함한다.
_ALLOWED_RERUN_FLAGS = frozenset(
    {
        "--spec",
        "--project-dir",
        "--backend",
        "--backends",
        "--distribute",
        "--cross-check",
        "--role-backend",
        "--max-units",
        "--concurrency",
        "--budget",
        "--model",
        "--poll-interval",
        "--delegate",
        "--max-attempts",
        "--retries",
        "--timeout",
        "--mock",
        "--help",
        "-h",
    }
)


def _validate_rerun_argv(argv) -> tuple[bool, str]:
    """rerun.json 의 argv 검증 (#90 + #15).

    rerun.json 은 로컬 신뢰 데이터지만 손상/조작될 수 있으므로 임의 프로그램 실행과
    예상치 못한 플래그 주입을 막는다.
    - argv 는 list[str] 여야 함
    - 첫 토큰은 '--' 로 시작하는 오케스트레이터 플래그여야 함 (절대경로/다른 프로그램명 거부)
    - '-' 로 시작하는 토큰은 모두 화이트리스트(_ALLOWED_RERUN_FLAGS)에 있어야 함.
      플래그가 아닌 토큰은 값(value)으로 보고 통과시킨다 (실용성: 로컬 신뢰 데이터).
      단, '--flag=value' 형태는 '=' 앞부분만 떼어 화이트리스트와 대조한다.
    """
    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        return False, "재실행 인자 형식 오류 (list[str] 아님)"
    first = argv[0]
    # `python -m orchestrator <argv>` 로 실행되므로 첫 토큰은 플래그여야 정상.
    # 절대경로/상대경로 형태(=다른 프로그램 지정 시도)나 비플래그 토큰은 거부.
    if first.startswith("/") or first.startswith("\\") or os.path.isabs(first):
        return False, "재실행 인자 거부 (첫 토큰이 절대경로)"
    if not first.startswith("-"):
        return False, "재실행 인자 거부 (첫 토큰이 오케스트레이터 플래그가 아님)"
    # 화이트리스트 검사: '-' 로 시작하는 모든 토큰(=플래그)을 허용 목록과 대조한다 (#15).
    for tok in argv:
        if not tok.startswith("-"):
            continue  # 비플래그 토큰은 직전 플래그의 값으로 본다
        name = tok.split("=", 1)[0]  # '--flag=value' → '--flag'
        if name not in _ALLOWED_RERUN_FLAGS:
            return False, f"재실행 인자 거부 (허용되지 않은 플래그: {name})"
    return True, ""


def _num(value, fallback: float = 0.0) -> float:
    """board.json 의 숫자 필드를 안전하게 float 로 강제 변환 (#142).

    수동 편집/부분 손상으로 비숫자(null/문자열/list 등)가 들어와도 포매팅(:.4f / :,)이
    터지지 않도록 try float() 후 실패 시 fallback 으로 떨어진다.
    """
    try:
        if isinstance(value, bool):  # bool 은 int 의 서브클래스 → 비용/토큰으로 취급하지 않음
            return fallback
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _read_board(orch_dir: Path) -> dict:
    """board.json 을 읽는다. 파일 없음과 손상을 구분한다 (#70).

    - 파일 없음(아직 run 시작 전): {}  → 대기 화면
    - 파일은 있으나 파싱 불가(상태 손상): {"_corrupt": True}  → 명확한 손상 표시
    """
    p = orch_dir / "board.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"_corrupt": True}
    except Exception:
        return {"_corrupt": True}


def _read_agent_log(orch_dir: Path, role: str, n: int = 500) -> str:
    p = orch_dir / "agents" / f"{role}.log"
    if not p.exists():
        return ""
    try:
        return "\n".join(p.read_text(encoding="utf-8").splitlines()[-n:])
    except Exception:
        return ""


# 상세 뷰 로그 mtime 캐시: 같은 파일이 안 바뀌었으면 매 refresh 마다 다시 읽지 않는다 (#36).
# {경로: (mtime, size, tail_text)}
_LOG_CACHE: dict[str, tuple[float, int, str]] = {}


def _read_agent_log_cached(orch_dir: Path, role: str, n: int = 500) -> str:
    """파일이 바뀌었을 때(mtime/size)만 다시 읽어 큰 로그에서 redraw 가 느려지지 않게 한다 (#36)."""
    p = orch_dir / "agents" / f"{role}.log"
    key = str(p)
    try:
        stat = p.stat()
    except OSError:
        _LOG_CACHE.pop(key, None)
        return ""
    cached = _LOG_CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    text = _read_agent_log(orch_dir, role, n=n)
    _LOG_CACHE[key] = (stat.st_mtime, stat.st_size, text)
    return text


def render_snapshot(board: dict, roles: list[str], alive: bool | None = None) -> str:
    """Pure text snapshot of the agent table (used by --once and tests).

    alive=False 면 죽은 run 으로 보고 running 에이전트를 stopped 로 표시(웹과 동일).
    """
    phase = board.get("phase", "?")
    cost = _num(board.get("total_cost_usd", 0.0))  # 비숫자 손상값에도 :.4f 안 터지게 (#142)
    units = board.get("units", [])
    done = sum(1 for u in units if u.get("status") == "done")
    agents = board.get("agents", {})

    def status_of(a):
        st = a.get("status", "-")
        return "stopped" if (alive is False and st == "running") else st

    run_n = sum(1 for r in roles if status_of(agents.get(r, {})) == "running")
    toks = int(_num(board.get("total_tokens", 0)))
    est = " est." if board.get("cost_estimated") else ""
    state = _state_label(bool(alive), phase, board.get("warnings") or [], units)
    lines = [
        f"phase={phase}   cost=${cost:.4f}{est}   tokens={toks:,}   "
        f"units={done}/{len(units)}   running_agents={run_n}   state={state}",
        "",
        f"   {'agent':<22}{'state':<9}{'model/backend':<20}"
        f"{'$cost':>9}  {'tokens':>9}  {'calls':>5}  unit",
        "   " + "-" * 84,
    ]
    for r in roles:
        a = agents.get(r, {})
        st = status_of(a)
        icon = "●" if st == "running" else "○"
        model = (a.get("model") or a.get("backend") or "")[:18]
        unit = a.get("current_unit") or ""
        a_tok = int(_num(a.get("tokens", 0)))
        tok = f"{a_tok:,}" if a_tok else "-"
        lines.append(
            f" {icon} {r:<22}{st:<9}{model:<20}{_num(a.get('cost_usd', 0.0)):>9.4f}  "
            f"{tok:>9}  {int(_num(a.get('calls', 0))):>5}  {unit}"
        )
    return "\n".join(lines)


# ---------------- curses TUI ----------------


def _safe_add(win, y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w:
        try:
            win.addnstr(y, x, text, max(0, w - x - 1), attr)
        except Exception:
            pass


def _state_label(alive: bool, phase: str, warnings: list, units: list | None = None) -> str:
    """run 상태 라벨: 실행중 / 완료 / 중단. 실패·블록 unit 을 라벨에 노출한다 (#132).

    예: 'done(2 failed)', 'done⚠1(1 blocked)', 'stopped(1 failed)' 처럼 실패가 한눈에 보이게.
    """
    units = units or []
    failed = sum(1 for u in units if u.get("status") == "failed")
    blocked = sum(1 for u in units if u.get("status") == "blocked")
    bad = []
    if failed:
        bad.append(f"{failed} failed")
    if blocked:
        bad.append(f"{blocked} blocked")
    suffix = f"({', '.join(bad)})" if bad else ""
    if alive:
        return "running" + suffix
    if phase == "done":
        base = f"done⚠{len(warnings)}" if warnings else "done"
        return base + suffix
    return "stopped" + suffix


def _disp_width(ch: str) -> int:
    """터미널 표시 폭 (CJK/전각은 2칸)."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _wrap_line(text: str, width: int) -> list[str]:
    """한 줄을 표시 폭 기준으로 soft-wrap (좁은 화면에서 잘리지 않게)."""
    text = text.replace("\t", "    ")
    if width < 2:
        return [text]
    out, cur, cur_w = [], "", 0
    for ch in text:
        cw = _disp_width(ch)
        if cur_w + cw > width:
            out.append(cur)
            cur, cur_w = ch, cw
        else:
            cur += ch
            cur_w += cw
    out.append(cur)
    return out


def _draw_list(stdscr, board, roles, sel, orch_dir, alive) -> None:
    import curses

    h, w = stdscr.getmaxyx()
    phase = board.get("phase", "—")
    cost = _num(board.get("total_cost_usd", 0.0))  # 손상값 가드 (#142)
    units = board.get("units", [])
    done = sum(1 for u in units if u.get("status") == "done")
    agents = board.get("agents", {})

    def status_of(a):
        st = a.get("status", "idle")
        return "stopped" if (not alive and st == "running") else st

    run_n = sum(1 for r in roles if status_of(agents.get(r, {})) == "running")

    _safe_add(stdscr, 0, 0, " MULTI-AGENT MONITOR ", curses.A_REVERSE | curses.A_BOLD)
    _safe_add(
        stdscr,
        1,
        0,
        f" phase:{phase}  cost:${cost:.4f}{' est.' if board.get('cost_estimated') else ''}  "
        f"tokens:{int(_num(board.get('total_tokens', 0))):,}  units:{done}/{len(units)}  "
        f"동시실행:{run_n}  [{_state_label(alive, phase, board.get('warnings') or [], units)}]",
        curses.A_BOLD,
    )
    _safe_add(stdscr, 2, 1, f"📁 {orch_dir.parent}", curses.A_DIM)
    _safe_add(
        stdscr,
        4,
        1,
        f"{'AGENT':<22}{'STATE':<9}{'MODEL/BACKEND':<18}{'$COST':>9}  {'TOKENS':>9}  "
        f"{'CALLS':>5}  UNIT",
        curses.A_DIM,
    )

    row = 5
    for i, r in enumerate(roles):
        a = agents.get(r, {})
        st = status_of(a)
        running = st == "running"
        icon = "●" if running else "○"
        model = (a.get("model") or a.get("backend") or "-")[:16]
        unit = a.get("current_unit") or "-"
        a_tok = int(_num(a.get("tokens", 0)))
        tok = f"{a_tok:,}" if a_tok else "-"
        line = (
            f"{icon} {r:<22}{st:<9}{model:<18}{_num(a.get('cost_usd', 0.0)):>9.4f}  "
            f"{tok:>9}  {int(_num(a.get('calls', 0))):>5}  {unit}"
        )
        attr = curses.A_REVERSE if i == sel else (curses.A_BOLD if running else 0)
        _safe_add(stdscr, row + i, 1, line, attr)

    action = "s: stop" if alive else "r: rerun"
    _safe_add(
        stdscr,
        h - 1,
        0,
        f" ↑/↓ move  Enter: open  {action}  a: artifacts  c: backends  q: quit ",
        curses.A_REVERSE,
    )


def _draw_artifacts(stdscr, board: dict, orch_dir: Path, scroll: int) -> int:
    import curses

    h, w = stdscr.getmaxyx()
    proj = str(orch_dir.parent)
    body = [f"📁 {proj}", ""]
    glob = board.get("artifacts", [])
    if glob:
        body.append("[ 설계·공통 / design & shared ]")
        body += [f"  {proj}/{a}" for a in glob]
        body.append("")
    for u in board.get("units", []):
        arts = u.get("artifacts", [])
        if arts:
            body.append(f"[ {u['id']} — {u.get('title', '')} ]  ({u.get('status')})")
            body += [f"  {proj}/{a}" for a in arts]
            body.append("")
    if len(body) <= 2:
        body.append("(아직 생성된 산출물 없음)")

    _safe_add(stdscr, 0, 0, " ARTIFACTS (생성된 파일) ", curses.A_REVERSE | curses.A_BOLD)
    top = 2
    view_h = max(1, h - top - 1)
    view_w = max(2, w - 2)
    body = [seg for line in body for seg in _wrap_line(line, view_w)]  # soft-wrap
    scroll = min(scroll, max(0, len(body) - view_h))
    for j, line in enumerate(body[scroll : scroll + view_h]):
        _safe_add(stdscr, top + j, 1, line)
    _safe_add(stdscr, h - 1, 0, " ↑/↓ scroll   b/Esc: back   q: quit ", curses.A_REVERSE)
    return scroll


def _draw_backends(stdscr) -> None:
    import curses

    h, _w = stdscr.getmaxyx()
    _safe_add(stdscr, 0, 0, " BACKEND AVAILABILITY ", curses.A_REVERSE | curses.A_BOLD)
    row = 2
    statuses = backend_status()
    last_row = h - 2  # 마지막 줄(h-1)은 footer 용으로 예약
    overflow = False
    for s in statuses:
        if row >= last_row:  # 화면 높이를 넘기면 조용히 사라지지 않게 힌트 표시 (#133)
            overflow = True
            break
        ok = s["ok"]
        icon = "✅" if ok else "❌"
        info = BACKEND_INFO.get(s["name"], "")
        attr = 0 if ok else curses.A_DIM
        _safe_add(stdscr, row, 1, f"{icon} {s['name']:<14}{info:<40}{s['reason']}", attr)
        row += 1
    if overflow:
        _safe_add(stdscr, last_row, 1, "… (터미널을 키우세요 / resize terminal)", curses.A_DIM)
    _safe_add(stdscr, h - 1, 0, " b/Esc: back   q: quit ", curses.A_REVERSE)


def _draw_detail(stdscr, board: dict, orch_dir: Path, role: str, scroll: int, follow: bool):
    """에이전트 상세 로그. follow=True 면 최신(맨 아래)을 자동으로 따라간다(스트리밍).

    (effective_scroll, max_scroll) 를 반환해 호출자가 스크롤 상태를 갱신할 수 있게 한다.
    """
    import curses

    h, w = stdscr.getmaxyx()
    a = board.get("agents", {}).get(role, {})
    st = a.get("status", "idle")
    running = st == "running"

    a_cost = _num(a.get("cost_usd", 0.0))  # 손상값 가드 (#142)
    a_tok = int(_num(a.get("tokens", 0)))
    a_calls = int(_num(a.get("calls", 0)))
    _safe_add(stdscr, 0, 0, f" AGENT: {role} ", curses.A_REVERSE | curses.A_BOLD)
    _safe_add(
        stdscr,
        1,
        0,
        f" state: {st}    model: {a.get('model') or '-'}    $cost: {a_cost:.4f}"
        f"    tokens: {a_tok:,}    calls: {a_calls}"
        f"    unit: {a.get('current_unit') or '-'}    backend: {a.get('backend') or '-'}",
        curses.A_BOLD if running else 0,
    )
    _safe_add(stdscr, 2, 1, "activity (live):", curses.A_DIM)

    # 매 refresh 마다 2000줄을 다시 읽지 않고, mtime 이 바뀐 경우에만 최근 500줄을 재로드 (#36)
    log = _read_agent_log_cached(orch_dir, role, n=500)
    body = log.splitlines() if log else ["(아직 활동 없음 — 이 에이전트가 시작되면 채워집니다)"]
    last_msg = a.get("last_message") or ""
    if last_msg:
        body += ["", "── last message ──"] + last_msg.splitlines()

    top = 3
    view_h = max(1, h - top - 1)
    view_w = max(2, w - 2)
    body = [seg for line in body for seg in _wrap_line(line, view_w)]  # soft-wrap
    max_scroll = max(0, len(body) - view_h)
    scroll = max_scroll if follow else min(scroll, max_scroll)  # follow → 맨 아래
    for j, line in enumerate(body[scroll : scroll + view_h]):
        _safe_add(stdscr, top + j, 1, line)

    foot = " ↑/↓ scroll   G: 최신따라가기   b/Esc: back   q: quit "
    foot += "  ● LIVE(따라가는 중)" if follow else "  ⏸ paused(↑로 위로 봄)"
    _safe_add(stdscr, h - 1, 0, foot, curses.A_REVERSE)
    return scroll, max_scroll


def run_tui(project_dir: Path, interval: float = 1.0) -> None:
    import curses

    orch = Path(project_dir) / ".orchestrator"
    roles = list(ROLES)

    def _loop(stdscr):
        curses.curs_set(0)
        stdscr.timeout(int(interval * 1000))
        mode = "list"
        sel = 0
        detail_role = None
        scroll = 0
        detail_follow = True  # 상세 로그: 기본은 최신 따라가기(스트리밍)
        max_scroll = 0
        status_msg = ""
        while True:
            board = _read_board(orch)
            alive = _run_alive(orch)
            stdscr.erase()
            if mode == "backends":
                _draw_backends(stdscr)
            elif mode == "artifacts":
                scroll = _draw_artifacts(stdscr, board, orch, scroll)
            elif board.get("_corrupt"):  # 손상 board.json 을 빈 보드로 숨기지 않고 표시 (#70)
                _safe_add(stdscr, 0, 0, " MULTI-AGENT MONITOR ", curses.A_REVERSE | curses.A_BOLD)
                _safe_add(
                    stdscr,
                    2,
                    1,
                    f"⚠ board.json 손상 — 읽을 수 없습니다: {orch / 'board.json'}",
                    curses.A_BOLD,
                )
                _safe_add(
                    stdscr, 3, 1, "run 상태가 깨졌습니다. 파일 확인.  c: 백엔드 체크   q: 종료"
                )
            elif not board:
                _safe_add(stdscr, 0, 0, " MULTI-AGENT MONITOR ", curses.A_REVERSE)
                _safe_add(stdscr, 2, 1, f"run 대기 중… {orch / 'board.json'} 가 아직 없습니다.")
                _safe_add(
                    stdscr, 3, 1, "오케스트레이터 실행 시 자동 갱신.  c: 백엔드 체크   q: 종료"
                )
            elif mode == "list":
                _draw_list(stdscr, board, roles, sel, orch, alive)
            else:
                scroll, max_scroll = _draw_detail(
                    stdscr, board, orch, detail_role, scroll, detail_follow
                )
            if status_msg and mode in ("list", "detail"):
                hh, _ww = stdscr.getmaxyx()
                _safe_add(stdscr, hh - 2, 1, "→ " + status_msg, curses.A_BOLD)
            stdscr.refresh()

            try:
                c = stdscr.getch()
            except KeyboardInterrupt:
                break
            if c == -1:
                continue
            if c == ord("q"):
                break
            if mode == "backends":
                if c in (ord("b"), 27, ord("c"), curses.KEY_LEFT):
                    mode = "list"
            elif mode == "artifacts":
                if c in (ord("b"), 27, ord("a"), curses.KEY_LEFT):
                    mode = "list"
                elif c in (curses.KEY_DOWN, ord("j")):
                    scroll += 1
                elif c in (curses.KEY_UP, ord("k")):
                    scroll = max(0, scroll - 1)
            elif mode == "list":
                if c in (curses.KEY_DOWN, ord("j")):
                    sel = min(len(roles) - 1, sel + 1)
                elif c in (curses.KEY_UP, ord("k")):
                    sel = max(0, sel - 1)
                elif c in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                    mode, detail_role, scroll, detail_follow = "detail", roles[sel], 0, True
                elif c == ord("a"):
                    mode, scroll = "artifacts", 0
                elif c == ord("c"):
                    mode = "backends"
                elif c == ord("s"):  # 정지 (실행 중일 때만)
                    if alive:
                        status_msg = (
                            "정지 요청됨 (SIGTERM→SIGKILL)" if _stop_run(orch) else "정지 실패"
                        )
                    else:
                        status_msg = "이미 정지 상태"
                elif c == ord("r"):  # 재실행 (정지 상태에서만)
                    if alive:
                        status_msg = "실행 중 — 먼저 s 로 정지하세요"
                    else:
                        _, status_msg = _rerun(orch)
            else:  # detail: 기본은 최신 따라가기, ↑로 보면 멈춤, G 로 다시 따라가기
                if c in (ord("b"), 27, curses.KEY_LEFT):
                    mode = "list"
                elif c in (curses.KEY_UP, ord("k")):
                    detail_follow = False
                    scroll = max(0, scroll - 1)
                elif c in (curses.KEY_DOWN, ord("j")):
                    scroll = min(scroll + 1, max_scroll)
                    detail_follow = scroll >= max_scroll  # 맨 아래 도달 시 다시 따라가기
                elif c in (ord("G"), curses.KEY_END):
                    detail_follow = True

    curses.wrapper(_loop)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="orchestrator.monitor", description="멀티에이전트 실시간 모니터 TUI"
    )
    p.add_argument("--project-dir", type=Path, required=True, help="감시할 타깃 디렉터리")
    p.add_argument("--once", action="store_true", help="1회 스냅샷만 출력하고 종료 (헤드리스)")
    p.add_argument("--interval", type=float, default=1.0, help="갱신 주기(초)")
    a = p.parse_args(argv)

    orch = a.project_dir.resolve() / ".orchestrator"
    if a.once:
        board = _read_board(orch)
        if board.get("_corrupt"):  # 손상을 빈 보드로 숨기지 않고 명확히 보고 (#70)
            print(f"(board.json corrupt at {orch / 'board.json'})")
            return 1
        if not board:
            print(f"(no run state at {orch / 'board.json'})")
            return 1
        print(render_snapshot(board, list(ROLES), _run_alive(orch)))
        return 0

    try:
        run_tui(a.project_dir.resolve(), a.interval)
    except Exception as e:  # curses 미지원 환경 등
        print(f"TUI 실행 불가: {e}\n--once 로 스냅샷을 보거나 events.log 를 tail 하세요.")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
