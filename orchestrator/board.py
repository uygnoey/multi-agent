"""공유 보드: <project-dir>/.orchestrator/board.json 의 단일 writer.

오케스트레이터만 이 파일을 갱신한다. 역할 세션은 타깃 repo 파일을 편집하고
결과 JSON 만 남기며, 그 결과를 읽어 보드를 전이시키는 것은 오케스트레이터다.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from .config import normalize_role

_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")

# 제어문자(개행/탭/CR 포함): 아티팩트 경로에 끼면 문서 주입/표 왜곡을 일으키므로 제거 대상.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# 로그/디렉티브 본문 최대 길이: LLM 폭주 출력이 파일을 무한히 키우는 것을 방지.
_MAX_BODY_CHARS = 20000

# unit 텍스트/경로 필드 길이 상한 (#audit13): 런어웨이 입력(수만~수십만 자 title/description/
# artifact)이 board.json·report.md·API payload 를 부풀리지 않게 캡. 정상 콘텐츠는 보존하되
# 명백한 폭주만 잘라 '…(truncated)' 마커를 남긴다.
_MAX_TITLE_CHARS = 1000
_MAX_DESCRIPTION_CHARS = 8000
_MAX_ARTIFACT_CHARS = 1024


def _ts() -> str:
    """이벤트/디렉티브/에이전트 블록용 타임스탬프.

    예전에는 '%H:%M:%S' (시:분:초)만 써서 자정/장기 run 을 넘나들면 타임스탬프가
    비-단조(non-monotonic)로 보였다. 날짜를 포함(ISO 8601, '%Y-%m-%d %H:%M:%S')해
    여러 날에 걸친 로그도 정렬·진단이 가능하게 한다.
    """
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _truncate_body(text: str) -> str:
    """본문을 _MAX_BODY_CHARS 로 잘라 '…(truncated)' 마커를 붙임 (runaway 파일 증가 방지)."""
    s = str(text)
    if len(s) <= _MAX_BODY_CHARS:
        return s
    return s[:_MAX_BODY_CHARS] + "\n…(truncated)"


def _cap_text(value, limit: int) -> str:
    """unit title/description 텍스트 필드를 limit 자로 캡 (#audit13). 비-str 은 str() 방어."""
    s = str(value)
    if len(s) <= limit:
        return s
    return s[:limit] + "…(truncated)"


def _coerce_finite_float(raw) -> float:
    """비-숫자/NaN/Inf 는 0.0 으로 변환 (잘못된 비용 메타데이터 방어)."""
    # #audit17(N3): bool 은 int 의 서브클래스라 float(True)==1.0 이 된다. _safe_report_num 과
    # 동일하게 명시적으로 거부 — cost_add=True 같은 오입력이 $1.00 을 누적하지 않게 한다.
    if isinstance(raw, bool):
        return 0.0
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return val if math.isfinite(val) else 0.0


def _json_key_safe(key):
    # #audit17(N4): 비-str 키를 모두 str 로 정규화한다. 예전엔 int/bool/float 키를 그대로 두어,
    # 1 과 "1" 같은 int-vs-str 충돌이 _json_safe 의 dict 컴프리헨션에서 별개 키로 남고 json.dumps
    # 가 중복 키({"1":..,"1":..})를 뱉어 브라우저 JSON.parse 가 한 값을 조용히 잃었다. 모두
    # str 로 만들면 컴프리헨션이 충돌을 last-wins 로 합쳐 항상 유일 키만 직렬화된다.
    return key if isinstance(key, str) else str(key)


def _json_safe(obj, _seen: set[int] | None = None):
    # #RA-nan: NaN/Infinity 는 표준 JSON 토큰이 아니어서 JS JSON.parse(webui)가 깨진다.
    # dict/list/tuple/set 를 재귀 순회하며 비-유한(float NaN/±Inf)을 치환한다. 순환참조는
    # 문자열 마커로 끊어, 폴백 직렬화가 writer 를 죽이지 않게 한다.
    if _seen is None:
        _seen = set()
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else 0.0
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    oid = id(obj)
    # #audit17(N4): set 은 재귀 대상에서 제외해 leaf 로 둔다 → 호출부 default=str 가 "{...}"
    # 문자열로 격하한다(set→str 의미 보존, 기존 동작/테스트와 일치). dict/list/tuple 만 재귀해
    # 내부 NaN/순환/키를 살균한다(set 원소는 hashable 이라 set 자체를 품지 못해 순환 위험 없음).
    if isinstance(obj, (dict, list, tuple)):
        if oid in _seen:
            return "<cycle>"
        _seen.add(oid)
        try:
            if isinstance(obj, dict):
                return {_json_key_safe(k): _json_safe(v, _seen) for k, v in obj.items()}
            return [_json_safe(v, _seen) for v in obj]
        finally:
            _seen.discard(oid)
    return obj


def _dumps_safe(data, **kw) -> str:
    # #audit17(N4): 항상 _json_safe 로 먼저 살균한 뒤 직렬화한다. 예전엔 json.dumps 를 직접
    # 시도하고 예외 때만 폴백했는데, 비-str 키(예: int 1 과 str "1")는 json.dumps 가 예외 없이
    # 둘 다 "1" 로 직렬화해 '중복 키'를 뱉었다(브라우저 JSON.parse 가 한 값을 조용히 유실).
    # _json_safe 가 모든 키를 str 로 정규화하고 dict 컴프리헨션이 충돌을 last-wins 로 합쳐 항상
    # 유일 키만 남긴다. NaN/Inf→0, 순환→마커도 함께 처리. set/bytes 등 비-직렬화 leaf 값은
    # 그대로 두고 호출부의 default=str 가 격하한다(set→"{...}" 문자열 의미 보존). 비용은 작은
    # board 재구성으로 _flush 의 fsync×2 에 비하면 무시할 수준.
    return json.dumps(_json_safe(data), ensure_ascii=False, allow_nan=False, **kw)


def _coerce_int(raw) -> int:
    """비-정수 입력은 0 으로 변환 (잘못된 토큰 메타데이터 방어)."""
    if isinstance(raw, bool):
        return 0
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
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():  # 예: C:\... 형태의 드라이브 절대경로
        return None
    # 경로 traversal('..') 토큰 차단 (제어문자 제거 후 재검사)
    parts = re.split(r"[\\/]+", s)
    if ".." in parts:
        return None
    # #audit13: 런어웨이 길이의 '경로'가 board.json/report 를 부풀리지 않게 캡.
    if len(s) > _MAX_ARTIFACT_CHARS:
        s = s[:_MAX_ARTIFACT_CHARS]
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


def _path_under_root(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == root or root in resolved.parents


def _guard_managed_project_path(project_dir: Path, path: Path, label: str) -> None:
    """Reject symlink escapes before Board writes managed project artifacts."""

    if os.environ.get("ORCH_ALLOW_UNSAFE_PROJECT_DIR") == "1":
        return
    root = project_dir.resolve()
    try:
        rel = path.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError(f"{label} path escapes project dir: {path}") from exc

    cur = project_dir
    for part in rel.parts:
        cur = cur / part
        try:
            is_link = cur.is_symlink()
        except OSError as exc:
            raise ValueError(f"cannot verify symlink status for {cur}") from exc
        if is_link:
            raise ValueError(f"{label} path is a symlink: {cur}")
        if cur.exists() and not _path_under_root(cur, root):
            raise ValueError(f"{label} path escapes project dir: {cur.resolve()}")


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

# #audit13: set_status 는 상태 머신에 정의된 값만 허용한다. 오타("doen")나 손상된 호출이
# 임의 문자열을 board/report/UI 로 전파해 TERMINAL_OK 판정을 영구 오분류하는 것을 막는다.
_VALID_STATUSES = frozenset(
    {TODO, DESIGNING, DESIGNED, IN_PROGRESS, DEV_DONE, TESTING, TESTED, DONE, BLOCKED, FAILED}
)


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
        # 단일 초장문 라인처럼 청크 안에 개행이 전혀 없으면 내용을 통째로 버리지 말고
        # tail segment 를 보존한다. 최신 지시/로그가 한 줄로 길게 쌓인 경우 빈 결과가 되는
        # 것보다 불완전한 tail 이라도 보여주는 편이 복구/진단에 유용하다.
        if nl != -1:
            text = text[nl + 1 :]
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
        # events.log / directives.md 의 append-only 쓰기 전용 락. 무거운 _flush(fsync x2)를
        # 잡는 self._lock 과 분리해, 로그/디렉티브 기록이 보드 변형과 직렬화 경합하지 않게 한다.
        self._log_lock = asyncio.Lock()
        # #RA-loglock: per-agent .log 추가쓰기 전용 동기 락. _append_agent_log/write_agent_block 은
        # SYNC 메서드인데 runner 가 async 코루틴에서 await 없이 호출한다(runner.py:262/268).
        # asyncio.Lock 은 동기 컨텍스트에서 못 잡으므로, 동일 agent 로그를 동시에 쓰는
        # 코루틴/스레드의 줄 인터리빙을 막기 위해 별도 threading.Lock 으로 직렬화한다.
        self._agent_log_lock = threading.Lock()
        self._data: dict[str, Any] = {"units": [], "agents": {}}
        self.spec_text: str = ""

    # ---- persistence ----
    def _flush(self) -> None:
        # board.json 은 단일 진실원본(single source of truth)이다. temp→rename 의 원자성만으로는
        # 부족하다: write 후 fsync 없이 크래시/정전이 나면 atomic rename 에도 불구하고 board.json 이
        # 0바이트/부분 파일로 남아 영속성 공백(durability gap)이 생긴다. 따라서:
        #   1) tmp 를 실제 파일핸들로 열어 본문을 쓰고 flush()+os.fsync(fd) 로 디스크에 강제 반영,
        #   2) 그 다음 atomic 하게 replace,
        #   3) best-effort 로 상위 디렉터리 fd 도 fsync 해 rename 메타데이터를 영속화한다.
        # default=str: set/bytes/커스텀 객체 같은 비-직렬화 값(예: 살균되지 않은
        # title/description/stack/message)이 _data 에 들어와도 TypeError 로 단일 writer 를
        # 죽이지 않고 문자열로 격하시킨다. (안 그러면 in-memory 상태는 변형됐는데 영속화는 실패해
        # 디버전스가 나고 이후 모든 변형도 실패한다.)
        tmp = self.path.with_suffix(".json.tmp")
        # #RA-nan: allow_nan=False + NaN/Inf 폴백 살균을 거쳐 board.json 에 표준-비준수
        # NaN/Infinity 토큰이 새지 않게 한다(webui 의 JSON.parse 보호).
        payload = _dumps_safe(self._data, indent=2, default=str)
        # #audit20: write/fsync 도중 예외(ENOSPC/EIO 등)가 나면 stale board.json.tmp 가 남는다.
        # _atomic_write_text 와 동일하게 어떤 예외든 tmp 를 정리한 뒤 재던진다(replace 는 write 가
        # 끝난 뒤에만 실행되므로 board.json 본체는 손상되지 않지만, tmp 잔존은 정리한다).
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())  # tmp 본문을 디스크에 강제 반영 (rename 전 내구성 확보)
            tmp.replace(self.path)  # 원자적 교체 유지
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        # rename(디렉터리 엔트리 변경)을 영속화하려면 상위 디렉터리도 fsync 해야 한다. 단,
        # 일부 플랫폼/파일시스템(예: Windows)은 디렉터리 fd-fsync 를 지원하지 않으므로 best-effort.
        try:
            dir_fd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # 디렉터리 fsync 미지원 플랫폼/디렉터리는 조용히 건너뛴다 (atomic rename 자체는 유지됨).
            pass

    @staticmethod
    def _atomic_write_text(path: Path, text: str) -> None:
        """report.md / DELIVERABLES*.md 를 원자적으로 기록 (temp→os.replace).

        plain write_text 는 비-원자적이라 크래시/정전 시 부분 파일이 남을 수 있다. _flush 와
        동일하게 같은 디렉터리에 tmp 로 쓰고 flush()+fsync 후 os.replace 로 교체한다.
        """
        tmp = path.with_name(path.name + ".tmp")
        # #M07: 쓰기 도중 예외가 나면 stale .tmp 가 남는다. 어떤 예외든 tmp 를 정리한 뒤
        # 재던지고, replace 성공 후에는 _flush 와 동일하게 상위 디렉터리도 fsync 해
        # rename 메타데이터를 영속화한다 (best-effort, 미지원 플랫폼은 건너뜀).
        try:
            with tmp.open("w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())  # tmp 본문을 디스크에 강제 반영 (rename 전 내구성 확보)
            os.replace(tmp, path)  # 원자적 교체
        except BaseException:
            tmp.unlink(missing_ok=True)  # stale tmp 제거 후 재던짐
            raise
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # 디렉터리 fsync 미지원 플랫폼/디렉터리는 조용히 건너뛴다 (atomic rename 자체는 유지됨).
            pass

    async def init(self, spec_text: str, stack: dict) -> None:
        async with self._lock:
            self.orch_dir.mkdir(parents=True, exist_ok=True)
            self.results_dir.mkdir(parents=True, exist_ok=True)
            self.agents_dir.mkdir(parents=True, exist_ok=True)
            # #H08: 재사용 project-dir 에서 이전 run 의 per-agent 로그가 누적되지 않도록 run 시작 시
            # 비운다. 이후 backend tee 는 append 모드(PROMPT 블록·retry 로그 보존)로 쌓는다.
            for _log in self.agents_dir.glob("*.log"):
                try:
                    _log.unlink()
                except OSError:
                    pass
            self._data = {
                "created_at": time.time(),
                # #audit17(N5): 비-str spec_text(int/dict 등)면 [:2000] 슬라이스가 TypeError 로
                # init 을 반쪽 실패시켜 board.json 이 안 생겼다. str() 로 방어(입력 경로 일관).
                "spec_excerpt": str(spec_text)[:2000],
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
        # 락 안에서는 add_warning(동일 락 재획득) 을 호출할 수 없으므로 경고를
        # 모아 두었다가 락 해제 후 기록한다.
        collision_warnings: list[str] = []
        skipped_warnings: list[str] = []
        async with self._lock:
            # 외부에서 board.json 이 손상돼 id 가 없는 unit 이 끼어 있어도 KeyError 로
            # 단일 writer 가 죽지 않게 .get() 으로 안전하게 수집(손상 엔트리는 건너뜀).
            existing = {uid for u in self._data["units"] if (uid := u.get("id"))}
            # 이번 호출에서 본 raw id(문자열화) 집합: 동일 raw 의 재투입은 진짜 중복 → skip.
            # str() 로 키를 만들어 dict/list/숫자 같은 비정상 입력에도 해시 안전하다.
            seen_raw: set[str] = set()
            # id_map: dep 해석용 raw/canonical id → '최종' id 매핑. 기존 unit 은 항등 매핑 seed.
            # (충돌-rename 으로 둘째 unit 이 U-1-2 가 돼도, 그 unit 을 가리키는 dep 가 첫 unit
            #  (U-1)에 잘못 묶이지 않도록 raw id 까지 최종 id 로 정확히 remap.)
            id_map: dict[str, str] = {e: e for e in existing}
            # #audit19(P4): 각 최종 uid 를 *만든 raw_key* 를 기록한다. "이미 존재하는 uid" 가
            # 같은 raw 의 멱등 재투입인지(skip) vs 다른 raw 가 sanitize 되며 충돌한 것인지(rename)
            # 를 구분하기 위함. 기존 unit 은 canonical(자기 자신)이 origin 이라고 seed.
            uid_origin: dict[str, str] = {e: e for e in existing}
            accepted: list[tuple[str, dict]] = []  # 1차에서 확정된 (최종 id, 원본 unit)
            for u in units:
                if not isinstance(u, dict):
                    skipped_warnings.append(f"unit skipped: non-object unit {u!r}")
                    continue
                raw_id = u.get("id")
                raw_key = str(raw_id)
                # 동일한 raw id 가 이번 호출에서 또 나오면 진짜 중복 → skip.
                if raw_key in seen_raw:
                    skipped_warnings.append(f"unit skipped: duplicate raw id {raw_id!r}")
                    continue
                seen_raw.add(raw_key)
                # 숫자 ID 문자열화 + 경로/식별자 안전 문자만 (traversal·특수문자 차단)
                uid = _safe_unit_id(raw_id)
                if not uid:
                    skipped_warnings.append(f"unit skipped: invalid id {raw_id!r}")
                    continue
                if uid in existing:
                    if uid_origin.get(uid) == raw_key:
                        # 같은 raw id 가 만든 동일 unit 의 멱등 재투입 → 중복 생성 없이 skip
                        # (원인은 보드 경고로 가시화). #audit19(P4): 예전엔 raw_key==uid 만 봐서
                        # 'a/b'→'a-b' 가 먼저 들어오면 뒤따르는 raw 'a-b'(다른 unit)를 잘못
                        # 멱등으로 보고 유실했다. 이제 origin(만든 raw)이 같을 때만 멱등 처리.
                        skipped_warnings.append(f"unit skipped: duplicate id {raw_id!r}")
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
                uid_origin[uid] = raw_key  # #audit19(P4): 이 최종 uid 를 만든 raw 기록
                added += 1
                # raw id·최종 id 둘 다로 이 unit 을 찾게 한다(canonical 키는 첫 소유자 우선).
                id_map.setdefault(raw_key, uid)
                id_map.setdefault(uid, uid)
                accepted.append((uid, u))
            # 2차: 모든 최종 id 가 확정된 뒤 deps 를 해석한다(앞/뒤 순서·충돌-rename 무관하게 정확).
            for uid, u in accepted:
                # deps/roles 정규화: list→문자열들, scalar→[scalar], dict/None→[] (이상값 방어).
                # dep 해석 우선순위: ① raw/최종 id 정확 매치 → ② canonical(_safe_unit_id) 매치 →
                # ③ 알 수 없는 dep 는 기존처럼 _safe_unit_id 결과로 보존. 빈 항목은 drop.
                deps = [
                    r
                    for r in (
                        id_map.get(str(x)) or id_map.get(_safe_unit_id(x)) or _safe_unit_id(x)
                        for x in _norm_str_list(u.get("deps"))
                    )
                    if r
                ]
                roles_raw = _norm_str_list(u.get("roles"))
                self._data["units"].append(
                    {
                        "id": uid,
                        # #audit13: 런어웨이 길이 title/description 캡 (board.json 부풀림 방지)
                        "title": _cap_text(u.get("title", uid), _MAX_TITLE_CHARS),
                        "description": _cap_text(u.get("description", ""), _MAX_DESCRIPTION_CHARS),
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
        # 충돌/skip 은 락 밖에서 보드 경고로 기록(가시성 확보).
        for msg in [*collision_warnings, *skipped_warnings]:
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
        # #audit13: 상태 머신에 없는 값은 적용하지 않고 경고만 남긴다(거짓 전이/오분류 방지).
        if status not in _VALID_STATUSES:
            await self.log_event(
                unit_id, f"WARNING: invalid status {status!r} rejected (not applied)"
            )
            return
        matched = False
        async with self._lock:
            for u in self._data["units"]:
                # 손상된 보드(id/notes 누락)에서도 KeyError 로 죽지 않게 .get() 사용
                if u.get("id") == unit_id:
                    u["status"] = status
                    if note:
                        u.setdefault("notes", []).append(note)
                    matched = True
                    break  # id 는 유일하므로 매칭 후 즉시 종료
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
                # 손상된 보드(id/artifacts 누락)에서도 KeyError 로 죽지 않게 .get()/setdefault 사용
                if u.get("id") == unit_id:
                    matched = True
                    arts = u.setdefault("artifacts", [])
                    for a in clean:
                        if a not in arts:
                            arts.append(a)
                    break  # id 는 유일하므로 매칭 후 즉시 종료
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
                # 손상된 보드(id 누락)에서도 KeyError 로 죽지 않게 .get() 사용
                if u.get("id") == unit_id:
                    u["test_status"] = test_status
                    matched = True
                    break  # id 는 유일하므로 매칭 후 즉시 종료
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
        # #audit18(A8): _coerce_finite_float 로 정규화한다 — bool/NaN/Inf/비-숫자는 0.0.
        # 예전 bare float(amount) 는 float(True)==1.0 이라 add_cost(True) 가 $1.00 을 누적했다
        # (agent_update 경로는 audit17 N3 로 막혔는데 총비용 누적기만 누락됐다). 비용은 누적만
        # 가능하므로 음수/0(=정규화 실패 포함)은 no-op.
        val = _coerce_finite_float(amount)
        if val <= 0:
            return
        async with self._lock:
            # 기존 저장값이 손상돼 비-숫자(문자열 등)면 TypeError 가 나므로 누적 전에 강제 변환
            cur = _coerce_finite_float(self._data.get("total_cost_usd", 0.0))
            self._data["total_cost_usd"] = cur + val
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
            # #L11: current_unit 은 unit 이 실제로 주어졌을 때만 갱신한다. status=="running"
            # 인데 unit 이 None 인 갱신(예: cost/메시지만 보내는 호출)이 진행 중인 unit 을
            # 지우지 않도록 한다. 비우는 것은 terminal(non-running) 상태일 때만.
            if unit is not None:
                a["current_unit"] = unit
            elif status is not None and status != "running":
                a["current_unit"] = None
            if backend is not None:
                a["backend"] = backend
            if model is not None:
                a["model"] = model
            if cost_add:
                # 잘못된 비용 메타데이터(비-숫자/NaN/Inf)가 업데이트를 깨지 않도록 방어
                add = _coerce_finite_float(cost_add)
                # per-agent 비용도 누적만 가능 → 음수는 무시(비용이 감소하지 않게)
                if add > 0:
                    # 기존 저장값이 손상돼 비-숫자면 TypeError → 누적 전에 강제 변환
                    a["cost_usd"] = _coerce_finite_float(a.get("cost_usd", 0.0)) + add
            if cost_est:
                a["cost_est"] = True
                self._data["cost_estimated"] = True
            if tokens_add:
                # 잘못된 토큰 메타데이터(비-정수 등)는 0 으로 강제(int 코어션 가드 유지)
                add_t = _coerce_int(tokens_add)
                # 토큰도 비용처럼 누적만 가능 → 음수는 no-op(per-agent/total 둘 다 감소 금지)
                if add_t > 0:
                    # 기존 저장값이 손상돼 비-정수(문자열 등)면 TypeError → 누적 전에 강제 변환
                    a["tokens"] = _coerce_int(a.get("tokens", 0)) + add_t
                    self._data["total_tokens"] = (
                        _coerce_int(self._data.get("total_tokens", 0)) + add_t
                    )
            if message is not None:
                a["last_message"] = str(message)[:500]
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
        # #RA-loglock: 동기 threading 락으로 동시 추가쓰기의 줄 인터리빙을 차단
        with self._agent_log_lock:
            with self._log_path(role).open("a", encoding="utf-8") as f:
                # 날짜 포함 타임스탬프(자정/장기 run 넘나듦에도 단조 정렬 가능)
                f.write(f"{_ts()} {text}\n")

    def write_agent_block(self, role: str, title: str, body: str) -> None:
        """프롬프트/결과 같은 상세 블록을 per-agent 로그에 기록 (실시간 상세 로그용)."""
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        bar = "─" * 56
        # 단일 프롬프트/결과가 로그를 폭증시키지 않도록 본문 크기 제한
        body = _truncate_body(body)
        # #RA-loglock: 동기 threading 락으로 동시 추가쓰기의 줄/블록 인터리빙을 차단
        with self._agent_log_lock:
            with self._log_path(role).open("a", encoding="utf-8") as f:
                # 날짜 포함 타임스탬프(자정/장기 run 넘나듦에도 단조 정렬 가능)
                f.write(f"\n{bar}\n{_ts()} {title}\n{bar}\n{body}\n")

    def agents(self) -> dict:
        # #N01: snapshot() 과 일관되게 ensure_ascii=False (한글 등 비-ASCII 를 이스케이프하지
        # 않아 round-trip 이 가볍고 동일 동작).
        # #RA-nan: snapshot() 과 동일하게 allow_nan=False 살균 후 round-trip (webui 보호).
        return json.loads(_dumps_safe(self._data.get("agents", {}), default=str))

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
        # 손상된 보드(status 누락)에서도 KeyError 로 리포트 작성이 깨지지 않게 .get() 사용
        done = sum(1 for u in units if u.get("status") in TERMINAL_OK)
        failed = [u for u in units if u.get("status") in (BLOCKED, FAILED)]
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
            # 손상된 보드(id/status 누락)에서도 KeyError 로 죽지 않게 .get() 사용
            lines.append(
                f"| {_md_cell(u.get('id', ''))} | {_md_cell(u.get('status'))} | "
                f"{_md_cell(u.get('test_status'))} | "
                f"{len(u.get('artifacts', []))} | {_md_cell(u.get('title', ''))} |"
            )
        report = self.orch_dir / "report.md"
        # report.md 도 board.json 과 동일하게 원자적(temp→os.replace)으로 기록
        self._atomic_write_text(report, "\n".join(lines) + "\n")
        return report

    def write_deliverables(self) -> list[str]:
        """보드 상태로 개발 산출물 문서를 사람이 읽는 4개 언어(EN/KO/JA/ES)로 생성 (fallback).

        사람이 보는 문서/가이드만 다국어로 만든다(#lang). docs-writer 백엔드가 이미
        docs/DELIVERABLES.md / .ko.md / .ja.md / .es.md 를 작성했으면 덮어쓰지 않고, 없는 언어만
        보드 요약으로 채워 fallback 을 보장한다. 실제로 존재하는 경로만 반환(스케줄러가 전역
        아티팩트로 추가).
        """
        d = self._data
        units = d.get("units", [])
        # 손상된 보드(status 누락)에서도 KeyError 로 산출물 문서 작성이 깨지지 않게 .get() 사용
        done = sum(1 for u in units if u.get("status") in TERMINAL_OK)
        artifacts = d.get("artifacts", [])
        docs_dir = self.project_dir / "docs"
        _guard_managed_project_path(self.project_dir, docs_dir, "docs deliverables")
        docs_dir.mkdir(parents=True, exist_ok=True)

        def table(headers):
            out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
            for u in units:
                # 손상된 보드(id/status 누락)에서도 KeyError 로 죽지 않게 .get() 사용
                out.append(
                    f"| {_md_cell(u.get('id', ''))} | {_md_cell(u.get('status'))} | "
                    f"{_md_cell(u.get('test_status'))} | "
                    f"{len(u.get('artifacts', []))} | {_md_cell(u.get('title', ''))} |"
                )
            return out

        def unit_files():
            out = []
            for u in units:
                if u.get("artifacts"):
                    # id/title 이스케이프: LLM 제공 값이 마크다운 구조를 깨지 못하게(id 누락 방어)
                    out.append(f"### {_md_cell(u.get('id', ''))} — {_md_cell(u.get('title', ''))}")
                    # 아티팩트도 최소한 개행을 중화해 문서 주입 방지
                    out += [f"- {_md_cell(a)}" for a in u.get("artifacts", [])]
                    out.append("")
            return out

        cost_str = f"${_safe_report_num(d.get('total_cost_usd', 0.0)):.4f}"

        def build(labels: dict) -> list[str]:
            return (
                [
                    labels["title"],
                    "",
                    f"- {labels['phase']}: **{d.get('phase')}**",
                    f"- {labels['done']}: **{done}/{len(units)}**",
                    f"- {labels['cost']}: **{cost_str}**",
                    f"- {labels['stack']}: {d.get('stack')}",
                    "",
                    f"## {labels['workunits']}",
                    "",
                ]
                + table(labels["headers"])
                + ["", f"## {labels['shared']}", ""]
                # 전역 산출물도 per-unit 과 동일하게 _md_cell 로 이스케이프(개행/파이프 주입 방지)
                + ([f"- {_md_cell(a)}" for a in artifacts] or [labels["none"]])
                + ["", f"## {labels['perunit']}", ""]
                + unit_files()
                + [labels["runguide"]]
            )

        # #lang: 사람이 읽는 문서만 4개 언어(EN/KO/JA/ES)로. AI-facing 텍스트는 영어 그대로.
        langs = [
            (
                "",
                {
                    "title": "# Development Deliverables",
                    "phase": "phase",
                    "done": "units done",
                    "cost": "total cost",
                    "stack": "stack",
                    "workunits": "Work units",
                    "headers": ["id", "status", "test", "files", "title"],
                    "shared": "Design & shared artifacts",
                    "none": "- (none)",
                    "perunit": "Per-unit files",
                    "runguide": "See `docs/RUN_GUIDE.md` for how to run.",
                },
            ),
            (
                ".ko",
                {
                    "title": "# 개발 산출물",
                    "phase": "단계",
                    "done": "완료 unit",
                    "cost": "총비용",
                    "stack": "스택",
                    "workunits": "작업 단위",
                    "headers": ["id", "상태", "테스트", "파일수", "제목"],
                    "shared": "설계·공통 산출물",
                    "none": "- (없음)",
                    "perunit": "단위별 파일",
                    "runguide": "실행 방법은 `docs/RUN_GUIDE.ko.md` 참고.",
                },
            ),
            (
                ".ja",
                {
                    "title": "# 開発成果物",
                    "phase": "フェーズ",
                    "done": "完了ユニット",
                    "cost": "総コスト",
                    "stack": "スタック",
                    "workunits": "作業ユニット",
                    "headers": ["id", "状態", "テスト", "ファイル数", "タイトル"],
                    "shared": "設計・共有成果物",
                    "none": "- (なし)",
                    "perunit": "ユニット別ファイル",
                    "runguide": "実行方法は `docs/RUN_GUIDE.ja.md` を参照。",
                },
            ),
            (
                ".es",
                {
                    "title": "# Entregables de desarrollo",
                    "phase": "fase",
                    "done": "unidades completadas",
                    "cost": "costo total",
                    "stack": "stack",
                    "workunits": "Unidades de trabajo",
                    "headers": ["id", "estado", "prueba", "archivos", "título"],
                    "shared": "Diseño y artefactos compartidos",
                    "none": "- (ninguno)",
                    "perunit": "Archivos por unidad",
                    "runguide": "Consulte `docs/RUN_GUIDE.es.md` para ejecutar.",
                },
            ),
        ]
        # 에이전트가 작성한 산출물은 덮어쓰지 않고, 없는 언어만 보드 요약으로 채운다.
        written: list[str] = []
        for suffix, labels in langs:
            path = docs_dir / f"DELIVERABLES{suffix}.md"
            _guard_managed_project_path(self.project_dir, path, "docs deliverables")
            if not path.exists():
                # board.json 과 동일하게 원자적(temp→os.replace)으로 기록(부분 파일 방지)
                self._atomic_write_text(path, "\n".join(build(labels)) + "\n")
            if path.exists():
                written.append(f"docs/DELIVERABLES{suffix}.md")
        return written

    # ---- reads (best-effort snapshots) ----
    def units(self) -> list[dict]:
        # 중첩 리스트(artifacts/deps/roles/notes)까지 깊은 복사해 호출부가 보드 상태를
        # lock 밖에서 변형하지 못하게 함 (얕은 dict 복사는 내부 list 를 공유했었음)
        return [copy.deepcopy(u) for u in self._data.get("units", [])]

    def snapshot(self) -> dict:
        # #RA-nan: allow_nan=False + NaN/Inf 살균을 거쳐 webui 가 NaN 토큰을 받지 않게 한다.
        return json.loads(_dumps_safe(self._data, default=str))

    # ---- logs / directives ----
    async def log_event(self, who: str, msg: str) -> None:
        # 날짜 포함 타임스탬프(자정/장기 run 넘나듦에도 단조 정렬 가능)
        line = f"{_ts()} [{who}] {msg}\n"
        # 무거운 _flush 를 잡는 self._lock 과 분리된 _log_lock 사용(append 경합 제거)
        async with self._log_lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def recent_events(self, n: int = 20) -> str:
        # #20: 전체 events.log 를 읽지 않고 끝 청크만 seek-read 해 마지막 n 줄만 반환(대용량 방어).
        return "\n".join(_tail_lines(self.events_path, n))

    async def append_directive(self, who: str, text: str) -> None:
        # 대량/이상 LLM 출력이 directives.md 를 무한히 키우지 않도록 본문 크기 제한
        # 날짜 포함 타임스탬프(자정/장기 run 넘나듦에도 단조 정렬 가능)
        block = f"\n### {_ts()} — {who}\n{_truncate_body(text)}\n"
        # 무거운 _flush 를 잡는 self._lock 과 분리된 _log_lock 사용(append 경합 제거)
        async with self._log_lock:
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
            # 줄 중간에서 시작했을 수 있으니 첫 불완전 줄은 버린다. 단, #L09: 청크 전체가
            # 개행 없는 단일 >limit 블록이면 _tail_lines 처럼 통째로 버리지 말고 보존한다(빈
            # 결과보다 불완전 tail 이 복구/진단에 유용).
            nl = text.find("\n")
            if nl != -1:
                text = text[nl + 1 :]
            text = "…(오래된 directives 생략)\n" + text
        return text
