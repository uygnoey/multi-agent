"""공유 보드: <project-dir>/.orchestrator/board.json 의 단일 writer.

오케스트레이터만 이 파일을 갱신한다. 역할 세션은 타깃 repo 파일을 편집하고
결과 JSON 만 남기며, 그 결과를 읽어 보드를 전이시키는 것은 오케스트레이터다.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

# unit 상태 머신
TODO = "todo"
DESIGNING = "designing"
DESIGNED = "designed"
IN_PROGRESS = "in_progress"
DEV_DONE = "dev_done"
TESTING = "testing"
TESTED = "tested"
DONE = "done"
BLOCKED = "blocked"
FAILED = "failed"

TERMINAL_OK = (DONE, TESTED)


class Board:
    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.orch_dir = self.project_dir / ".orchestrator"
        self.path = self.orch_dir / "board.json"
        self.results_dir = self.orch_dir / "results"
        self.events_path = self.orch_dir / "events.log"
        self.directives_path = self.orch_dir / "directives.md"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"units": []}
        self.spec_text: str = ""

    # ---- persistence ----
    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def init(self, spec_text: str, stack: dict) -> None:
        async with self._lock:
            self.orch_dir.mkdir(parents=True, exist_ok=True)
            self.results_dir.mkdir(parents=True, exist_ok=True)
            self._data = {
                "created_at": time.time(),
                "spec_excerpt": spec_text[:2000],
                "stack": stack,
                "phase": "init",
                "total_cost_usd": 0.0,
                "units": [],
            }
            self._flush()
        await self.log_event("board", "initialized")

    # ---- mutations (single writer) ----
    async def add_units(self, units: list[dict]) -> None:
        async with self._lock:
            existing = {u["id"] for u in self._data["units"]}
            for u in units:
                uid = u.get("id")
                if not uid or uid in existing:
                    continue
                existing.add(uid)
                self._data["units"].append(
                    {
                        "id": uid,
                        "title": u.get("title", uid),
                        "description": u.get("description", ""),
                        "status": DESIGNED,
                        "deps": list(u.get("deps", [])),
                        "roles": list(u.get("roles", []))
                        or ["frontend-developer", "backend-developer", "dba"],
                        "artifacts": [],
                        "test_status": None,
                        "notes": [],
                    }
                )
            self._flush()
        await self.log_event("board", f"added {len(units)} unit(s)")

    async def set_status(self, unit_id: str, status: str, note: str | None = None) -> None:
        async with self._lock:
            for u in self._data["units"]:
                if u["id"] == unit_id:
                    u["status"] = status
                    if note:
                        u["notes"].append(note)
            self._flush()
        await self.log_event(unit_id, f"status={status}" + (f" :: {note}" if note else ""))

    async def add_artifacts(self, unit_id: str, artifacts: list[str]) -> None:
        if not artifacts:
            return
        async with self._lock:
            for u in self._data["units"]:
                if u["id"] == unit_id:
                    for a in artifacts:
                        if a not in u["artifacts"]:
                            u["artifacts"].append(a)
            self._flush()

    async def set_test_status(self, unit_id: str, test_status: str) -> None:
        async with self._lock:
            for u in self._data["units"]:
                if u["id"] == unit_id:
                    u["test_status"] = test_status
            self._flush()

    async def set_phase(self, phase: str) -> None:
        async with self._lock:
            self._data["phase"] = phase
            self._flush()

    async def add_cost(self, amount: float) -> None:
        async with self._lock:
            self._data["total_cost_usd"] = round(
                self._data.get("total_cost_usd", 0.0) + float(amount), 6
            )
            self._flush()

    def write_report(self) -> Path:
        """Write a human-readable run report to .orchestrator/report.md."""
        d = self._data
        units = d.get("units", [])
        done = sum(1 for u in units if u["status"] == "done")
        lines = [
            "# Run Report",
            "",
            f"- phase: **{d.get('phase')}**",
            f"- units done: **{done}/{len(units)}**",
            f"- total cost: **${d.get('total_cost_usd', 0.0):.4f}**",
            f"- stack: {d.get('stack')}",
            "",
            "## Units",
            "",
            "| id | status | test | artifacts | title |",
            "|----|--------|------|-----------|-------|",
        ]
        for u in units:
            lines.append(
                f"| {u['id']} | {u['status']} | {u.get('test_status')} | "
                f"{len(u.get('artifacts', []))} | {u.get('title', '')} |"
            )
        report = self.orch_dir / "report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report

    # ---- reads (best-effort snapshots) ----
    def units(self) -> list[dict]:
        return [dict(u) for u in self._data.get("units", [])]

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    # ---- logs / directives ----
    async def log_event(self, who: str, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{who}] {msg}\n"
        async with self._lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def recent_events(self, n: int = 20) -> str:
        if not self.events_path.exists():
            return ""
        return "\n".join(self.events_path.read_text(encoding="utf-8").splitlines()[-n:])

    async def append_directive(self, who: str, text: str) -> None:
        block = f"\n### {time.strftime('%H:%M:%S')} — {who}\n{text}\n"
        async with self._lock:
            with self.directives_path.open("a", encoding="utf-8") as f:
                f.write(block)

    def directives(self) -> str:
        if self.directives_path.exists():
            return self.directives_path.read_text(encoding="utf-8")
        return ""
