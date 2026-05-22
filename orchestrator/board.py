"""공유 보드: <project-dir>/.orchestrator/board.json 의 단일 writer.

오케스트레이터만 이 파일을 갱신한다. 역할 세션은 타깃 repo 파일을 편집하고
결과 JSON 만 남기며, 그 결과를 읽어 보드를 전이시키는 것은 오케스트레이터다.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
import time
from pathlib import Path
from typing import Any

from .config import normalize_role

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

# 제어문자(개행/탭/CR 포함): 아티팩트 경로에 끼면 문서 주입/표 왜곡을 일으키므로 제거 대상.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# 로그/디렉티브 본문 최대 길이: LLM 폭주 출력이 파일을 무한히 키우는 것을 방지.
_MAX_BODY_CHARS = 20000


def _truncate_body(text: str) -> str:
    """본문을 _MAX_BODY_CHARS 로 잘라 '…(truncated)' 마커를 붙임 (runaway 파일 증가 방지)."""
    s = str(text)
    if len(s) <= _MAX_BODY_CHARS:
        return s
    return s[:_MAX_BODY_CHARS] + "\n…(truncated)"


def _coerce_finite_float(raw) -> float:
    """비-숫자/NaN/Inf 는 0.0 으로 변환 (잘못된 비용 메타데이터 방어)."""
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val if math.isfinite(val) else 0.0


def _coerce_int(raw) -> int:
    """비-정수 입력은 0 으로 변환 (잘못된 토큰 메타데이터 방어)."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _safe_artifact(raw) -> str | None:
    """아티팩트 경로 경량 검증: str 만 허용, 제어문자 제거 후 절대경로/'..' 포함 시 drop.

    이 경로들은 타깃 프로젝트 기준 상대경로다. 비-str/빈값/안전하지 않은 값은 None.
    """
    if not isinstance(raw, str):
        return None
    # 개행/탭/CR 등 제어문자를 먼저 제거 → report/deliverables 표·문서 주입 차단.
    # 제거 후 양끝 공백을 strip 하고, 빈 문자열이 되면 drop.
    s = _CONTROL_CHARS.sub("", raw).strip()
    if not s:
        return None
    # 절대경로(POSIX '/' 또는 Windows 드라이브/역슬래시) 차단
    if s.startswith("/") or s.startswith("\\"):
        return None
    if len(s) >= 2 and s[1] == ":":  # 예: C:\... 형태의 드라이브 절대경로
        return None
    # 경로 traversal('..') 토큰 차단 (제어문자 제거 후 재검사)
    parts = re.split(r"[\\/]+", s)
    if ".." in parts:
        return None
    return s


def _safe_unit_id(raw) -> str:
    """unit id 를 경로/파일명/식별자에 안전한 문자만 남겨 정규화.

    '/', '..', 공백, 특수문자를 '-' 로 치환 → result 파일/마이그레이션/생성코드에서 traversal 차단.
    안전화 후 빈 문자열이면 "" (호출부에서 skip).
    """
    if raw in (None, ""):
        return ""
    s = _UNSAFE_ID.sub("-", str(raw).strip()).strip("-")
    return s


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


def _md_cell(v) -> str:
    """마크다운 표 셀 안전화: 파이프/개행이 표를 깨지 않게 이스케이프."""
    return str(v).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


# 리포트/산출물에 찍히는 경고 1건의 최대 길이: 거대한 경고가 report.md 를 부풀리지 않게 캡.
_MAX_WARNING_CHARS = 500


def _safe_report_num(raw) -> float:
    """리포트 포맷용 숫자 강제: float() 실패/bool/NaN/Inf 는 0.0.

    부분 손상된 보드(문자열/null/list/bool 비용·토큰)가 :.4f 포맷에서 터지지 않게 방어해
    report.md / docs/DELIVERABLES*.md 가 항상 기록되도록(복구성) 보장한다.
    """
    # bool 은 int 의 서브클래스라 float(True)==1.0 이 되므로 명시적으로 거부.
    if isinstance(raw, bool):
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val if math.isfinite(val) else 0.0


def _safe_warning(raw) -> str:
    """경고 문자열을 리포트에 안전하게: 개행/파이프 중화 후 길이 캡(구조 왜곡·비대화 방지)."""
    s = _md_cell(raw)
    if len(s) > _MAX_WARNING_CHARS:
        s = s[:_MAX_WARNING_CHARS] + "…(truncated)"
    return s


