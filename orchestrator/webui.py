"""웹 UI: 브라우저에서 기획서를 업로드해 실행하고, 멀티에이전트 진행을 실시간으로 본다.

TUI(monitor.py)와 동일한 데이터 소스(`<project-dir>/.orchestrator/board.json`,
`agents/<role>.log`)를 읽으므로 기능이 일치한다 — 에이전트 리스트 → 클릭 상세
(실시간 활동·비용) → 뒤로. 의존성 0(stdlib http.server). 기본 127.0.0.1 바인딩.

사용:
  python -m orchestrator.webui --port 8765 [--base-dir ~/agent-runs]
  python -m orchestrator --web --port 8765
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import procutil
from .backends import backend_status, resolve
from .board import _safe_unit_id, _tail_lines
from .config import BACKEND_INFO, FRAMEWORK_ROOT, ROLES, VALID_BACKENDS
from .monitor import _read_agent_log, _read_board

MAX_BODY_BYTES = 4 * 1024 * 1024  # 요청 바디 상한 (메모리 고갈 방지)
MAX_SPEC_BYTES = 1024 * 1024  # 기획서 텍스트 상한


def _token_equal(provided: str, expected: str) -> bool:
    # hmac.compare_digest 는 non-ascii str 을 거부한다(공백/특수문자는 OK 지만 한글 등은 TypeError).
    # 따라서 원문을 직접 비교하지 않고 UTF-8 바이트의 SHA-256 다이제스트를 상수시간 비교한다.
    # 다이제스트는 항상 같은 길이라 compare_digest 의 상수시간 보장이 유지되고, non-ascii 토큰도
    # 안전하게 인증된다(예전엔 non-ascii WEB_UI_TOKEN 이 무조건 인증 실패했음).
    if not provided or not expected:
        return False
    p = hashlib.sha256(provided.encode("utf-8")).digest()
    e = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(p, e)


def _read_events(orch_dir, n: int = 300) -> str:
    """events.log 의 최근 n 줄 (통합 로그 패널용).

    #20: 전체 파일을 읽지 않고 끝 청크만 seek-read 해 마지막 n 줄만 반환(대용량 로그 방어).
    """
    p = orch_dir / "events.log"
    if not p.exists():
        return ""
    return "\n".join(_tail_lines(p, n))


def _read_agent_logs(orch_dir, roles, n: int = 120) -> dict:
    """역할별 실시간 로그 tail {role: text} (활동 있는 역할만). 파일엔 전체가 저장됨.

    #34/#35: 폴링마다 역할별 600줄을 보내면 /api/state 페이로드가 과도하다.
    대시보드 카드 표시에 충분한 작은 tail(기본 120줄)만 보내고, 전체 로그는
    역할별 /api/agent 엔드포인트에서 조회한다.
    """
    out = {}
    ad = orch_dir / "agents"
    for role in roles:
        # #RA2: board._log_path 는 _safe_unit_id 로 정규화해 쓰므로(예: 공백/특수문자 →
        #       '-', 빈 결과는 '_unknown') 읽기도 동일 규칙을 써야 파일이 어긋나지 않는다.
        p = ad / f"{_safe_unit_id(role) or '_unknown'}.log"
        if not p.exists():
            continue
        # #20: 전체 파일을 읽지 않고 끝 청크만 seek-read 해 마지막 n 줄만 보낸다(대용량 방어).
        lines = _tail_lines(p, n)
        if lines:
            out[role] = "\n".join(lines)
    return out


def _read_pid(pf) -> int | None:
    """run.pid 에서 *양의 정수* PID 만 안전하게 읽는다 (손상/악성 pidfile 방어).

    HIGH(안전): 예전엔 int(read_text().strip()) 로 곧장 변환해 0/-1 같은 값도 그대로
    os.kill/os.getpgid/os.killpg 로 흘러갔다. os.kill(-1, sig) 는 호출자가 시그널을 보낼 수
    있는 *모든* 프로세스에 시그널을 보내고(0 은 호출자의 프로세스 그룹 전체), 이는 손상된
    pidfile 한 줄로 무관한 프로세스를 무더기로 죽이는 사고로 이어진다. 따라서 PID 가
    *양의 정수*(>0)일 때만 반환하고, 그 외(0/음수/비숫자/빈값/없음)는 None 을 돌려준다.

    pidfile 포맷: 첫 줄이 PID. 다른 소유자가 둘째 줄에 start-time 토큰을 덧붙일 수 있으므로
    (선택적) 추가 줄을 너그럽게 허용한다 — 단 강제하지는 않는다(첫 줄만 본다).
    """
    try:
        txt = pf.read_text(encoding="utf-8")
    except Exception:
        return None
    first = txt.strip().split("\n", 1)[0].strip()
    if not re.fullmatch(r"[+-]?\d+", first):
        return None
    try:
        pid = int(first)
    except (ValueError, OverflowError):
        return None
    return pid if pid > 0 else None


def _is_zombie(pid: int) -> bool:
    """pid 가 좀비(이미 종료, 부모가 reap 대기 중) 인지 best-effort 로 판별.

    Linux 는 /proc, 그 외(macOS 등)는 `ps` 로 상태 코드를 본다. 판별 불가 시 False.
    좀비 = 더 이상 실행 중이 아니므로 stop 입장에서는 종료로 취급한다.
    """
    try:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            # 형식: pid (comm) STATE ... — comm 에 ')' 가 있을 수 있어 마지막 ')' 기준.
            txt = stat.read_text(encoding="utf-8", errors="replace")
            idx = txt.rfind(")")
            # #L17: monitor._is_zombie_uncached 와 동일 가드 — ')' 없는 손상 stat 은
            # [-1+1:]=[0:] 오독을 막기 위해 좀비 아님(False) 처리한다.
            if idx == -1:
                return False
            state = txt[idx + 1 :].strip().split(" ", 1)[0]
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


def _run_alive(orch_dir) -> bool:
    """run.pid 의 프로세스가 살아있는지 (웹 서버 재시작/외부 실행에도 정확).

    semantics 는 캐시 없이 항상 즉시 판정한다 — 폴링 핫패스의 캐싱은 호출부
    (RunManager._run_alive_cached)에서만 적용한다(#6). 그래야 직접 호출하는 곳/테스트는
    monkeypatch(_is_zombie 등)의 효과를 즉시 본다.
    """
    pf = orch_dir / "run.pid"
    if not pf.exists():
        return False
    # HIGH(#1): int() 직접 변환 대신 _read_pid 로 *양의 정수* PID 만 받는다(0/-1 거부).
    pid = _read_pid(pf)
    if pid is None:
        return False
    # #M6: pid 가 재사용된 무관 프로세스면(시작시각 토큰 불일치) 우리 run 이 아니다 → not alive.
    if not procutil.pid_is_ours(pid, procutil.read_pid_token(pf)):
        return False
    try:
        os.kill(pid, 0)  # signal 0 = 존재/권한 확인 (죽었으면 OSError)
    except OSError:
        return False
    # #135: 좀비(종료됐지만 reap 안 됨)는 os.kill(pid,0) 이 계속 성공하므로 stop 의 _alive()
    #       판정과 어긋난다. is_running/_run_alive 와 stop cleanup 이 일치하도록 좀비는
    #       종료로 본다 (그렇지 않으면 프로세스가 끝났는데도 UI 가 계속 "running" 으로 남는다).
    return not _is_zombie(pid)


def slugify(name: str) -> str:
    # #137: name 이 str 이 아니면(예: 손상된 _run_opts.json 의 숫자/None) .strip() 이
    #       raise 하므로, 비문자열은 빈 문자열로 취급해 기본 "run" 으로 폴백한다.
    base = name.strip() if isinstance(name, str) else ""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-").lower()
    return s or "run"


def new_run_id(name: str) -> str:
    # 같은 초 충돌 방지를 위해 짧은 랜덤 suffix 추가
    return f"{slugify(name)}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _coerce_int(value, default: int) -> int:
    """#38: 손상된 _run_opts.json 의 숫자 옵션을 관대하게 정수로 변환.

    int()/float() 가 그대로 raise 하면 rerun 이 사용자에게 내부 예외 문자열을 노출한다
    (예: "invalid literal for int() with base 10: 'abc'"). 변환 불가/None/빈값이면
    default 로 폴백해, 손상된 옵션이 있어도 명령이 깨지지 않고 합리적으로 동작하게 한다.
    """
    if value in (None, ""):
        return default
    try:
        if isinstance(value, bool):
            return default
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value) if math.isfinite(value) and value.is_integer() else default
        if isinstance(value, str):
            s = value.strip()
            if re.fullmatch(r"[+-]?\d+", s):
                return int(s)
            try:
                f = float(s)
            except ValueError:
                return default
            if math.isfinite(f) and f.is_integer():
                return int(f)
            return default
        return default
    except (TypeError, ValueError, OverflowError):
        return default


def _coerce_float(value, default):
    """#38: 손상된 _run_opts.json 의 실수 옵션을 관대하게 변환 (변환 불가 시 default)."""
    if value in (None, ""):
        return default
    try:
        f = float(value)
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _parse_int_option(value, *, field: str, minimum: int) -> tuple[int | None, str | None]:
    """웹 API 정수 옵션 검증: JSON float truncation 없이 진짜 정수만 허용."""
    if value in (None, ""):
        return None, None
    if isinstance(value, bool):
        return None, f"invalid {field}: {value!r}"
    if isinstance(value, int):
        iv = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None, f"invalid {field}: {value!r}"
        iv = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if not re.fullmatch(r"[+-]?\d+", s):
            return None, f"invalid {field}: {value!r}"
        iv = int(s)
    else:
        return None, f"invalid {field}: {value!r}"
    if iv < minimum:
        return None, f"{field} must be >= {minimum} (got {iv})"
    return iv, None


def _fmt_num(value) -> str:
    """#12: 숫자를 CLI 인자 문자열로 포맷. 정수값은 ".0" 없이(600), 소수는 보존(1.5).

    poll_interval 은 float 이지만 정수값(600/30)을 "600.0" 처럼 보내면 보기에 지저분하고
    기존 동작과 어긋난다. 정수와 같은 값이면 int 로, 아니면 float repr 로 렌더한다.
    (CLI 는 --poll-interval 을 float 로 파싱하므로 어느 쪽이든 동일하게 받는다.)
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f.is_integer() else repr(f)


def build_command(py: str, spec_path: Path, project_dir: Path, opts: dict) -> list[str]:
    # #61/#136: poll-interval 은 opts 에서 읽되 웹 기본은 600(장기 감독 주기). 미지정 시 600.
    #   CLI RunConfig 기본(20초)과 의도적으로 다르다 — 웹 dogfood 는 감독 주기를 길게 둔다.
    #   이 divergence 가 조용하지 않도록 UI 폼에 poll-interval 입력칸과 라벨(웹 600/CLI 20)을
    #   노출했다. 사용자는 폼에서 직접 짧은 주기를 지정할 수 있다.
    # #12/#38: poll_interval 은 float (CLI/RunConfig 모두 float; 1.5 같은 소수 유효).
    #   예전엔 _coerce_int 로 강제 정수화해 1.5 가 1 로 깎였다 — 웹 검증이 float 를
    #   통과시켜도 명령엔 int 로 전달되는 불일치. _coerce_float 로 raw 값을 그대로 전달한다.
    #   손상된 _run_opts.json(예: poll_interval="abc")이 와도 raise 하지 않고 600 폴백.
    poll = _coerce_float(opts.get("poll_interval"), 600)
    cmd = [
        py,
        "-m",
        "orchestrator",
        "--spec",
        str(spec_path),
        "--project-dir",
        str(project_dir),
        "--backend",
        str(opts.get("backend", "mock")),
        "--concurrency",
        str(_coerce_int(opts.get("concurrency"), 3)),
        "--poll-interval",
        _fmt_num(poll),
    ]
    # #audit19(C5): 증분(기능 추가) 모드를 웹/API 에서도 트리거할 수 있게 --feature 를 전달한다.
    # 예전엔 build_command 가 항상 greenfield(--spec) 명령만 만들어 웹으로는 feature 모드 불가였다.
    # '=' 형식으로 넘겨 값이 '-' 로 시작하거나 개행을 포함해도 child argparse 가 플래그로 오인하지
    # 않게 한다(--spec 은 그대로 추가 컨텍스트로 함께 전달).
    feature = opts.get("feature")
    if isinstance(feature, str) and feature.strip():
        cmd.append(f"--feature={feature}")
    backends = opts.get("backends")
    if backends:
        cmd += ["--backends", ",".join(backends) if isinstance(backends, list) else str(backends)]
    for role, prov in (opts.get("role_backends") or {}).items():
        if not prov:
            continue
        # #100/#101: prov 가 우선순위 리스트면 콤마로 합쳐 CLI 의 ROLE=B1,B2 형식으로.
        #            (리스트 repr 이 그대로 한 백엔드 문자열로 넘어가는 버그 방지)
        prov_str = ",".join(str(p) for p in prov if p) if isinstance(prov, list) else str(prov)
        if prov_str:
            cmd += ["--role-backend", f"{role}={prov_str}"]
    if opts.get("distribute"):
        cmd.append("--distribute")
    if opts.get("cross_check"):
        cmd.append("--cross-check")
    if opts.get("mock"):
        cmd.append("--mock")
    if opts.get("delegate"):
        cmd.append("--delegate")
    if opts.get("full_access"):
        cmd.append("--full-access")
    if opts.get("auto_commit") is False:
        cmd.append("--no-auto-commit")
    completion_level = str(opts.get("completion_level") or "mvp").strip().lower()
    if completion_level in ("mvp", "production"):
        cmd += ["--completion-level", completion_level]
    # #38: max_units/max_attempts 도 손상값을 관대하게 변환. max_units 는 0/음수면 "전체"로
    #      간주해 플래그를 생략(폴백 0). max_attempts 는 0이면 "고쳐질 때까지" 기본값.
    # NIT(#10): 같은 변환을 두 번 계산하지 않도록 한 번만 구해 변수에 담는다.
    max_units = _coerce_int(opts.get("max_units"), 0)
    if max_units > 0:
        cmd += ["--max-units", str(max_units)]
    if opts.get("max_attempts") not in (None, ""):
        cmd += ["--max-attempts", str(_coerce_int(opts.get("max_attempts"), 0))]
    # #62: timeout/retries/budget/model 도 있으면 CLI 로 전달 (웹 실행에서도 사용 가능).
    # #38: float()/int() 가 손상값에 raise 하지 않도록 관대하게 변환(변환 불가 시 옵션 생략).
    if opts.get("timeout") not in (None, ""):
        tv = _coerce_float(opts.get("timeout"), None)
        if tv is not None:
            cmd += ["--timeout", str(tv)]
    if opts.get("retries") not in (None, ""):
        cmd += ["--retries", str(_coerce_int(opts.get("retries"), 0))]
    if opts.get("budget") not in (None, ""):
        bv = _coerce_float(opts.get("budget"), None)
        if bv is not None:
            cmd += ["--budget", str(bv)]
    if opts.get("model"):
        cmd += ["--model", str(opts["model"])]
    return cmd


def sanitize_run_opts(opts: dict) -> dict:
    """MEDIUM(#3): rerun 경로가 저장된 _run_opts.json 을 build_command 에 넘기기 전에
    /api/run 과 같은 타입 보장으로 정리한다.

    /api/run 은 backends=list[str], role_backends=dict[str, str|list[str]],
    completion_level∈{mvp,production} 등을 검증한다. rerun 은 이 검증을 거치지 않고 저장
    파일을 그대로 start() 로 넘겨, 손상/수기편집된 opts(예: backends 가 dict, role_backends
    가 list)가 build_command 안에서 .items()/",".join 등으로 불투명하게 깨졌다.

    /api 처럼 *거부(400)* 하는 대신 여기서는 *정규화* 한다 — rerun 은 사용자가 폼을 다시
    채울 기회 없이 한 번에 재실행하는 경로라, 합리적으로 복구 가능한 값은 기본값으로
    수렴시켜 명령이 깨지지 않게 한다(기존 _coerce_* 의 관대한 정책과 일관).
    """
    if not isinstance(opts, dict):
        return {"mock": True}
    out = dict(opts)

    # #audit19(P3): 백엔드 값을 타입뿐 아니라 *유효성*까지 검증·정규화한다. /api/run 은
    # resolve()∈VALID_BACKENDS 로 검증하지만 rerun(이 경로)은 안 했다. 손상/수기편집된
    # _run_opts.json 의 backend="--mock" 같은 값이 그대로 build_command 로 흘러 child argparse 를
    # 깨뜨릴 수 있었다. resolve 해서 valid 면 canonical 이름으로 정규화, 아니면 드롭.
    def _valid_backend(b):
        if not isinstance(b, str) or not b:
            return None
        try:
            r = resolve(b)
        except Exception:
            return None
        return r if r in VALID_BACKENDS else None

    # backend: 유효 백엔드만, canonical 이름으로. 그 외 → "mock".
    out["backend"] = _valid_backend(out.get("backend", "mock")) or "mock"

    # backends: list[유효 canonical] 만. 문자열이면 콤마 분해, 무효/비문자열은 제거.
    backends = out.get("backends")
    if isinstance(backends, str):
        backends = [b.strip() for b in backends.split(",") if b.strip()]
    if isinstance(backends, list):
        backends = [r for b in backends if (r := _valid_backend(b))]
        out["backends"] = backends or None
    elif backends is not None:
        out["backends"] = None

    # role_backends: dict 만 허용. 값은 유효 canonical str 또는 list[str], 알 수 없는 역할은 버린다.
    rb = out.get("role_backends")
    if isinstance(rb, dict):
        clean_rb = {}
        for role, prov in rb.items():
            if role not in ROLES:
                continue
            if isinstance(prov, str):
                if r := _valid_backend(prov):
                    clean_rb[role] = r
            elif isinstance(prov, list):
                provs = [r for p in prov if (r := _valid_backend(p))]
                if provs:
                    clean_rb[role] = provs
            # 그 외(dict/숫자/None)는 버린다.
        out["role_backends"] = clean_rb
    elif rb is not None:
        out["role_backends"] = None

    # completion_level: 화이트리스트. 그 외/손상 → mvp.
    level = str(out.get("completion_level") or "mvp").strip().lower()
    out["completion_level"] = level if level in ("mvp", "production") else "mvp"

    # name: str 또는 None 만 (slugify 가 비문자열도 폴백하지만 명시적으로 정규화).
    name = out.get("name")
    if name is not None and not isinstance(name, str):
        out["name"] = None

    # #M03: model 은 str 만, 길이 상한 + '-' 시작 거부('--flag' 로 오인돼 argv 가 오염되는 것 방지).
    #       그 외(비문자열/과길이/대시 시작) → None(백엔드 기본 모델).
    model = out.get("model")
    if model is not None and (
        not isinstance(model, str) or len(model) > 200 or model.startswith("-")
    ):
        out["model"] = None

    # #M08: 저장된 rerun opts 의 숫자 범위를 클램프한다. 음수가 build_command 를 거쳐
    #       `--concurrency -5` 처럼 스폰 프로세스의 argparse 검증 에러로 재실행을 깨지 않게 한다
    #       (concurrency≥1, retries/max_attempts/max_units≥0; max_units 0 은 build_command 가 생략).
    # #RA-numclamp: 하한만이 아니라 상한도 클램프한다 — concurrency=10**9 같은 손상값이
    #       스폰 프로세스에서 스레드 폭증/OOM 을 일으키지 않게 RunConfig 상한과 맞춘다
    #       (concurrency≤64; max_attempts/retries/max_units 는 1000 으로 sane cap).
    for fld, lo, hi in (
        ("concurrency", 1, 64),
        ("max_units", 0, 1000),
        ("max_attempts", 0, 1000),
        ("retries", 0, 1000),
    ):
        if fld in out:
            iv = _coerce_int(out.get(fld), lo)
            out[fld] = min(hi, max(lo, iv))

    # #RA-numclamp: poll_interval/timeout/budget 실수 옵션도 정리한다. /api/run 의 검증
    #       (음수/비유한값 거부)을 거치지 않는 rerun 경로라, 손상된 _run_opts.json 이
    #       `--budget -2`/`--timeout -1`/`--poll-interval inf` 처럼 스폰 프로세스의 검증
    #       에러로 재실행을 깨뜨리지 않게 한다. poll_interval 은 0 으로 폴백(RunConfig 가
    #       안전 하한 클램프), timeout/budget 은 키 자체를 제거(build_command 가 생략).
    pi = out.get("poll_interval")
    if pi not in (None, ""):
        fv = _coerce_float(pi, None)
        out["poll_interval"] = fv if (fv is not None and fv >= 0) else 0
    for fld in ("timeout", "budget"):
        if fld in out and out.get(fld) not in (None, ""):
            fv = _coerce_float(out.get(fld), None)
            if fv is None or fv < 0:
                out.pop(fld, None)
            else:
                out[fld] = fv
    return out


def list_runs(base_dir: Path) -> list[dict]:
    out = []
    if base_dir.exists():
        base = base_dir.resolve()
        for d in sorted(base_dir.iterdir(), reverse=True):
            if not (d / ".orchestrator" / "board.json").exists():
                continue
            # project_dir() 가 거부할 항목(심볼릭/외부 경로)은 목록에서 제외 (#81)
            try:
                resolved = (base / d.name).resolve()
            except Exception:
                continue
            if resolved == base or base not in resolved.parents:
                continue
            out.append({"id": d.name, "project_dir": str(d)})
    return out


class RunManager:
    """기획서 텍스트로 오케스트레이터 서브프로세스를 띄우고 추적."""

    # LOW(#6): /api/runs(3초)·/api/state(1초) 폴링마다 run 별로 os.kill + `ps` 좀비 검사를
    #   동기 수행하면 run 수에 비례해 blocking I/O 가 늘어난다(특히 macOS 의 ps 서브프로세스).
    #   monitor 의 _LOG_CACHE 와 같은 결로, run.pid 의 (mtime,size) 기준 liveness 결과를 짧은
    #   TTL 동안 캐시해 폴링 부하를 상수에 가깝게 묶는다. TTL 이 짧아(0.8초) UI 갱신 주기보다
    #   작으므로 stop/완료 직후의 상태 전이는 그대로 보인다(관찰 가능한 의미 불변).
    _ALIVE_TTL = 0.8  # 초
    _ALIVE_CACHE_MAX = 256

    def __init__(self, base_dir: Path, spawn=None, max_running: int = 16):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._spawn = spawn or self._default_spawn
        self.max_running = max(1, int(max_running or 16))
        self._procs: dict[str, object] = {}
        self._lock = threading.Lock()
        # #6: liveness 캐시 — {key: (deadline, alive)}. key 는 pidfile 경로+mtime+size.
        self._alive_cache: dict[str, tuple[float, bool]] = {}
        self._alive_lock = threading.Lock()

    @staticmethod
    def _default_spawn(cmd: list[str], log_path: Path):
        f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 (closed on reap)
        try:
            # start_new_session=True → 새 프로세스 그룹 → stop 시 자식까지 killpg 가능
            proc = subprocess.Popen(
                cmd,
                cwd=str(FRAMEWORK_ROOT),
                stdout=f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            f.close()
            raise
        proc._logfile = f  # is_running 에서 reap 시 close
        return proc

    def project_dir(self, run_id: str) -> Path:
        """run_id 를 base_dir 안으로 한정 (경로 traversal 차단)."""
        # 빈/공백 run_id 는 base 자체로 resolve 되므로 명시적으로 거부 (#67)
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError(f"invalid run id: {run_id!r}")
        base = self.base_dir.resolve()
        p = (base / run_id).resolve()
        # base 자체로 resolve 되면(예: ".") 거부, base 하위만 허용 (#67/#81)
        if p == base or base not in p.parents:
            raise ValueError(f"invalid run id: {run_id!r}")
        return p

    def start(self, spec_text: str, opts: dict) -> str:
        run_id = new_run_id(opts.get("name", "run"))
        project = self.project_dir(run_id)
        with self._lock:
            # /api/run 은 HTTP 레벨 요청 수에 제한이 없으므로, 전체 active run cap 이 없으면
            # 반복 POST 로 프로세스/디스크를 고갈시킬 수 있다. 종료된 tracked proc 은 먼저 정리한다.
            for rid, proc in list(self._procs.items()):
                try:
                    if proc.poll() is not None:
                        lf = getattr(proc, "_logfile", None)
                        if lf:
                            lf.close()
                        self._procs.pop(rid, None)
                except Exception:
                    self._procs.pop(rid, None)
            running = sum(
                1 for proc in self._procs.values() if getattr(proc, "poll", lambda: None)() is None
            )
            # #audit18(A5): 서버 재시작 후 _procs 가 비어도 디스크 pidfile 로 살아있는 외부 run 은
            # cap 에 포함해야 한다. 안 그러면 재시작마다 cap 이 0 으로 리셋돼 반복 POST 로 프로세스/
            # 디스크를 고갈시킬 수 있다(cap 의 목적 무력화). _procs 에 없는 live run 만 합산한다.
            tracked = set(self._procs)
            try:
                base = self.base_dir.resolve()
                entries = list(base.iterdir()) if base.exists() else []
            except OSError:
                base = self.base_dir
                entries = []
            for d in entries:
                try:
                    # #audit19(F2): list_runs 와 동일하게 symlink/containment 를 막는다. 예전엔
                    # d.is_dir() 가 symlink 를 따라가 base_dir 밖(또는 순환)을 가리키는 심볼릭
                    # 디렉터리도 live run 으로 세어 임의 run.pid 를 읽고 os.kill(pid,0) 프로브 +
                    # cap 오집계가 가능했다. symlink 는 건너뛰고, resolve 후 base 하위만 인정한다.
                    if d.name in tracked or d.is_symlink():
                        continue
                    rp = d.resolve()
                    if rp.parent != base or not d.is_dir():
                        continue
                    if self._run_alive_cached(d / ".orchestrator"):
                        running += 1
                except OSError:
                    continue
            if running >= self.max_running:
                raise RuntimeError(f"too many active runs ({running}/{self.max_running})")
            project.mkdir(parents=True, exist_ok=True)
            spec_path = project / "_spec.md"
            spec_path.write_text(spec_text or "# (empty spec)\n", encoding="utf-8")
            # 재실행(rerun)용으로 옵션 저장
            # LOW(#8): 예전엔 write 실패를 조용히 삼켰다(except: pass). 그러면 이후 rerun 이
            #          원래 backends/옵션을 잃고 조용히 기본값으로 실행된다. 실패를 stderr 로
            #          알려(서버 로그에 남게) 운영자가 디스크/권한 문제를 인지하게 한다.
            try:
                # #audit16: spec_text 는 이미 _spec.md 에 저장되므로 _run_opts.json 에 중복
                # 저장하지 않는다(대형/민감 spec 이 디스크에 두 번 남지 않게). rerun 은 spec 을
                # _spec.md 에서 읽으므로 영향 없음.
                opts_to_save = {k: v for k, v in opts.items() if k != "spec_text"}
                (project / "_run_opts.json").write_text(
                    json.dumps(opts_to_save, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as e:
                print(
                    f"[webui] 경고: _run_opts.json 저장 실패 ({project / '_run_opts.json'}): {e} "
                    "— 이후 재실행(rerun)은 기본 옵션으로 동작할 수 있습니다.",
                    file=sys.stderr,
                )
            cmd = build_command(sys.executable, spec_path, project, opts)
            try:
                proc = self._spawn(cmd, self.base_dir / f"{run_id}.log")
            except Exception:
                shutil.rmtree(project, ignore_errors=True)
                raise
            self._procs[run_id] = proc
        return run_id

    def stop(self, run_id: str) -> bool:
        """run 의 프로세스 그룹을 종료. SIGTERM(유예) 후 SIGKILL(강제)로 확실히 종료.

        오케스트레이터가 SIGTERM 을 트랩(graceful)해 안 죽는 경우가 있어 SIGKILL 폴백 필수.

        audit7(PID/PGID 재사용 방어): pgid 를 시작 시 한 번 캡처해 수 초간 os.killpg 를 반복하면,
        그 사이 그룹 리더가 죽고 커널이 같은 PGID 를 무관한 새 프로세스 그룹에 재할당할 때
        엉뚱한 그룹을 죽일 수 있다. 그래서 매 그룹 시그널 직전에 리더가 아직 그 그룹을 이끄는지
        재검증(_leader_leads_group)하고, 리더가 사라진 순간 그룹 시그널을 완전히 중단한다 —
        마지막 blanket 그룹 SIGKILL 도 보내지 않는다.
        """
        pid = None
        orch = self.project_dir(run_id) / ".orchestrator"
        pf = orch / "run.pid"
        if pf.exists():
            # HIGH(#1): 양의 정수 PID 만 받는다. 0/-1 같은 손상값이 os.kill/os.getpgid/
            #           os.killpg 로 흘러가 무관한 프로세스를 죽이는 사고를 막는다.
            pid = _read_pid(pf)
            # #M6: pidfile 의 pid 가 재사용된 무관 프로세스면(시작시각 토큰 불일치) 신뢰하지 않고
            #      tracked proc(우리가 직접 띄운 프로세스)으로 폴백한다(엉뚱한 프로세스 오살 방지).
            if pid is not None and not procutil.pid_is_ours(pid, procutil.read_pid_token(pf)):
                pid = None
        with self._lock:
            proc = self._procs.get(run_id)
        if pid is None and proc is not None:
            pid = getattr(proc, "pid", None)
        if not pid:
            return False
        try:
            pgid = os.getpgid(pid)
        except Exception:
            pgid = None

        def _leader_alive() -> bool:
            # audit7: 그룹 시그널 직전, 원래 리더 pid 가 아직 살아있고(우리가 띄운 자식이면
            #         poll() 로 좀비를 reap) 그 pid 가 여전히 같은 그룹을 이끄는지 재검증한다.
            #         tracked child 면 proc.poll() 을 liveness 의 1차 기준으로 쓴다(좀비 reap).
            if proc is not None and hasattr(proc, "poll"):
                try:
                    if proc.poll() is not None:  # 이미 종료(우리 자식) → 리더 사라짐
                        return False
                except Exception:
                    pass
            try:
                os.kill(pid, 0)  # signal 0 = 존재 확인 (죽었으면 OSError)
                return os.getpgid(pid) == pgid if pgid is not None else True
            except OSError:
                return False

        def _kill(sig):
            # audit7: 그룹 시그널은 원래 리더가 그 그룹을 아직 이끌 때만 보낸다(PGID 재사용 방어).
            #         리더가 사라졌으면(또는 그룹 정보 부재) per-process 폴백만, 그것도 리더 생존
            #         확인 시에만 — 재사용됐을 수 있는 pgid 에는 어떤 시그널도 보내지 않는다.
            try:
                if pgid is not None:
                    if _leader_alive():
                        os.killpg(pgid, sig)
                elif _leader_alive():
                    os.kill(pid, sig)
            except Exception:
                pass

        def _alive() -> bool:
            # 우리가 띄운 자식이면 poll() 로 좀비를 reap (os.kill(pid,0) 은 좀비도 살아있다고 봄).
            if proc is not None and hasattr(proc, "poll"):
                try:
                    return proc.poll() is None
                except Exception:
                    pass
            try:
                os.kill(pid, 0)  # signal 0 = 존재 확인 (죽었으면 OSError)
            except OSError:
                return False
            # 외부(우리가 reap 못하는) 프로세스가 종료되면 좀비로 남아 os.kill(pid,0) 은
            # 계속 성공한다. 좀비는 더 이상 run 상태를 쓰지 않으므로 종료로 간주한다.
            return not _is_zombie(pid)

        def _remove_pidfile():
            # #1: 비교도 _read_pid(첫 줄 PID)로 한다 — 다른 소유자가 둘째 줄에 start-time
            #     토큰을 덧붙여도(선택적 추가 줄) PID 가 우리 것과 같으면 정상 제거한다.
            try:
                if _read_pid(pf) != pid:
                    return
                pf.unlink()
            except Exception:
                pass

        def _sweep_stragglers():
            # audit5 #1/#3 복원: 리더 종료 후 SIGTERM 무시 잔존 자식을 그룹 SIGKILL 로 일소.
            # 그룹이 비어있지 않은 한 pgid 는 재사용 불가(잔존 자식이 '우리 그룹'의 증거)라 우리
            # 그룹만 친다(비었으면 ESRCH 무해). 완전한 stale-pidfile/PID 재사용 방어는 start-time
            # 검증이 필요 — 후속 과제. (audit7 wait-phase 가드 + audit5 reap 의 절충.)
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass

        # #13: pidfile 을 즉시 지우면 프로세스가 아직 살아 run 상태를 쓰는 중인데도
        #      UI/monitor 가 stopped 로 보인다. SIGTERM 후 실제 종료를 "확인"한 뒤에만
        #      pidfile 을 제거한다. 요청 스레드를 길게 막지 않도록 짧게(≈0.5초)만 동기 확인하고,
        #      그래도 살아있으면 백그라운드 supervisor 가 SIGKILL 폴백 후 제거한다.
        _kill(signal.SIGTERM)
        for _ in range(10):  # ≈0.5초: 대부분의 프로세스는 SIGTERM 으로 즉시 종료
            if not _alive():
                _sweep_stragglers()  # #3: 리더 사후 SIGTERM-무시 잔존 자식 일소
                _remove_pidfile()
                return True
            time.sleep(0.05)

        def _supervise():
            # 남은 시간(최대 ≈3.5초) graceful 종료를 더 기다린다(0.1초 간격 폴링).
            for _ in range(35):
                if not _alive():
                    _sweep_stragglers()  # #3: 리더 사후 잔존 자식 일소
                    _remove_pidfile()
                    return
                time.sleep(0.1)
            # 트랩 대비 강제 종료. SIGKILL 은 비동기라 즉시 죽지 않을 수 있으니 잠깐 사멸을
            # 확인한 뒤(있다면 좀비 reap 포함) pidfile 을 제거한다. #135: 어떤 경우에도
            # 최종적으로 pidfile 은 반드시 제거되어 "running" 잔상이 남지 않게 한다.
            _kill(signal.SIGKILL)  # 리더 생존 중 → 그룹 강제 종료(audit7 가드 통과)
            for _ in range(20):  # ≈1초: SIGKILL 후 커널 teardown 대기
                if not _alive():
                    break
                time.sleep(0.05)
            _sweep_stragglers()  # 리더 종료 후 남은 자식까지 일소
            _remove_pidfile()

        threading.Thread(target=_supervise, daemon=True).start()
        return True

    def rerun(self, run_id: str) -> str:
        """저장된 spec + opts 로 새 run 을 시작."""
        project = self.project_dir(run_id)
        sp = project / "_spec.md"
        # #audit16: 존재하지 않는 run 을 rerun 하면 빈 mock run 이 조용히 생성되던 문제.
        # 실제 run 아티팩트(_spec.md)가 없으면 unknown run 으로 거부한다(빈 run 양산 방지).
        if not project.is_dir() or not sp.is_file():
            raise ValueError(f"unknown run: {run_id!r}")
        spec_text = sp.read_text(encoding="utf-8", errors="replace")
        opts = {"mock": True}
        op = project / "_run_opts.json"
        if op.exists():
            try:
                opts = json.loads(op.read_text(encoding="utf-8"))
            except Exception:
                pass
        # MEDIUM(#3): /api/run 이 적용하는 타입 검증을 rerun 도 통과시킨다 — 손상/수기편집된
        #             opts(backends 가 dict, role_backends 가 list 등)가 build_command 안에서
        #             불투명하게 깨지지 않도록 start() 전에 정규화한다.
        opts = sanitize_run_opts(opts)
        return self.start(spec_text, opts)

    def _run_alive_cached(self, orch_dir) -> bool:
        """#6: _run_alive 결과를 pidfile 의 (경로,mtime,size) 키로 짧은 TTL 동안 캐시.

        폴링 핫패스(/api/runs·/api/state)에서 run 수에 비례하는 os.kill+ps 비용을 막는다.
        pidfile 이 바뀌면(stop 이 제거/갱신) 키가 바뀌어 캐시가 자동 무효화된다.
        """
        pf = orch_dir / "run.pid"
        try:
            st = pf.stat()
            key = f"{pf}|{st.st_mtime_ns}|{st.st_size}"
        except OSError:
            # pidfile 부재 등 — _run_alive 가 빠르게 False 를 돌려주므로 캐시 불필요.
            return _run_alive(orch_dir)
        now = time.monotonic()
        with self._alive_lock:
            cached = self._alive_cache.get(key)
            if cached is not None and now < cached[0]:
                return cached[1]
        result = _run_alive(orch_dir)
        with self._alive_lock:
            self._alive_cache[key] = (now + self._ALIVE_TTL, result)
            if len(self._alive_cache) > self._ALIVE_CACHE_MAX:
                # 만료 시각이 이른 순(가장 오래된)으로 정리 — 장기 실행 서버 누적 방지.
                for old in sorted(self._alive_cache, key=lambda k: self._alive_cache[k][0])[
                    : len(self._alive_cache) - self._ALIVE_CACHE_MAX
                ]:
                    self._alive_cache.pop(old, None)
        return result

    def is_running(self, run_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is not None:
            if getattr(proc, "poll", lambda: 0)() is None:
                return True
            # 종료됨 → 좀비 reap + 로그 핸들 close
            try:
                if hasattr(proc, "wait"):
                    proc.wait()
            except Exception:
                pass
            fh = getattr(proc, "_logfile", None)
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
            # #15: reap 한 프로세스는 _procs 에서 제거 (장기 실행 서버에서 누적 방지)
            with self._lock:
                if self._procs.get(run_id) is proc:
                    self._procs.pop(run_id, None)
        # 이 서버가 띄우지 않은(고아/외부) run 도 PID 파일로 생존 확인
        # #6: 폴링 핫패스 비용을 줄이려고 짧은 TTL 캐시를 통해 판정한다(의미 불변).
        try:
            return self._run_alive_cached(self.project_dir(run_id) / ".orchestrator")
        except ValueError:
            return False


# ----------------- HTTP -----------------


def _make_handler(manager: RunManager, token: str | None = None):
    # #17: WEB_UI_TOKEN 이 설정되면(빈 문자열 아님) 모든 /api/* 요청에 토큰을 요구한다.
    #      미설정이면 인증 비활성(하위호환). serve() 가 env 에서 읽어 주입하지만, 테스트가
    #      직접 토큰을 넘길 수 있도록 인자도 받는다(인자 우선).
    auth_token = (token if token is not None else os.environ.get("WEB_UI_TOKEN", "")).strip()

    class Handler(BaseHTTPRequestHandler):
        # audit7(slow-loris 방어): BaseHTTPRequestHandler 는 이 클래스 속성 timeout 을
        #   소켓 read 타임아웃으로 설정한다(setup() 에서 self.connection.settimeout(self.timeout)).
        #   미설정(None)이면 헤더/바디 read 가 무한정 블록돼 느린 클라이언트가
        #   ThreadingHTTPServer 워커 스레드를 영구 점유한다. 30초 상한을 둔다.
        timeout = 30

        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str, extra_headers=None):
            # #88: 클라이언트 연결 끊김(소켓 쓰기 실패)은 조용히 무시 — 트레이스백 방지
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")  # 실시간: 캐시 금지
                # #audit15: defense-in-depth 보안 헤더. 단일 파일 UI(외부 리소스 없음)라
                # 정상 동작에는 영향이 없다. CSP 는 인라인 <script>/<style>/onclick 핸들러를
                # 쓰므로 'unsafe-inline' 을 script/style 에 허용하되 나머지는 self 로 제한.
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
                )
                for k, v in extra_headers or []:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionError):
                pass

        def _json(self, obj, code: int = 200):
            # #4: json.dumps 가 직렬화 불가 값(set/객체)이나 surrogate(비-UTF8) 경로명 등으로
            #     raise 하면, 응답을 못 보내고 핸들러가 죽어 클라이언트는 빈 연결을 받는다.
            #     직렬화/인코딩을 try/except 로 감싸 실패 시 제네릭 500 JSON 으로 응답한다.
            # #9: 바디가 UTF-8(ensure_ascii=False)이므로 Content-Type 에 charset=utf-8 명시.
            # #audit13: allow_nan=False — 손상 board 의 NaN/Infinity 가 비표준 JSON(`NaN`)으로
            #           나가 브라우저 JSON.parse 를 깨뜨리는 것을 막는다. 위반 시 ValueError →
            #           아래 except 가 잡아 제네릭 500 JSON 으로 응답한다.
            try:
                body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError):
                code = 500
                body = b'{"error": "internal serialization error"}'
            self._send(code, body, "application/json; charset=utf-8")

        def _redirect(self, location: str, extra_headers=None):
            self._send(
                303,
                b"",
                "text/plain; charset=utf-8",
                [("Location", location)] + (extra_headers or []),
            )

        # ---- #17: 토큰 인증 (WEB_UI_TOKEN 설정 시에만 활성) ----
        def _provided_token(self) -> str:
            """요청에서 토큰을 추출: Authorization: Bearer → X-Auth-Token → 쿠키."""
            auth = self.headers.get("Authorization", "") or ""
            if auth.startswith("Bearer "):
                t = auth[len("Bearer ") :].strip()
                if t:
                    return t
            x = (self.headers.get("X-Auth-Token") or "").strip()
            if x:
                return x
            for part in (self.headers.get("Cookie", "") or "").split(";"):
                k, _, val = part.strip().partition("=")
                if k == "token" and val:
                    # 쿠키 값은 set 시 percent-encode 되므로 비교 전에 원복한다(아무 토큰이나 round-trip).
                    return unquote(val)
            return ""

        def _authed(self) -> bool:
            if not auth_token:
                return True  # 토큰 미설정 → 인증 비활성(하위호환)
            provided = self._provided_token()
            # 타이밍 공격 방지를 위해 상수시간 비교.
            return _token_equal(provided, auth_token)

        def _require_auth(self) -> bool:
            """인증 실패면 401 을 보내고 True 를 반환(호출부는 곧장 return)."""
            if self._authed():
                return False
            self._json({"error": "unauthorized"}, 401)
            return True

        def _cookie_attrs(self) -> str:
            attrs = "Path=/; HttpOnly; SameSite=Strict"
            if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https":
                attrs += "; Secure"
            return attrs

        # ---- #9 / audit7: CSRF/Origin 방어 ----
        def _is_cookie_only_auth(self) -> bool:
            """이 요청이 쿠키만으로 인증됐는지(= Authorization/X-Auth-Token 헤더 없음) 판별.

            브라우저는 자동으로 쿠키를 실어 보내므로 쿠키 인증 요청이 CSRF 의 표적이 된다.
            반대로 Authorization/X-Auth-Token 헤더는 비-브라우저(curl 등)가 명시적으로 붙여야
            하므로 CSRF 로 위조되지 않는다 → 그런 요청은 Origin 없이도 진행을 허용한다.
            """
            auth = self.headers.get("Authorization", "") or ""
            if auth.startswith("Bearer ") and auth[len("Bearer ") :].strip():
                return False
            if (self.headers.get("X-Auth-Token") or "").strip():
                return False
            return True

        def _origin_ok(self) -> bool:
            """상태변경 POST 의 cross-origin 요청 차단(쿠키 인증을 켜도 CSRF 막기).

            audit7 강화:
            - host 비교를 정규화한다(대소문자 무시 + 기본 포트 보정) — Origin 의 host:port 를
              Host 헤더와 비교.
            - 토큰이 설정돼 있고 이 요청이 *쿠키로만* 인증된 경우(헤더 토큰 없음)에는, 상태변경
              POST 에 일치하는 Origin 을 *반드시* 요구한다(Origin 부재 시 fail-open 금지) —
              브라우저가 쿠키를 자동 전송하므로 CSRF 표적이기 때문.
            - Authorization/X-Auth-Token 헤더로 인증하는 비-브라우저 클라이언트(쿠키 없음)는
              Origin 없이도 진행을 허용한다(CSRF 로 위조 불가).

            audit9(#2) 강화 — 무토큰 dogfood 보호:
            - 토큰이 *미설정* 이어도 Origin 부재 시 무조건 허용하면, 로컬 악성 페이지가
              fetch 로 /api/run 에 POST 해 임의 빌드를 시작시킬 수 있다(브라우저는 same-site
              cross-origin POST 에 Origin 을 안 붙일 수 있다). 그래서 *상태변경 POST* 에서
              Origin 이 없으면 Sec-Fetch-Site 를 본다 — 모든 현대 브라우저가 fetch/navigation
              에 자동으로 붙이며 위조 불가하다:
                · same-origin / none(주소창 직접 입력) → 허용
                · cross-site / same-site → 차단(CSRF)
              Sec-Fetch-Site 도 없는 요청은 비-브라우저(curl/urllib 등)로 보고 기존대로 허용해
              자동화/테스트 호환을 유지한다(브라우저가 아니므로 CSRF 표적이 아님).
            """
            origin = self.headers.get("Origin")
            if not origin:
                # Origin 부재: 쿠키-only 인증(토큰 설정 시)이면 차단(CSRF 표적).
                if auth_token and self._is_cookie_only_auth():
                    return False
                # #2: 무토큰/헤더토큰이라도 브라우저발 cross-site/same-site POST 는 차단한다.
                sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                if sfs in ("cross-site", "same-site"):
                    return False
                # same-origin/none → 허용, 미설정(비-브라우저) → 허용(하위호환).
                return True
            try:
                parsed = urlparse(origin)
                o_host = parsed.hostname
                o_port = parsed.port
                o_scheme = parsed.scheme
            except Exception:
                return False
            host = self.headers.get("Host") or ""
            return self._host_port_eq(o_host, o_port, o_scheme, host)

        @staticmethod
        def _host_port_eq(o_host, o_port, o_scheme: str, host_header: str) -> bool:
            """Origin 의 (host, port) 가 Host 헤더의 (host, port) 와 같은지 정규화 비교한다.

            - host 는 대소문자 무시.
            - 포트가 명시되지 않으면 scheme 의 기본 포트(http=80/https=443)로 보정해 비교한다
              (예: Origin "http://Host" ≡ Host "host:80").
            """
            if not o_host:
                return False
            default_port = 443 if (o_scheme or "").lower() == "https" else 80
            o_port = o_port if o_port is not None else default_port
            # Host 헤더를 host/port 로 분해 ("host" 또는 "host:port"; IPv6 "[::1]:port" 대응).
            h = host_header.strip()
            if h.startswith("["):  # IPv6 리터럴
                end = h.find("]")
                h_host = h[1:end] if end != -1 else h
                rest = h[end + 1 :] if end != -1 else ""
                h_port_s = rest[1:] if rest.startswith(":") else ""
            else:
                h_host, _, h_port_s = h.partition(":")
            try:
                h_port = int(h_port_s) if h_port_s else default_port
            except ValueError:
                return False
            return o_host.lower() == h_host.lower() and o_port == h_port

        def _require_same_origin(self) -> bool:
            """cross-origin 이면 403 을 보내고 True 를 반환(호출부는 곧장 return)."""
            if self._origin_ok():
                return False
            self._json({"error": "cross-origin request blocked"}, 403)
            return True

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            # #17: 모든 데이터/제어는 /api/* 뒤에 있으므로 거기서 인증을 강제한다. index(/)
            #      자체는 비밀이 없는 정적 셸이라 인증 없이 제공하되, 유효한 ?token= 으로 접속하면
            #      쿠키를 심어 이후 fetch 가 자동 인증되게 한다(브라우저 사용성).
            if u.path.startswith("/api/") and self._require_auth():
                return
            # #RA-origin: deferred — GET /api/* 에 _require_same_origin 을 그대로 재사용하면
            #   기존 통과 테스트(test_audit5_webui.test_token_via_cookie)가 깨진다. 그 테스트는
            #   토큰 서버에서 *쿠키-only 인증 + Origin 부재* 인 GET /api/runs 가 200 을 받기를
            #   기대하는데, _origin_ok 는 바로 그 조합(auth_token and _is_cookie_only_auth and
            #   Origin 부재)을 CSRF 표적으로 보고 차단(403)한다 — 이는 POST 도 거부하는 경로다.
            #   즉 "POST 를 거부할 요청만 거부" 라는 제약을 지키면서 GET 에 적용하면, 합법적
            #   쿠키-only 브라우저 GET 까지 막혀 하위호환이 깨진다. RA-origin 은 LOW~MEDIUM(현재
            #   SameSite=Strict 쿠키가 cross-site 전송을 이미 차단)이므로, 기존 테스트를 깨지
            #   않도록 GET 에는 적용하지 않고 보류한다(POST 의 same-origin 방어는 그대로 유지).
            if u.path == "/":
                extra = None
                if auth_token:
                    qt = (q.get("token") or [""])[0]
                    if _token_equal(qt, auth_token):
                        # #10: HttpOnly 로 JS/XSS 의 쿠키 탈취를 막고 SameSite=Strict 로 cross-site
                        #      전송을 차단한다. TLS 종단 프록시가 X-Forwarded-Proto=https 를 주면
                        #      Secure 도 붙인다. 토큰에 쿠키-불가 문자(공백/;/non-ascii)가 있어도
                        #      percent-encode 해 항상 유효한 쿠키로 심는다(=불가문자 시 401 루프 방지).
                        cookie_val = quote(qt, safe="")
                        extra = [("Set-Cookie", f"token={cookie_val}; {self._cookie_attrs()}")]
                        # query token 은 브라우저 히스토리/리퍼러에 남을 수 있으므로 쿠키 설정 즉시
                        # 깨끗한 URL 로 이동시킨다. API 토큰 인증은 그대로 유지된다.
                        self._redirect("/", extra)
                        return
                self._send(
                    200,
                    INDEX_HTML.encode("utf-8"),
                    "text/html; charset=utf-8",
                    extra_headers=extra,
                )
            elif u.path == "/api/runs":
                runs = list_runs(manager.base_dir)
                for r in runs:
                    r["running"] = manager.is_running(r["id"])
                self._json({"runs": runs})
            elif u.path == "/api/check":
                rows = backend_status()
                for r in rows:
                    r["info"] = BACKEND_INFO.get(r["name"], "")
                self._json({"backends": rows, "roles": list(ROLES)})
            elif u.path == "/api/state":
                run = (q.get("run") or [""])[0]
                proj = None
                board = {}
                events = ""
                agent_logs = {}
                exists = False  # #69: board.json 존재 여부(미초기화 vs 미존재 구분)
                if run:
                    try:
                        proj = manager.project_dir(run)
                    except ValueError:
                        self._json({"error": "invalid run id"}, 400)
                        return
                    orch = proj / ".orchestrator"
                    exists = (orch / "board.json").exists()
                    board = _read_board(orch)
                    events = _read_events(orch)
                    # #34/#35: 폴링마다 모든 역할 로그를 보내지 않고, board 에 기록된
                    # (= 한 번이라도 활동한) 역할의 로그만 보낸다. 전체 로그는 /api/agent.
                    active_roles = [r for r in ROLES if r in board.get("agents", {})]
                    agent_logs = _read_agent_logs(orch, active_roles)
                running = manager.is_running(run) if run else False
                # 런이 죽었는데 board 에 "running" 으로 남은 에이전트는 stopped 로 표시
                # (정상 종료 중 취소되거나 강제 종료된 경우 — 실제로는 안 돌고 있음)
                if run and not running:
                    for a in board.get("agents", {}).values():
                        if a.get("status") == "running":
                            a["status"] = "stopped"
                self._json(
                    {
                        "roles": list(ROLES),
                        "board": board,
                        "running": running,
                        "exists": exists,
                        # #12: _read_board 가 손상 board.json 에 {"_corrupt": True} 를 돌려주는데
                        #      예전엔 응답에 반영하지 않아 UI 가 "미초기화"처럼 보였다. corrupt 를
                        #      명시적으로 노출해 UI 가 "board 손상" 을 표시할 수 있게 한다.
                        "corrupt": bool(board.get("_corrupt")),
                        "project_dir": str(proj) if proj else "",
                        "events": events,
                        "agent_logs": agent_logs if run else {},
                    }
                )
            elif u.path == "/api/agent":
                run = (q.get("run") or [""])[0]
                role = (q.get("role") or [""])[0]
                # #68: run/role 둘 다 비어있으면 안 됨 — 빈 성공 응답 대신 400
                if not run or not role:
                    self._json({"error": "run and role are required"}, 400)
                    return
                if role not in ROLES:  # 경로 traversal/임의 파일 읽기 차단
                    self._json({"error": "invalid role"}, 400)
                    return
                try:
                    orch = manager.project_dir(run) / ".orchestrator"
                except ValueError:
                    self._json({"error": "invalid run id"}, 400)
                    return
                board = _read_board(orch)
                self._json(
                    {
                        "agent": board.get("agents", {}).get(role, {}),
                        "log": _read_agent_log(orch, role),
                    }
                )
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path not in ("/api/run", "/api/stop", "/api/rerun"):
                self._json({"error": "not found"}, 404)
                return
            # #17: run 제어(/api/run·stop·rerun)는 토큰 인증을 먼저 강제한다(무토큰/오토큰 → 401).
            #      (origin 보다 먼저: 자격증명 없는 요청은 CSRF 판정 이전에 명확히 401 로 거부.)
            if self._require_auth():
                return
            # #9: 인증을 통과해도 cross-origin / 쿠키-only(Origin 부재) 상태변경 POST 는 차단한다
            #     (쿠키 자동전송을 악용한 CSRF 방어). 헤더 토큰 인증 비-브라우저 요청은 통과.
            if self._require_same_origin():
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):  # malformed 헤더 → 핸들러가 죽지 않게 400
                self._json({"error": "invalid Content-Length"}, 400)
                return
            if length < 0:
                self._json({"error": "invalid Content-Length"}, 400)
                return
            if length > MAX_BODY_BYTES:
                self._json({"error": f"body too large (> {MAX_BODY_BYTES} bytes)"}, 413)
                return
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "invalid json"}, 400)
                return
            # #78: json.loads 는 list/str/number/null 도 반환할 수 있다 — 이후 data.get(...)
            #      이 AttributeError 를 내고 연결이 끊기는 대신 400 으로 거부.
            if not isinstance(data, dict):
                self._json({"error": "request body must be a JSON object"}, 400)
                return

            if u.path == "/api/stop":
                run = data.get("run", "")
                try:
                    ok = manager.stop(run) if run else False
                except ValueError:
                    self._json({"error": "invalid run id"}, 400)
                    return
                self._json({"stopped": ok})
                return
            if u.path == "/api/rerun":
                run = data.get("run", "")
                try:
                    alive = bool(run) and manager.is_running(run)
                except ValueError:
                    self._json({"error": "invalid run id"}, 400)
                    return
                if not run or alive:
                    self._json({"error": "실행 중인 run 은 재실행 불가 — 먼저 정지하세요"}, 409)
                    return
                try:
                    rid = manager.rerun(run)
                except ValueError:
                    self._json({"error": "invalid run id"}, 400)
                    return
                except Exception:
                    # #38: 내부 예외 텍스트(int() 변환 오류 등)를 그대로 노출하지 않고
                    #      깔끔한 메시지로 응답한다. (build_command 는 이미 손상값을 관대히 처리)
                    self._json(
                        {"error": "재실행 실패 — 저장된 실행 옵션이 손상되었을 수 있습니다"}, 400
                    )
                    return
                self._json({"run_id": rid})
                return

            # /api/run
            # #89: spec_text 는 반드시 문자열 — list/object 면 write_text 에서 깨지므로 400.
            spec_text = data.get("spec_text", "")
            if spec_text is None:
                spec_text = ""
            if not isinstance(spec_text, str):
                self._json({"error": "spec_text must be a string"}, 400)
                return
            # #60: 글자 수가 아니라 인코딩 바이트 길이로 검사 (MAX_SPEC_BYTES 의 의미에 맞춤).
            if len(spec_text.encode("utf-8")) > MAX_SPEC_BYTES:
                self._json({"error": f"spec too large (> {MAX_SPEC_BYTES} bytes)"}, 413)
                return
            # #136: backend 는 resolve() 에 넘기기 전에 반드시 str 이어야 한다.
            #       JSON 배열/객체는 unhashable → ALIASES.get() 이 TypeError 를 내고
            #       400 대신 핸들러가 죽는다. 타입을 먼저 검증한다.
            backend = data.get("backend", "mock")
            if not isinstance(backend, str):
                self._json({"error": "backend must be a string"}, 400)
                return
            if resolve(backend) not in VALID_BACKENDS:  # CLI 와 동일하게 alias 허용
                self._json({"error": f"invalid backend: {backend}"}, 400)
                return
            # #79: backends 는 list(또는 null)여야 함 — 문자열이면 글자 단위로 순회되므로 거부.
            backends = data.get("backends")
            if backends is not None and not isinstance(backends, list):
                self._json({"error": "backends must be a list"}, 400)
                return
            for b in backends or []:
                # #136: 각 엔트리도 str 이어야 resolve() 에 안전하게 넘길 수 있다.
                if not isinstance(b, str):
                    self._json({"error": f"backend in priority list must be a string: {b!r}"}, 400)
                    return
                if resolve(b) not in VALID_BACKENDS:
                    self._json({"error": f"invalid backend in priority list: {b}"}, 400)
                    return
            # #80: role_backends 는 dict(또는 null)여야 함 — .items() 전에 타입 검증.
            role_backends = data.get("role_backends")
            if role_backends is not None and not isinstance(role_backends, dict):
                self._json({"error": "role_backends must be an object"}, 400)
                return
            for role, prov in (role_backends or {}).items():
                if role not in ROLES:
                    self._json({"error": f"invalid role: {role}"}, 400)
                    return
                # #136: prov 는 str 또는 list-of-str 만 허용. dict/숫자/중첩 리스트는
                #       resolve() 에서 unhashable TypeError 를 내므로 먼저 거부한다.
                if not (isinstance(prov, (str, list)) or prov is None):
                    self._json(
                        {"error": f"role_backends value must be a string or list: {role}"}, 400
                    )
                    return
                # #100/#101: prov 는 단일 백엔드(str) 또는 우선순위 리스트(list) 가능.
                provs = prov if isinstance(prov, list) else ([prov] if prov else [])
                for p in provs:
                    if not isinstance(p, str):
                        self._json({"error": f"backend for {role} must be a string: {p!r}"}, 400)
                        return
                    if p and resolve(p) not in VALID_BACKENDS:
                        self._json({"error": f"invalid backend for {role}: {p}"}, 400)
                        return
            # 정수 옵션 검증: int 변환 + 범위. 음수/비정상값은 400 으로 거부.
            # 이 필드들은 CLI/RunConfig 에서도 진짜 int (concurrency/max_units/
            # max_attempts/retries). retries/max_attempts 는 0 허용(>=0), 나머지는 >=1.
            for fld in ("concurrency", "max_units", "max_attempts", "retries"):
                v = data.get(fld)
                lo = 0 if fld in ("retries", "max_attempts") else 1
                _iv, err = _parse_int_option(v, field=fld, minimum=lo)
                if err:
                    self._json({"error": err}, 400)
                    return
            # #12: poll_interval / timeout / budget 은 실수(float) 옵션이다.
            #      CLI(--poll-interval type=float, default=20.0)와 RunConfig
            #      (poll_interval: float)에서 모두 float 이므로 1.5 같은 소수도 유효하다.
            #      예전엔 poll_interval 을 int(v) 로 검증해 1.5 가 불필요하게 400 으로
            #      거부됐다 — 같은 값이 CLI 에선 통과하는데 웹에선 막히는 정책 불일치.
            #      이제 float 로 검증해 두 진입점의 타입 정책을 일치시킨다.
            #      poll_interval 은 0 허용(>=0; RunConfig __post_init__ 가 안전 하한으로
            #      클램프), budget/timeout 도 0 은 의미 없으나 음수만 거부(>=0).
            for fld in ("poll_interval", "timeout", "budget"):
                v = data.get(fld)
                if v in (None, ""):
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    self._json({"error": f"invalid {fld}: {v!r}"}, 400)
                    return
                # #8/#9: NaN/Inf 는 float() 를 통과하지만 비교를 무력화한다(budget=nan 이면
                #        예산 enforcement 가 꺼지고, inf poll/timeout 은 supervisor 를 멈춘다).
                #        fv<0 검사보다 먼저 비유한값을 거부한다(nan<0 은 False 라 통과하므로).
                if not math.isfinite(fv):
                    self._json({"error": f"{fld} must be a finite number (got {fv})"}, 400)
                    return
                if fv < 0:
                    self._json({"error": f"{fld} must be >= 0 (got {fv})"}, 400)
                    return
            level = str(data.get("completion_level") or "mvp").strip().lower()
            if level not in ("mvp", "production"):
                self._json({"error": f"invalid completion_level: {level!r}"}, 400)
                return
            # #137: name 은 slugify().strip() 으로 흘러가므로 str 이 아니면 .strip() 에서
            #       raise 한다. None(미지정)은 start() 가 기본값 "run" 으로 처리하므로 허용,
            #       그 외 비문자열(숫자/list/object)은 400 으로 거부한다.
            name = data.get("name")
            if name is not None and not isinstance(name, str):
                self._json({"error": "name must be a string"}, 400)
                return
            # #M03: model 은 검증 없이 build_command 의 --model 인자로 흘러갔다. 비문자열/과길이/
            #       '-' 시작('--flag' 로 오인돼 argv 오염)을 거부한다.
            model = data.get("model")
            if model is not None and (
                not isinstance(model, str) or len(model) > 200 or model.startswith("-")
            ):
                self._json({"error": "invalid model"}, 400)
                return
            # #audit16: manager.start() 예외(동시 run 한도 초과·mkdir/write 실패 등)를 핸들러
            # traceback/연결종료로 흘리지 않고 JSON 에러로 매핑한다.
            try:
                run_id = manager.start(spec_text, data)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
                return
            except RuntimeError as e:
                # 동시 run 한도 초과 등(우리 메시지, 비민감) → 사유를 노출
                self._json({"error": str(e)}, 429)
                return
            except Exception:
                # mkdir/write 등 내부 오류 → 경로 등 민감정보 노출 방지로 일반 메시지
                self._json({"error": "run 시작 실패 (서버 내부 오류)"}, 500)
                return
            self._json({"run_id": run_id})

    return Handler


def serve(port: int = 8765, base_dir: Path | None = None, host: str = "127.0.0.1") -> None:
    base = Path(base_dir) if base_dir else (Path.home() / "agent-runs")
    # #17: WEB_UI_TOKEN 이 설정되면 토큰 인증을 켠다(없으면 하위호환으로 인증 없음).
    token = os.environ.get("WEB_UI_TOKEN", "").strip()
    loopback = host in ("127.0.0.1", "localhost", "::1")
    # #8: 인증 없이 비-루프백(예: 0.0.0.0)에 바인딩하는 것은 fail-closed 로 거부한다 —
    #     run 제어 UI 가 무인증으로 네트워크에 노출되는 배포 사고를 막는다. Docker 기본은
    #     0.0.0.0 이므로 WEB_UI_TOKEN 을 반드시 설정해야 웹 UI 가 기동한다.
    if not loopback and not token:
        raise SystemExit(
            f"거부: 인증 없이 비-루프백 호스트({host})에 바인딩할 수 없습니다. "
            "WEB_UI_TOKEN 을 설정(예: -e WEB_UI_TOKEN=…)하거나 127.0.0.1 에 바인딩하세요."
        )
    manager = RunManager(base)
    httpd = ThreadingHTTPServer((host, port), _make_handler(manager, token))
    # LOW(#5): ThreadingHTTPServer.daemon_threads 기본은 False — 종료 시 진행 중인 워커
    #          스레드를 join 하느라 매달릴 수 있다. daemon=True 로 즉시 종료 가능하게 한다.
    httpd.daemon_threads = True
    print(f"web ui: http://{host}:{port}   (runs → {base})")
    if token:
        print(f"  [auth] WEB_UI_TOKEN 필요 — 최초 접속: http://{host}:{port}/?token=…")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
    finally:
        # LOW(#5): server_close() 누락 시 소켓이 닫히지 않아 포트가 바인딩된 채 남을 수 있다.
        httpd.server_close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.webui", description="멀티에이전트 웹 UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--base-dir", type=Path, help="실행 결과 디렉터리 (기본 ~/agent-runs)")
    a = p.parse_args(argv)
    serve(a.port, a.base_dir, a.host)
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Multi-Agent Console</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo",sans-serif;
       background:#0d1117;color:#e6edf3}
  header{padding:12px 20px;border-bottom:1px solid #30363d;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
  h1{font-size:16px;margin:0}
  .muted{color:#8b949e;font-size:13px}
  main{padding:16px 20px;max-width:1000px;margin:0 auto}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
  label{display:block;font-size:12px;color:#8b949e;margin:8px 0 4px}
  input,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 9px;font-size:13px}
  input[type=text],select{width:100%}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row>div{flex:1;min-width:140px}
  button{background:#238636;color:#fff;border:0;border-radius:6px;padding:9px 16px;font-size:14px;cursor:pointer}
  button.secondary{background:#21262d;border:1px solid #30363d}
  button:disabled{opacity:.5;cursor:default}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d}
  th{color:#8b949e;font-weight:600}
  tr.agent{cursor:pointer}
  tr.agent:hover{background:#1c2330}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:#3d444d}
  .dot.run{background:#2ea043;box-shadow:0 0 7px #2ea043}
  .pill{font-size:11px;padding:2px 8px;border-radius:999px;background:#21262d;border:1px solid #30363d}
  pre{background:#010409;border:1px solid #30363d;border-radius:6px;padding:12px;overflow:auto;max-height:52vh;
      font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
  .hide{display:none}
  a{color:#58a6ff}
  .cost{color:#d29922;font-variant-numeric:tabular-nums}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
  .agent-card{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px}
  .agent-card.run{border-color:#2ea043}
  .agent-card h5{margin:0 0 4px;font-size:14px}
  .agent-card .meta{font-size:11px;color:#8b949e;margin-bottom:6px}
  .agent-card pre{max-height:30vh;margin:0;font-size:11px}
  .badge{font-size:10px;padding:1px 6px;border-radius:999px;background:#21262d;border:1px solid #30363d;margin-left:6px}
</style></head>
<body>
<header>
  <h1>🤖 Multi-Agent Console</h1>
  <span class="muted" id="hdr">기획서를 업로드해 실행하세요.</span>
  <span style="flex:1"></span>
  <label style="margin:0">run</label>
  <select id="runSel" style="width:auto" onchange="selectRun(this.value)"></select>
  <button class="secondary" onclick="showLaunch()">+ 새 실행</button>
</header>
<main>
  <!-- Run picker (자동선택 안 함 — 사용자가 선택) -->
  <section id="picker" class="card hide">
    <h3 style="margin-top:0">실행(run) 선택</h3>
    <div id="runList" class="muted">불러오는 중…</div>
    <button class="secondary" style="margin-top:10px" onclick="showLaunch()">+ 새 실행</button>
  </section>

  <!-- Launch -->
  <section id="launch" class="card">
    <h3 style="margin-top:0">새 실행 — 기획서 업로드</h3>
    <div class="row">
      <div><label>기획서 파일 (.md/.txt/.html)</label><input type="file" id="specFile" accept=".md,.txt,.markdown,.html,.htm"/></div>
      <div><label>실행 이름</label><input type="text" id="name" placeholder="my-app"/></div>
    </div>
    <div class="row">
      <div style="flex:2"><label>백엔드 (우선순위 순, 콤마 · 1개=단일 / 여러 개=폴오버·분산·교차)</label>
        <input type="text" id="backends" placeholder="claude-cli   또는   claude-cli,codex"/></div>
      <div><label>동시성</label><input type="text" id="concurrency" value="3"/></div>
      <div><label>max-units (선택)</label><input type="text" id="maxUnits" placeholder="전체"/></div>
      <div><label>max-attempts</label><input type="text" id="maxAttempts" value="0"/></div>
    </div>
    <div class="row">
      <div><label title="감독(PM/PL) 주기. 웹 기본 600초 — CLI 기본(20초)보다 길게 설정됨. 짧게 하려면 직접 입력.">poll-interval (초, 선택 · 웹 기본 600 / CLI 20)</label><input type="text" id="pollInterval" placeholder="600 (웹 기본)"/></div>
      <div><label>timeout (초, 선택)</label><input type="text" id="timeout" placeholder="기본"/></div>
      <div><label>retries (선택)</label><input type="text" id="retries" placeholder="1"/></div>
      <div><label>completion</label><select id="completionLevel"><option value="mvp">mvp</option><option value="production">production</option></select></div>
      <div><label>budget (USD, 선택)</label><input type="text" id="budget" placeholder="없음"/></div>
      <div><label>model (선택)</label><input type="text" id="model" placeholder="백엔드 기본값"/></div>
    </div>
    <div id="backendStatus" class="muted" style="margin-top:8px;font-size:12px">백엔드 상태 확인 중…</div>
    <details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">역할별 프로바이더 직접 지정 (auto = 미지정 → cross-check 시 교차 배치)</summary>
      <div id="roleGrid" class="row" style="flex-wrap:wrap;margin-top:8px"></div>
    </details>
    <div class="row" style="margin-top:10px;align-items:center">
      <label style="margin:0"><input type="checkbox" id="mock"/> mock (무비용 · 선택한 실제 백엔드 무시)</label>
      <label style="margin:0"><input type="checkbox" id="delegate"/> delegate (팀 위임)</label>
      <label style="margin:0"><input type="checkbox" id="fullAccess"/> full-access (머신 전체 권한)</label>
      <label style="margin:0"><input type="checkbox" id="autoCommit" checked/> auto-commit (단계별 git)</label>
      <label style="margin:0"><input type="checkbox" id="distribute"/> distribute (풀 분산)</label>
      <label style="margin:0"><input type="checkbox" id="crossCheck"/> cross-check (교차 검증)</label>
      <span style="flex:1"></span>
      <button id="runBtn" onclick="startRun()">▶ 실행</button>
    </div>
    <p class="muted" id="launchMsg"></p>
  </section>

  <!-- Monitor: 단일 대시보드 (클릭 불필요, 모두 항상 표시) -->
  <section id="dash" class="card hide">
    <div class="row" style="align-items:center;margin-bottom:8px">
      <button class="secondary" onclick="showPicker()">← run 목록</button>
      <span style="flex:1"></span>
      <button id="stopBtn" class="secondary" onclick="stopRun()">■ 정지</button>
      <button id="rerunBtn" class="secondary" onclick="rerunRun()">↻ 재실행</button>
    </div>
    <div class="muted" style="margin-bottom:6px">
      📁 결과물 저장 위치: <b id="projDir" style="user-select:all">—</b>
    </div>
    <div class="row" style="align-items:center;margin-bottom:8px">
      <div class="muted">phase <b id="phase">—</b></div>
      <div class="muted">cost <b class="cost" id="cost">$0</b></div>
      <div class="muted">tokens <b id="tok">0</b></div>
      <div class="muted">units <b id="units">0/0</b></div>
      <div class="muted">동시 실행 <b id="runCount">0</b></div>
      <div class="muted">상태 <span class="pill" id="running">—</span></div>
    </div>
    <!-- #23: /api/state fetch/렌더 오류를 조용히 삼키지 않고 사용자에게 표시 -->
    <div id="err" style="display:none;color:#f85149;background:#2d1214;border:1px solid #6e2329;
      border-radius:6px;padding:8px 10px;margin-bottom:8px;font-size:13px"></div>

    <h4 style="margin:6px 0">에이전트 (카드 · 실시간 로그)</h4>
    <div id="agentCards" class="cards"></div>

    <h4 style="margin:14px 0 6px">통합 로그 (이벤트)</h4>
    <pre id="liveLog" style="max-height:26vh">(로그 대기 중…)</pre>

    <h4 style="margin:14px 0 6px">산출물 (생성된 파일)</h4>
    <div id="artifacts" class="muted" style="font-size:12px">(아직 없음)</div>
  </section>
</main>
<script>
let CUR=null;
const $=id=>document.getElementById(id);
async function loadChecks(){
  let data={backends:[],roles:[]};
  try{data=await (await fetch("/api/check")).json()}catch(e){}
  const rows=data.backends||[];
  const bad=rows.filter(r=>!r.ok);
  // #65: 백엔드 이름/사유를 esc() 로 이스케이프 — HTML/스크립트 주입 방지
  $("backendStatus").innerHTML="백엔드: "+rows.map(r=>(r.ok?"✅":"❌")+" "+esc(r.name)).join("&nbsp;&nbsp;")+
    (bad.length?"<br>미가용 — "+bad.map(r=>esc(r.name)+": "+esc(r.reason)).join(" · "):"");
  // 백엔드 입력칸이 비어있으면 가용한 첫 백엔드를 기본값으로 채움
  const okOnes=rows.filter(r=>r.ok).map(r=>r.name);
  if(!$("backends").value && okOnes.length) $("backends").value=okOnes[0];
  // 역할별 프로바이더 그리드 (auto + 백엔드들)
  const names=rows.map(r=>r.name);
  const grid=$("roleGrid");grid.innerHTML="";
  (data.roles||[]).forEach(role=>{
    const d=document.createElement("div");d.style.minWidth="210px";
    const s=document.createElement("select");s.dataset.role=role;s.style.width="100%";
    const a=document.createElement("option");a.value="";a.text="auto";s.appendChild(a);
    names.forEach(n=>{const o=document.createElement("option");o.value=n;o.text=n;s.appendChild(o)});
    const lab=document.createElement("label");lab.textContent=role;
    d.appendChild(lab);d.appendChild(s);grid.appendChild(d);
  });
}

async function startRun(){
  const f=$("specFile").files[0];
  if(!f){$("launchMsg").textContent="기획서 파일을 선택하세요.";return}
  const spec_text=await f.text();
  $("runBtn").disabled=true;$("launchMsg").textContent="실행 시작 중…";
  // #63: fetch/json 에서 예외가 나도 runBtn 이 영구 비활성화되지 않도록 try/finally.
  try{
    const blist=$("backends").value.split(",").map(s=>s.trim()).filter(Boolean);
    const role_backends={};
    document.querySelectorAll("#roleGrid select").forEach(s=>{if(s.value)role_backends[s.dataset.role]=s.value});
    // #99: +field||default 로 0/NaN 을 가리지 않고 원본 문자열을 그대로 보낸다.
    //      비어있는 선택 필드는 null 로 보내 서버가 무시하게 한다.
    const raw=id=>{const v=$(id).value.trim();return v===""?null:v};
    const body={spec_text,name:$("name").value||f.name.replace(/\.[^.]+$/,""),
      backend:blist[0]||"mock",backends:blist.length>1?blist:null,role_backends,
      distribute:$("distribute").checked,cross_check:$("crossCheck").checked,
      concurrency:raw("concurrency"),
      max_units:raw("maxUnits"),
      max_attempts:raw("maxAttempts"),
      poll_interval:raw("pollInterval"),timeout:raw("timeout"),
      retries:raw("retries"),completion_level:$("completionLevel").value,
      budget:raw("budget"),model:raw("model"),
      mock:$("mock").checked,delegate:$("delegate").checked,full_access:$("fullAccess").checked,
      auto_commit:$("autoCommit").checked};
    const r=await fetchWithTimeout("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)},30000);
    const j=await r.json();
    if(j.error){$("launchMsg").textContent="오류: "+j.error;return}
    await refreshRuns(); selectRun(j.run_id);
  }catch(e){
    $("launchMsg").textContent="오류: "+e;
  }finally{
    $("runBtn").disabled=false;
  }
}
async function refreshRuns(){
  let runs=[];
  // #23/#7: 예전엔 빈 catch(e){} 로 실패를 조용히 삼켜 목록이 갱신 안 돼도 사용자가 몰랐다.
  //         실패를 오류 배너로 노출한다(성공 시 자동 해제는 tick() 가 담당).
  try{runs=((await (await fetchWithTimeout("/api/runs",{},15000)).json()).runs)||[]}
  catch(e){showErr("실행 목록 갱신 실패: "+e)}
  const sel=$("runSel");const prev=CUR;sel.innerHTML="";
  if(!runs.length){const o=document.createElement("option");o.value="";o.text="(실행 없음)";sel.appendChild(o)}
  runs.forEach(r=>{const o=document.createElement("option");o.value=r.id;
    o.text=r.id+(r.running?" ● running":"");sel.appendChild(o)});
  if(prev&&runs.some(r=>r.id===prev))sel.value=prev;
  return runs;
}
function showLaunch(){CUR=null;$("launch").classList.remove("hide");$("dash").classList.add("hide");$("picker").classList.add("hide")}
function showPicker(){CUR=null;$("picker").classList.remove("hide");$("launch").classList.add("hide");$("dash").classList.add("hide")}
function selectRun(id){if(!id)return;CUR=id;$("runSel").value=id;
  $("launch").classList.add("hide");$("picker").classList.add("hide");$("dash").classList.remove("hide");tick();}
function renderPicker(runs){
  const el=$("runList");
  // #66: run id 를 onclick 문자열에 직접 끼우면 따옴표/스크립트 주입 가능.
  //      data-run 속성(esc)으로 담고 addEventListener 로 처리한다.
  if(!runs.length){el.textContent="(실행 없음 — 새 실행을 시작하세요)";return}
  el.innerHTML=runs.map(r=>'<div class="run-item" data-run="'+esc(r.id)+
    '" style="padding:7px 0;cursor:pointer;border-bottom:1px solid #21262d">'+
    (r.running?"🟢 ":"⚪️ ")+esc(r.id)+'</div>').join("");
  el.querySelectorAll(".run-item").forEach(d=>{
    d.addEventListener("click",()=>selectRun(d.dataset.run));
  });
}
async function stopRun(){
  if(!CUR)return;
  if(!confirm("이 run 을 정지할까요?"))return;
  // #7: 예전엔 빈 catch(e){} 로 실패를 조용히 삼켰다. 네트워크/HTTP 오류를 배너로 알리고,
  //     {stopped:false}(정지 대상 없음/실패) 응답도 사용자에게 표시한다.
  try{
    const r=await fetchWithTimeout("/api/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({run:CUR})},15000);
    let j={};try{j=await r.json()}catch(_){}
    if(j&&j.error){showErr("정지 오류: "+j.error)}
    else if(j&&j.stopped===false){showErr("정지할 수 없습니다 — 실행 중이 아니거나 PID 를 찾지 못했습니다.")}
    else{showErr("")}
  }catch(e){showErr("정지 실패: "+e)}
}
async function rerunRun(){
  if(!CUR)return;
  let j={};
  // #M04: 예전엔 빈 catch(e){} 로 네트워크/HTTP 오류를 조용히 삼켜 재실행이 안 돼도 사용자가 몰랐다.
  try{j=await (await fetchWithTimeout("/api/rerun",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({run:CUR})},30000)).json()}
  catch(e){showErr("재실행 실패: "+e);return}
  if(j.run_id){await refreshRuns();selectRun(j.run_id)}
  else if(j.error){showErr("재실행 오류: "+j.error)}
  else{showErr("재실행 실패: 알 수 없는 응답")}
}

function statusDot(s){return '<span class="dot'+(s==="running"?" run":"")+'"></span>'}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function fetchWithTimeout(url, opts={}, ms=30000){
  const ctrl=new AbortController();
  const t=setTimeout(()=>ctrl.abort(),ms);
  try{return await fetch(url,{...opts,signal:ctrl.signal})}
  finally{clearTimeout(t)}
}
// #136: 손상된 board.json 의 비숫자 cost/tokens(문자열/null/객체)가 와도 toFixed/
//       toLocaleString 이 throw 해 tick() 의 catch 가 이를 삼키고 대시보드가 조용히
//       멈추는 것을 막는다. Number(...) 가 NaN 이면 0 으로 강제한다.
function num(v){const n=Number(v);return Number.isFinite(n)?n:0}
// #23: 오류 배너 토글 — 빈 메시지면 숨긴다.
function showErr(msg){const e=$("err");if(!e)return;
  if(msg){e.textContent="⚠ "+msg;e.style.display=""}else{e.textContent="";e.style.display="none"}}
async function tick(){
  if(!CUR)return;
  try{
    // #23: 응답 객체를 보존해 status/에러 본문을 검사한다(조용히 삼키지 않음).
    const r=await fetchWithTimeout("/api/state?run="+encodeURIComponent(CUR),{},15000);
    const s=await r.json();
    if(s&&s.error){showErr("상태 조회 오류: "+s.error+(r.status===401?" — 인증 필요: 먼저 /?token=<TOKEN> 으로 한 번 접속해 인증 쿠키를 설정하면 대시보드가 동작합니다":""));return}
    // #12: board.json 이 손상된 경우 미초기화처럼 보이지 않고 명확히 "board 손상" 을 표시한다.
    if(s&&s.corrupt){showErr("board.json 손상 — 읽을 수 없습니다 (실행 상태를 표시할 수 없습니다).");return}
    showErr("");  // 성공 시 이전 오류 제거
    const b=s.board||{};const ag=b.agents||{};const proj=s.project_dir||"";
    const units=Array.isArray(b.units)?b.units:[];
    const warns=Array.isArray(b.warnings)?b.warnings:[];
    const globalArts=Array.isArray(b.artifacts)?b.artifacts:[];
    const done=units.filter(u=>u&&(u.status==="done"||u.status==="tested")).length;
    $("projDir").textContent=proj||"—";
    $("phase").textContent=b.phase||"—";
    $("cost").textContent="$"+num(b.total_cost_usd).toFixed(4)+(b.cost_estimated?" est.":"");
    $("tok").textContent=num(b.total_tokens).toLocaleString();
    $("units").textContent=done+"/"+units.length;
    const runN=(s.roles||[]).filter(r=>(ag[r]||{}).status==="running").length;
    $("runCount").textContent=runN+"개";
    // 3-state: 실행중 / 완료(done, 경고 있으면 ⚠) / 중단(stopped)
    $("running").textContent=s.running?"running"
      :(b.phase==="done"?(warns.length?("⚠ done ("+warns.length+" 경고)"):"✅ done"):"⏹ stopped");
    $("stopBtn").style.display=s.running?"":"none";       // 실행 중에만 정지
    $("rerunBtn").style.display=s.running?"none":"";      // 정지/완료 상태에서만 재실행
    // 에이전트 카드 — 각 카드에 모델·비용·유닛 + 실시간 로그(프롬프트·스트리밍·결과)
    const logs=s.agent_logs||{};
    $("agentCards").innerHTML=(s.roles||[]).map(r=>{const a=ag[r]||{};
      const run=a.status==="running";
      const meta=[(a.model||a.backend||"—"),"$"+num(a.cost_usd).toFixed(4)+(a.cost_est?" est.":""),
        (a.tokens?num(a.tokens).toLocaleString()+" tok":null),"calls "+(a.calls||0),
        (a.current_unit&&a.current_unit!=="global")?("unit "+a.current_unit):null].filter(Boolean).join(" · ");
      return '<div class="agent-card'+(run?" run":"")+'"><h5>'+statusDot(a.status)+esc(r)+
        '<span class="badge">'+esc(a.status||"idle")+'</span></h5><div class="meta">'+esc(meta)+
        '</div><pre>'+esc(logs[r]||"(대기 중)")+'</pre></div>'}).join("");
    document.querySelectorAll("#agentCards pre").forEach(p=>{p.scrollTop=p.scrollHeight});
    // 통합 로그 — 자동 스크롤
    const log=$("liveLog");const atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-30;
    log.textContent=s.events||"(로그 대기 중…)";
    if(atBottom)log.scrollTop=log.scrollHeight;
    // 산출물 — 경고(있으면) + 설계·공통 + unit별 (저장 경로)
    let html="";
    if(warns.length)html+="<div style='color:#f85149;margin-bottom:6px'>⚠ "+warns.map(esc).join("<br>⚠ ")+"</div>";
    html+="<div>보드/런상태: <b style='user-select:all'>"+esc(proj)+"/.orchestrator/</b></div>";
    if(globalArts.length)html+="<div style='margin-top:6px'><b>설계·공통</b><br>"+globalArts.map(a=>"&nbsp;&nbsp;"+esc(proj)+"/"+esc(a)).join("<br>")+"</div>";
    units.forEach(u=>{if(!u)return;const arts=Array.isArray(u.artifacts)?u.artifacts:[];if(arts.length){
      html+="<div style='margin-top:6px'><b>"+esc(u.id)+"</b> ("+esc(u.status)+", "+arts.length+" files)<br>"+
        arts.map(a=>"&nbsp;&nbsp;"+esc(proj)+"/"+esc(a)).join("<br>")+"</div>"}});
    $("artifacts").innerHTML=html;
  }catch(e){showErr("상태 조회/표시 실패: "+e)}  // #23: 네트워크/파싱 오류도 표시
}
let _tk=0;
let _looping=false;
async function loop(){
  // #11: 탭이 숨겨져 있으면(다른 탭/최소화) 폴링을 건너뛴다 — 보이지 않는 화면 갱신을 위해
  //      서버/네트워크를 두드릴 필요가 없다(부하 감소). 탭이 다시 보이면 다음 틱부터 재개.
  if(document.hidden)return;
  // #64: 이전 loop(tick 포함)가 끝나기 전에 다음 틱이 겹치면 stale 렌더가 날 수 있다.
  //      플래그로 중첩을 막고, tick() 을 반드시 await 한다.
  if(_looping)return;
  _looping=true;
  try{
    if(_tk++%3===0){const runs=await refreshRuns();if(!CUR)renderPicker(runs);}  // 목록/picker 갱신
    if(CUR)await tick();                                                          // 매 초 실시간 갱신
  }finally{
    _looping=false;
  }
}
async function init(){
  await loadChecks();
  const runs=await refreshRuns();
  renderPicker(runs);
  showPicker();   // 자동선택 안 함 — 사용자가 run 을 선택해야 보인다
  setInterval(loop,1000);
}
init();
</script>
</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
