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
import unicodedata
from pathlib import Path

from .backends import backend_status
from .config import BACKEND_INFO, ROLES


def _run_alive(orch_dir: Path) -> bool:
    """run.pid 의 프로세스가 살아있는지 (웹 UI 와 동일 기준)."""
    pf = orch_dir / "run.pid"
    if not pf.exists():
        return False
    try:
        os.kill(int(pf.read_text(encoding="utf-8").strip()), 0)
    except (OSError, ValueError):
        return False
    return True


def _stop_run(orch_dir: Path) -> bool:
    """run.pid 프로세스 그룹 종료 (SIGTERM → 4s 후 SIGKILL). 웹 stop 과 동일 기준."""
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

    _kill(signal.SIGTERM)
    threading.Timer(4.0, lambda: _kill(signal.SIGKILL)).start()
    try:
        pf.unlink()
    except Exception:
        pass
    return True


def _rerun(orch_dir: Path) -> tuple[bool, str]:
    """저장된 rerun.json(argv)으로 같은 project-dir 에서 오케스트레이터를 다시 실행."""
    f = orch_dir / "rerun.json"
    if not f.exists():
        return False, "재실행 정보 없음 (rerun.json 없음 — 이 run 은 재실행 불가)"
    try:
        argv = json.loads(f.read_text(encoding="utf-8")).get("argv") or []
    except Exception:
        return False, "rerun.json 파싱 실패"
    if not argv:
        return False, "재실행 인자 없음"
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


def _read_board(orch_dir: Path) -> dict:
    p = orch_dir / "board.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _read_agent_log(orch_dir: Path, role: str, n: int = 400) -> str:
    p = orch_dir / "agents" / f"{role}.log"
    if not p.exists():
        return ""
    try:
        return "\n".join(p.read_text(encoding="utf-8").splitlines()[-n:])
    except Exception:
        return ""


def render_snapshot(board: dict, roles: list[str], alive: bool | None = None) -> str:
    """Pure text snapshot of the agent table (used by --once and tests).

    alive=False 면 죽은 run 으로 보고 running 에이전트를 stopped 로 표시(웹과 동일).
    """
    phase = board.get("phase", "?")
    cost = board.get("total_cost_usd", 0.0)
    units = board.get("units", [])
    done = sum(1 for u in units if u.get("status") == "done")
    agents = board.get("agents", {})

    def status_of(a):
        st = a.get("status", "-")
        return "stopped" if (alive is False and st == "running") else st

    run_n = sum(1 for r in roles if status_of(agents.get(r, {})) == "running")
    toks = board.get("total_tokens", 0)
    est = " est." if board.get("cost_estimated") else ""
    state = _state_label(bool(alive), phase, board.get("warnings") or [])
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
        tok = f"{a.get('tokens', 0):,}" if a.get("tokens") else "-"
        lines.append(
            f" {icon} {r:<22}{st:<9}{model:<20}{a.get('cost_usd', 0.0):>9.4f}  "
            f"{tok:>9}  {a.get('calls', 0):>5}  {unit}"
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


def _state_label(alive: bool, phase: str, warnings: list) -> str:
    """run 상태 라벨: 실행중 / 완료(경고 있으면 ⚠N) / 중단."""
    if alive:
        return "running"
    if phase == "done":
        return f"done⚠{len(warnings)}" if warnings else "done"
    return "stopped"


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
    cost = board.get("total_cost_usd", 0.0)
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
        f"tokens:{board.get('total_tokens', 0):,}  units:{done}/{len(units)}  "
        f"동시실행:{run_n}  [{_state_label(alive, phase, board.get('warnings') or [])}]",
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
        tok = f"{a.get('tokens', 0):,}" if a.get("tokens") else "-"
        line = (
            f"{icon} {r:<22}{st:<9}{model:<18}{a.get('cost_usd', 0.0):>9.4f}  "
            f"{tok:>9}  {a.get('calls', 0):>5}  {unit}"
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
    for s in backend_status():
        ok = s["ok"]
        icon = "✅" if ok else "❌"
        info = BACKEND_INFO.get(s["name"], "")
        attr = 0 if ok else curses.A_DIM
        _safe_add(stdscr, row, 1, f"{icon} {s['name']:<14}{info:<40}{s['reason']}", attr)
        row += 1
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

    _safe_add(stdscr, 0, 0, f" AGENT: {role} ", curses.A_REVERSE | curses.A_BOLD)
    _safe_add(
        stdscr,
        1,
        0,
        f" state: {st}    model: {a.get('model') or '-'}    $cost: {a.get('cost_usd', 0.0):.4f}"
        f"    tokens: {a.get('tokens', 0):,}    calls: {a.get('calls', 0)}"
        f"    unit: {a.get('current_unit') or '-'}    backend: {a.get('backend') or '-'}",
        curses.A_BOLD if running else 0,
    )
    _safe_add(stdscr, 2, 1, "activity (live):", curses.A_DIM)

    log = _read_agent_log(orch_dir, role, n=2000)
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