# tail 시 끝에서부터 읽을 청크 크기: 큰 로그 전체를 메모리에 올리지 않기 위함(약 128KB).
_TAIL_CHUNK_BYTES = 128 * 1024

# 매 프롬프트에 주입되는 directives 누적분의 최대 크기(약 16KB). 끝(최신)에서부터만 읽는다 (#21).
_MAX_DIRECTIVES_BYTES = 16 * 1024


def _tail_lines(path: Path, n: int) -> list[str]:
    """파일 끝에서 마지막 청크(~128KB)만 seek-read 해 마지막 n 줄을 반환.

    전체 파일을 읽지 않아 대용량 로그에서도 메모리/IO 가 일정하다. 작은 파일,
    디코드 오류는 모두 graceful 하게 처리한다.
    """
    if n <= 0:
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, 2)  # 파일 끝으로 이동
            size = f.tell()
            # 작은 파일은 통째로, 큰 파일은 마지막 청크만 읽는다.
            start = max(0, size - _TAIL_CHUNK_BYTES)
            f.seek(start)
            chunk = f.read()
    except OSError:
        return []
    # 잘못된 바이트는 무시(errors='ignore')해 디코드 오류로 죽지 않게 함.
    text = chunk.decode("utf-8", errors="ignore")
    # 청크가 줄 중간에서 시작했다면 첫 줄은 불완전할 수 있으니 버린다(파일 시작이 아닌 경우).
    if start > 0:
        nl = text.find("\n")
        text = text[nl + 1 :] if nl != -1 else ""
    return text.splitlines()[-n:]


def _norm_str_list(v) -> list[str]:
    """deps/roles 입력 정규화: list→[str…], scalar→[str], dict/None/빈값→[] (이상값 방어)."""
    if v in (None, "") or isinstance(v, dict):
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


