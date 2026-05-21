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
from pathlib import Path

from .config import ROLES


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


def render_snapshot(board: dict, roles: list[str]) -> str:
    """Pure text snapshot of the agent table (used by --once and tests)."""
    phase = board.get("phase", "?")
    cost = board.get("total_cost_usd", 0.0)
    units = board.get("units", [])
    done = sum(1 for u in units if u.get("status") == "done")
    agents = board.get("agents", {})

    lines = [f"phase={phase}   cost=${cost:.4f}   units={done}/{len(units)}", ""]
    lines.append(f"   {'agent':<22}{'state':<9}{'$cost':>9}{'calls':>6}  unit")
    lines.append("   " + "-" * 52)
    for r in roles:
        a = agents.get(r, {})
        st = a.get("status", "-")
        icon = "●" if st == "running" else "○"
        unit = a.get("current_unit") or ""
        lines.append(
            f" {icon} {r:<22}{st:<9}{a.get('cost_usd', 0.0):>9.4f}{a.get('calls', 0):>6}  {unit}"
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


def _draw_list(stdscr, board: dict, roles: list[str], sel: int) -> None:
    import curses

    h, w = stdscr.getmaxyx()
    phase = board.get("phase", "—")
    cost = board.get("total_cost_usd", 0.0)
    units = board.get("units", [])
    done = sum(1 for u in units if u.get("status") == "done")
    agents = board.get("agents", {})

    _safe_add(stdscr, 0, 0, " MULTI-AGENT MONITOR ", curses.A_REVERSE | curses.A_BOLD)
    _safe_add(
        stdscr,
        1,
        0,
        f" phase: {phase}    total cost: ${cost:.4f}    units: {done}/{len(units)}",
        curses.A_BOLD,
    )
    _safe_add(
        stdscr, 3, 1, f"{'AGENT':<24}{'STATE':<10}{'$COST':>9}{'CALLS':>6}  UNIT", curses.A_DIM
    )

    row = 4
    for i, r in enumerate(roles):
        a = agents.get(r, {})
        st = a.get("status", "idle")
        running = st == "running"
        icon = "●" if running else "○"
        unit = a.get("current_unit") or "-"
        line = f"{icon} {r:<22}{st:<10}{a.get('cost_usd', 0.0):>9.4f}{a.get('calls', 0):>6}  {unit}"
        attr = curses.A_REVERSE if i == sel else (curses.A_BOLD if running else 0)
        _safe_add(stdscr, row + i, 1, line, attr)

    _safe_add(
        stdscr,
        h - 1,
        0,
        " ↑/↓ move   Enter: open   q: quit ",
        curses.A_REVERSE,
    )


def _draw_detail(stdscr, board: dict, orch_dir: Path, role: str, scroll: int) -> int:
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
        f" state: {st}    $cost: {a.get('cost_usd', 0.0):.4f}    calls: {a.get('calls', 0)}"
        f"    unit: {a.get('current_unit') or '-'}    backend: {a.get('backend') or '-'}",
        curses.A_BOLD if running else 0,
    )
    _safe_add(stdscr, 2, 1, "activity (live):", curses.A_DIM)

    log = _read_agent_log(orch_dir, role)
    body = log.splitlines() if log else ["(아직 활동 없음 — 이 에이전트가 시작되면 채워집니다)"]
    last_msg = a.get("last_message") or ""
    if last_msg:
        body += ["", "── last message ──"] + last_msg.splitlines()

    top = 3
    view_h = max(1, h - top - 1)
    max_scroll = max(0, len(body) - view_h)
    scroll = min(scroll, max_scroll)
    for j, line in enumerate(body[scroll : scroll + view_h]):
        _safe_add(stdscr, top + j, 1, line)

    _safe_add(stdscr, h - 1, 0, " ↑/↓ scroll   b/Esc: back   q: quit ", curses.A_REVERSE)
    return scroll


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
        while True:
            board = _read_board(orch)
            stdscr.erase()
            if not board:
                _safe_add(stdscr, 0, 0, " MULTI-AGENT MONITOR ", curses.A_REVERSE)
                _safe_add(stdscr, 2, 1, f"run 대기 중… {orch / 'board.json'} 가 아직 없습니다.")
                _safe_add(stdscr, 3, 1, "오케스트레이터를 실행하면 자동으로 채워집니다. (q: 종료)")
            elif mode == "list":
                _draw_list(stdscr, board, roles, sel)
            else:
                scroll = _draw_detail(stdscr, board, orch, detail_role, scroll)
            stdscr.refresh()

            try:
                c = stdscr.getch()
            except KeyboardInterrupt:
                break
            if c == -1:
                continue
            if c == ord("q"):
                break
            if mode == "list":
                if c in (curses.KEY_DOWN, ord("j")):
                    sel = min(len(roles) - 1, sel + 1)
                elif c in (curses.KEY_UP, ord("k")):
                    sel = max(0, sel - 1)
                elif c in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                    mode, detail_role, scroll = "detail", roles[sel], 0
            else:
                if c in (ord("b"), 27, curses.KEY_LEFT):
                    mode = "list"
                elif c in (curses.KEY_DOWN, ord("j")):
                    scroll += 1
                elif c in (curses.KEY_UP, ord("k")):
                    scroll = max(0, scroll - 1)

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
        print(render_snapshot(board, list(ROLES)))
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