class Board:
    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.orch_dir = self.project_dir / ".orchestrator"
        self.path = self.orch_dir / "board.json"
        self.results_dir = self.orch_dir / "results"
        self.agents_dir = self.orch_dir / "agents"
        self.events_path = self.orch_dir / "events.log"
        self.directives_path = self.orch_dir / "directives.md"
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] = {"units": [], "agents": {}}
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
            self.agents_dir.mkdir(parents=True, exist_ok=True)
            self._data = {
                "created_at": time.time(),
                "spec_excerpt": spec_text[:2000],
                "stack": stack,
                "phase": "init",
                "total_cost_usd": 0.0,
                "total_tokens": 0,
                "cost_estimated": False,
                "warnings": [],
                "agents": {},
                "artifacts": [],  # 설계/공통 산출물 (특정 unit 에 속하지 않는)
                "units": [],
            }
            self._flush()
        await self.log_event("board", "initialized")

    # ---- mutations (single writer) ----
    async def add_units(self, units: list[dict]) -> None:
        added = 0
        # 락 안에서는 add_warning(동일 락 재획득) 을 호출할 수 없으므로 충돌 경고를
        # 모아 두었다가 락 해제 후 기록한다.
        collision_warnings: list[str] = []
        async with self._lock:
            existing = {u["id"] for u in self._data["units"]}
            # 이번 호출에서 본 raw id(문자열화) 집합: 동일 raw 의 재투입은 진짜 중복 → skip.
            # str() 로 키를 만들어 dict/list/숫자 같은 비정상 입력에도 해시 안전하다.
            seen_raw: set[str] = set()
            for u in units:
                raw_id = u.get("id")
                raw_key = str(raw_id)
                # 동일한 raw id 가 이번 호출에서 또 나오면 진짜 중복 → skip.
                if raw_key in seen_raw:
                    continue
                seen_raw.add(raw_key)
                # 숫자 ID 문자열화 + 경로/식별자 안전 문자만 (traversal·특수문자 차단)
                uid = _safe_unit_id(raw_id)
                if not uid:
                    continue
                if uid in existing:
                    if raw_key == uid:
                        # 이미 정규형(canonical) id 로 존재 → 동일 unit 의 멱등 재투입이므로
                        # 조용히 skip(중복 생성/경고 없음). 기존 add_units 멱등성 계약 유지.
                        continue
                    # 서로 다른 raw 입력이 같은 sanitized id 로 충돌하면 조용히 버리지 않고
                    # 숫자 접미사("-2","-3"…)를 붙여 보존하고 경고를 남긴다(silent drop 금지).
                    base = uid
                    suffix = 2
                    new_uid = f"{base}-{suffix}"
                    while new_uid in existing:
                        suffix += 1
                        new_uid = f"{base}-{suffix}"
                    collision_warnings.append(
                        f"unit id collision: {raw_id!r} sanitized to {base!r} which "
                        f"already exists; renamed to {new_uid!r}"
                    )
                    uid = new_uid
                existing.add(uid)
                added += 1
                # deps/roles 정규화: list→문자열들, scalar→[scalar], dict/None→[] (이상값 방어)
                # deps 도 unit id 와 동일하게 _safe_unit_id 로 안전화해야 sanitize 된
                # id("U/1"→"U-1")와 매칭됨. 안전화 후 빈 항목은 drop.
                deps = [d for d in (_safe_unit_id(x) for x in _norm_str_list(u.get("deps"))) if d]
                roles_raw = _norm_str_list(u.get("roles"))
                self._data["units"].append(
                    {
                        "id": uid,
                        "title": u.get("title", uid),
                        "description": u.get("description", ""),
                        "status": DESIGNED,
                        "deps": deps,
                        "roles": [normalize_role(r) for r in roles_raw]
                        or ["frontend-developer", "backend-developer", "dba"],
                        "artifacts": [],
                        "test_status": None,
                        "notes": [],
                    }
                )
            self._flush()
        # 충돌로 인한 rename 은 락 밖에서 보드 경고로 기록(가시성 확보).
        for msg in collision_warnings:
            await self.add_warning(msg)
        skipped = len(units) - added
        extra = f" ({skipped} skipped: dup/invalid id)" if skipped else ""
        await self.log_event("board", f"added {added} unit(s){extra}")

    async def add_warning(self, msg: str) -> None:
        """치명적이지 않지만 최종 성공으로 오해되면 안 되는 실패(설계/CI/문서 등)를 기록."""
        async with self._lock:
            self._data.setdefault("warnings", []).append(msg)
            self._flush()
        await self.log_event("scheduler", f"WARNING: {msg}")

    async def set_status(self, unit_id: str, status: str, note: str | None = None) -> None:
        matched = False
        async with self._lock:
            for u in self._data["units"]:
                if u["id"] == unit_id:
                    u["status"] = status
                    if note:
                        u["notes"].append(note)
                    matched = True
            self._flush()
        if matched:
            # 실제로 unit 이 갱신된 경우에만 상태 전이를 기록 (거짓 성공 방지)
            await self.log_event(unit_id, f"status={status}" + (f" :: {note}" if note else ""))
        else:
            # 매칭되는 unit 이 없으면 거짓 성공 대신 명확한 'unknown unit' 기록 (raise 하지 않음)
            await self.log_event(unit_id, f"WARNING: unknown unit, status={status} not applied")

    async def add_artifacts(self, unit_id: str, artifacts: list[str]) -> None:
        if not artifacts:
            return
        # 아티팩트 경량 검증: str 만, strip, 절대경로/'..' 등 안전하지 않은 값 drop
        clean = [s for s in (_safe_artifact(a) for a in artifacts) if s]
        if not clean:
            return
        matched = False
        async with self._lock:
            for u in self._data["units"]:
                if u["id"] == unit_id:
                    matched = True
                    for a in clean:
                        if a not in u["artifacts"]:
                            u["artifacts"].append(a)
            self._flush()
        if not matched:
            # 알 수 없는 unit id → 아티팩트가 조용히 사라지지 않도록 경고 기록
            await self.log_event(
                unit_id, f"WARNING: unknown unit, {len(clean)} artifact(s) dropped"
            )

    async def set_test_status(self, unit_id: str, test_status: str) -> None:
        matched = False
        async with self._lock:
            for u in self._data["units"]:
                if u["id"] == unit_id:
                    u["test_status"] = test_status
                    matched = True
            self._flush()
        if not matched:
            # 알 수 없는 unit id → 테스트 결과가 조용히 유실되지 않도록 경고 기록
            await self.log_event(
                unit_id, f"WARNING: unknown unit, test_status={test_status} dropped"
            )

    async def set_phase(self, phase: str) -> None:
        async with self._lock:
            self._data["phase"] = phase
            self._flush()

    async def add_global_artifacts(self, artifacts: list[str]) -> None:
        """특정 unit 에 속하지 않는 설계/공통 산출물 (architect docs, cicd 등)."""
        if not artifacts:
            return
        # 아티팩트 경량 검증: str 만, strip, 절대경로/'..' 등 안전하지 않은 값 drop
        clean = [s for s in (_safe_artifact(a) for a in artifacts) if s]
        if not clean:
            return
        async with self._lock:
            g = self._data.setdefault("artifacts", [])
            for a in clean:
                if a not in g:
                    g.append(a)
            self._flush()

    async def add_cost(self, amount: float) -> None:
        # NaN/Inf 같은 비정상 float 는 JSON 에 NaN/Infinity 로 새지 않도록 무시 (0 처리)
        try:
            val = float(amount)
        except (TypeError, ValueError):
            return
        if not math.isfinite(val):
            return
        # 비용은 누적만 가능하며 절대 감소하면 안 됨 → 음수 입력은 무시(0 처리)
        if val < 0:
            return
        async with self._lock:
            self._data["total_cost_usd"] = round(self._data.get("total_cost_usd", 0.0) + val, 6)
            self._flush()

    # ---- per-agent live state (for the monitor TUI) ----
    async def agent_update(
        self,
        role: str,
        *,
        status: str | None = None,
        unit: str | None = None,
        backend: str | None = None,
        cost_add: float | None = None,
        message: str | None = None,
        call: bool = False,
        activity: str | None = None,
        model: str | None = None,
        tokens_add: int | None = None,
        cost_est: bool = False,
    ) -> None:
        async with self._lock:
            agents = self._data.setdefault("agents", {})
            a = agents.setdefault(
                role,
                {
                    "status": "idle",
                    "calls": 0,
                    "cost_usd": 0.0,
                    "cost_est": False,
                    "tokens": 0,
                    "current_unit": None,
                    "backend": None,
                    "model": None,
                    "last_message": "",
                    "updated_at": 0.0,
                },
            )
            if status is not None:
                a["status"] = status
            if unit is not None or status == "running":
                a["current_unit"] = unit
            if backend is not None:
                a["backend"] = backend
            if model is not None:
                a["model"] = model
            if cost_add:
                # 잘못된 비용 메타데이터(비-숫자/NaN/Inf)가 업데이트를 깨지 않도록 방어
                add = _coerce_finite_float(cost_add)
                # per-agent 비용도 누적만 가능 → 음수는 무시(비용이 감소하지 않게)
                if add > 0:
                    a["cost_usd"] = round(a["cost_usd"] + add, 6)
            if cost_est:
                a["cost_est"] = True
                self._data["cost_estimated"] = True
            if tokens_add:
                # 잘못된 토큰 메타데이터(비-정수 등)는 0 으로 강제(int 코어션 가드 유지)
                add_t = _coerce_int(tokens_add)
                # 토큰도 비용처럼 누적만 가능 → 음수는 no-op(per-agent/total 둘 다 감소 금지)
                if add_t > 0:
                    a["tokens"] = a.get("tokens", 0) + add_t
                    self._data["total_tokens"] = self._data.get("total_tokens", 0) + add_t
            if message is not None:
                a["last_message"] = message[:500]
            if call:
                a["calls"] += 1
            a["updated_at"] = time.time()
            self._flush()
        if activity:
            self._append_agent_log(role, activity)

    def _log_path(self, role: str) -> Path:
        """role 을 안전한 파일명으로 정규화해 agents_dir 밖으로 쓰지 못하게 차단(traversal 방지)."""
        safe = _safe_unit_id(role) or "_unknown"
        return self.agents_dir / f"{safe}.log"

    def _append_agent_log(self, role: str, text: str) -> None:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        with self._log_path(role).open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {text}\n")

    def write_agent_block(self, role: str, title: str, body: str) -> None:
        """프롬프트/결과 같은 상세 블록을 per-agent 로그에 기록 (실시간 상세 로그용)."""
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        bar = "─" * 56
        # 단일 프롬프트/결과가 로그를 폭증시키지 않도록 본문 크기 제한
        body = _truncate_body(body)
        with self._log_path(role).open("a", encoding="utf-8") as f:
            f.write(f"\n{bar}\n{time.strftime('%H:%M:%S')} {title}\n{bar}\n{body}\n")

    def agents(self) -> dict:
        return json.loads(json.dumps(self._data.get("agents", {})))

    def agent_log_tail(self, role: str, n: int = 200) -> str:
        # 쓰기 경로와 동일하게 안전화된 파일명을 사용해 일관되게 읽기
        p = self._log_path(role)
        if not p.exists():
            return ""
        # 전체 파일을 읽지 않고 끝 청크만 seek-read 해 마지막 n 줄만 반환(대용량 방어).
        return "\n".join(_tail_lines(p, n))

    def write_report(self) -> Path:
        """Write a human-readable run report to .orchestrator/report.md."""
        d = self._data
        units = d.get("units", [])
        done = sum(1 for u in units if u["status"] == "done")
        failed = [u for u in units if u["status"] in (BLOCKED, FAILED)]
        warnings = d.get("warnings") or []
        if failed:
            result = f"❌ failed ({len(failed)} unit)"
        elif not units:
            # unit 이 하나도 없는 보드는 'ok' 로 보이면 안 됨 (작업 없음/상태 유실 가능)
            result = "⚠ no units"
        elif warnings:
            result = "⚠ done with warnings"
        else:
            result = "ok"
        lines = [
            "# Run Report",
            "",
            f"- phase: **{d.get('phase')}**",
            f"- result: **{result}**",
            f"- units done: **{done}/{len(units)}**",
            f"- total cost: **${_safe_report_num(d.get('total_cost_usd', 0.0)):.4f}**",
            f"- stack: {d.get('stack')}",
        ]
        if warnings:
            # 경고는 개행/마크다운/거대 텍스트를 포함할 수 있어 중화+길이 캡 후 기록.
            lines += ["", "## ⚠ Warnings", ""] + [f"- {_safe_warning(w)}" for w in warnings]
        lines += [
            "",
            "## Units",
            "",
            "| id | status | test | artifacts | title |",
            "|----|--------|------|-----------|-------|",
        ]
        for u in units:
            lines.append(
                f"| {_md_cell(u['id'])} | {_md_cell(u['status'])} | "
                f"{_md_cell(u.get('test_status'))} | "
                f"{len(u.get('artifacts', []))} | {_md_cell(u.get('title', ''))} |"
            )
        report = self.orch_dir / "report.md"
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report

    def write_deliverables(self) -> list[str]:
        """보드 상태로 개발 산출물 문서를 EN/KO 양쪽으로 생성 (보드 요약은 fallback).

        docs-writer 백엔드가 이미 docs/DELIVERABLES.md/.ko.md 를 작성한 경우 그 풍부한
        산출물을 덮어쓰지 않는다. 파일이 없을 때만 보드 요약을 써서 fallback 을 보장하고,
        실제로 디스크에 존재하는 경로만 반환한다(스케줄러가 전역 아티팩트로 추가).
        """
        d = self._data
        units = d.get("units", [])
        done = sum(1 for u in units if u["status"] == "done")
        artifacts = d.get("artifacts", [])
        docs_dir = self.project_dir / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)

        def table(headers):
            out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
            for u in units:
                out.append(
                    f"| {_md_cell(u['id'])} | {_md_cell(u['status'])} | "
                    f"{_md_cell(u.get('test_status'))} | "
                    f"{len(u.get('artifacts', []))} | {_md_cell(u.get('title', ''))} |"
                )
            return out

        def unit_files():
            out = []
            for u in units:
                if u.get("artifacts"):
                    # id/title 이스케이프: LLM 제공 값이 마크다운 구조를 깨지 못하게
                    out.append(f"### {_md_cell(u['id'])} — {_md_cell(u.get('title', ''))}")
                    # 아티팩트도 최소한 개행을 중화해 문서 주입 방지
                    out += [f"- {_md_cell(a)}" for a in u["artifacts"]]
                    out.append("")
            return out

        en = (
            [
                "# Development Deliverables",
                "",
                f"- phase: **{d.get('phase')}**",
                f"- units done: **{done}/{len(units)}**",
                f"- total cost: **${_safe_report_num(d.get('total_cost_usd', 0.0)):.4f}**",
                f"- stack: {d.get('stack')}",
                "",
                "## Work units",
                "",
            ]
            + table(["id", "status", "test", "files", "title"])
            + ["", "## Design & shared artifacts", ""]
            # 전역 산출물도 per-unit 과 동일하게 _md_cell 로 이스케이프(개행/파이프 주입 방지)
            + ([f"- {_md_cell(a)}" for a in artifacts] or ["- (none)"])
            + ["", "## Per-unit files", ""]
            + unit_files()
            + ["See `docs/RUN_GUIDE.md` for how to run."]
        )
        ko = (
            [
                "# 개발 산출물",
                "",
                f"- 단계: **{d.get('phase')}**",
                f"- 완료 unit: **{done}/{len(units)}**",
                f"- 총비용: **${_safe_report_num(d.get('total_cost_usd', 0.0)):.4f}**",
                f"- 스택: {d.get('stack')}",
                "",
                "## 작업 단위",
                "",
            ]
            + table(["id", "상태", "테스트", "파일수", "제목"])
            + ["", "## 설계·공통 산출물", ""]
            # 전역 산출물도 per-unit 과 동일하게 _md_cell 로 이스케이프(개행/파이프 주입 방지)
            + ([f"- {_md_cell(a)}" for a in artifacts] or ["- (없음)"])
            + ["", "## 단위별 파일", ""]
            + unit_files()
            + ["실행 방법은 `docs/RUN_GUIDE.ko.md` 참고."]
        )
        # 에이전트가 작성한 산출물을 보드 요약으로 덮어쓰지 않는다.
        # 파일이 없을 때만 보드 요약을 써서 항상 fallback 이 존재하도록 보장한다.
        en_path = docs_dir / "DELIVERABLES.md"
        ko_path = docs_dir / "DELIVERABLES.ko.md"
        if not en_path.exists():
            en_path.write_text("\n".join(en) + "\n", encoding="utf-8")
        if not ko_path.exists():
            ko_path.write_text("\n".join(ko) + "\n", encoding="utf-8")
        # 실제로 존재하는 경로만 반환(에이전트가 쓴 것이든 보드가 쓴 것이든 모두 포함).
        written: list[str] = []
        if en_path.exists():
            written.append("docs/DELIVERABLES.md")
        if ko_path.exists():
            written.append("docs/DELIVERABLES.ko.md")
        return written

    # ---- reads (best-effort snapshots) ----
    def units(self) -> list[dict]:
        # 중첩 리스트(artifacts/deps/roles/notes)까지 깊은 복사해 호출부가 보드 상태를
        # lock 밖에서 변형하지 못하게 함 (얕은 dict 복사는 내부 list 를 공유했었음)
        return [copy.deepcopy(u) for u in self._data.get("units", [])]

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    # ---- logs / directives ----
    async def log_event(self, who: str, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [{who}] {msg}\n"
        async with self._lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def recent_events(self, n: int = 20) -> str:
        # #20: 전체 events.log 를 읽지 않고 끝 청크만 seek-read 해 마지막 n 줄만 반환(대용량 방어).
        return "\n".join(_tail_lines(self.events_path, n))

    async def append_directive(self, who: str, text: str) -> None:
        # 대량/이상 LLM 출력이 directives.md 를 무한히 키우지 않도록 본문 크기 제한
        block = f"\n### {time.strftime('%H:%M:%S')} — {who}\n{_truncate_body(text)}\n"
        async with self._lock:
            with self.directives_path.open("a", encoding="utf-8") as f:
                f.write(block)

    def directives(self) -> str:
        # #21: directives 는 append 만 되고 매 역할 프롬프트에 통째로 다시 주입된다. 장기 run 에서
        # 파일이 커지면 모든 프롬프트가 동반 비대해진다. 끝에서 최대 _MAX_DIRECTIVES_BYTES 만
        # seek-read 해 크기를 묶는다(최신 디렉티브가 가장 중요하므로 tail 을 취한다).
        if not self.directives_path.exists():
            return ""
        try:
            with self.directives_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                start = max(0, size - _MAX_DIRECTIVES_BYTES)
                f.seek(start)
                chunk = f.read()
        except OSError:
            return ""
        text = chunk.decode("utf-8", errors="ignore")
        if start > 0:
            # 줄 중간에서 시작했을 수 있으니 첫 불완전 줄은 버리고, 생략 사실을 명시한다.
            nl = text.find("\n")
            text = text[nl + 1 :] if nl != -1 else ""
            text = "…(오래된 directives 생략)\n" + text
        return text
