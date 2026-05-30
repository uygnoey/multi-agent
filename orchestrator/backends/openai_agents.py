"""② OpenAI Agents SDK 백엔드 (API 키 방식).

pip install openai-agents / 인증 OPENAI_API_KEY.
내장 파일/배시 툴이 없으므로 타깃 cwd 로 스코프 한정한 function_tool 을 직접 제공한다.

환경변수:
- OPENAI_PRICING_FILE: 비용 추정 단가표 JSON 경로 (없으면 코드 fallback).
- ORCH_OPENAI_ALLOW_BASH: 기본 활성. 0/false/no/off 로 끄면 Bash 역할이라도 run_bash 를
  툴셋에서 제거한다 — in-process FS 격리가 불가능한 잠긴 배포용 정직한 옵트아웃(#1).
"""

from __future__ import annotations

import asyncio
import errno
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from .base import Backend, RoleRequest, RoleResult


def _extract_tokens(result) -> int | None:
    """Runner 결과에서 total token 사용량을 best-effort 로 뽑는다 (SDK 버전 차이에 견고).

    구버전/신버전 모두 대응: ① context_wrapper.usage.total_tokens
    ② raw_responses[*].usage(input/output) 합산. 없으면 None.
    """
    # ① context_wrapper.usage.total_tokens (최신 SDK)
    try:
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        total = getattr(usage, "total_tokens", None)
        if total:
            return int(total)
        it = getattr(usage, "input_tokens", None)
        ot = getattr(usage, "output_tokens", None)
        if it is not None or ot is not None:
            return int((it or 0) + (ot or 0))
    except Exception:
        pass
    # ② raw_responses[*].usage 합산
    try:
        total = 0
        found = False
        for resp in getattr(result, "raw_responses", None) or []:
            u = getattr(resp, "usage", None)
            if u is None:
                continue
            found = True
            tt = getattr(u, "total_tokens", None)
            if tt:
                total += int(tt)
            else:
                total += int(getattr(u, "input_tokens", 0) or 0)
                total += int(getattr(u, "output_tokens", 0) or 0)
        if found:
            return total or None
    except Exception:
        pass
    return None


def _extract_model(result) -> str | None:
    """#10: Runner 결과에서 실제 사용된 모델명을 best-effort 로 뽑는다 (SDK 형태에 견고).

    호출부가 req.model 을 고정하지 않으면 SDK 가 고른 실제 모델을 알 수 없어 비용 추정이
    영원히 None 으로 남는다. raw_responses[*].model 등을 guarded getattr 로 best-effort
    캡처한다(첫 비어있지 않은 값 채택). 끝내 알 수 없으면 None — 가짜 모델명을 날조하지 않는다.
    """
    # ① raw_responses[*].model (가장 신뢰할 수 있는 실제 응답 모델)
    try:
        for resp in getattr(result, "raw_responses", None) or []:
            m = getattr(resp, "model", None)
            if m:
                return str(m)
    except Exception:
        pass
    # ② last_agent.model 이 문자열로 박혀 있으면 그것을 사용 (객체/None 은 무시)
    try:
        m = getattr(getattr(result, "last_agent", None), "model", None)
        if isinstance(m, str) and m:
            return m
    except Exception:
        pass
    return None


def _extract_io_tokens(result) -> tuple[int, int] | None:
    """#8: input/output 토큰을 분리 추출 (비용 추정용). 합산만 가능하면 None.

    가격이 input/output 단가가 다르므로, 정직한 추정을 위해 분리값이 있을 때만 추정한다.
    """
    # ① context_wrapper.usage
    try:
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        it = getattr(usage, "input_tokens", None)
        ot = getattr(usage, "output_tokens", None)
        if it is not None or ot is not None:
            return int(it or 0), int(ot or 0)
    except Exception:
        pass
    # ② raw_responses[*].usage 합산 (input/output 별도 누적)
    try:
        in_t = 0
        out_t = 0
        found = False
        for resp in getattr(result, "raw_responses", None) or []:
            u = getattr(resp, "usage", None)
            if u is None:
                continue
            i = getattr(u, "input_tokens", None)
            o = getattr(u, "output_tokens", None)
            if i is None and o is None:
                continue
            found = True
            in_t += int(i or 0)
            out_t += int(o or 0)
        if found:
            return in_t, out_t
    except Exception:
        pass
    return None


# #8: OpenAI 모델 단가(1M 토큰당 USD): [input, output]. CLI 백엔드처럼 토큰×단가로
# 비용을 추정해 대시보드/리포트가 OpenAI 사용량을 0 으로 과소표시하지 않게 한다.
# 가격 변동 대응: 환경변수 OPENAI_PRICING_FILE 로 외부 JSON 을 지정할 수 있다.
# 모델이 표에 없으면 추정하지 않고 cost=None 으로 둔다(허위 비용 날조 금지).
_OPENAI_FALLBACK_PRICING = {
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.4": (2.5, 15.0),
    "gpt-5.4-mini": (0.75, 4.5),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "o4-mini": (1.1, 4.4),
}

# #7(audit7): model 키 뒤에 붙는 날짜 스냅샷 접미사: '-2024', '-2024-08', '-2024-08-06' 등.
# codex_cli 와 동일한 규칙으로, dated 스냅샷명을 base 모델로 매핑할 때만 쓴다.
_OPENAI_DATE_SUFFIX = re.compile(r"^-\d{4}(?:-\d{2}){0,2}$")


def _openai_pricing() -> dict:
    """OPENAI_PRICING_FILE(있으면) → 코드 fallback. 값은 [input, output] (1M 토큰당 USD)."""
    import json

    path = os.environ.get("OPENAI_PRICING_FILE")
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            out = {}
            for k, v in data.items():
                if k.startswith("_") or not isinstance(v, (list, tuple)) or len(v) < 2:
                    continue
                prices = (float(v[0]), float(v[1]))
                if any((not math.isfinite(x)) or x < 0 for x in prices):
                    continue
                out[k] = prices
            if out:
                return out
        except Exception:
            pass
    return dict(_OPENAI_FALLBACK_PRICING)


def _openai_price_for(model: str | None):
    """#7(audit7): 모델명에 맞는 단가 (input, output) 를 찾는다. 없으면 None.

    ① 정확 매칭 우선('gpt-4o-mini' 는 'gpt-4o' 가 아니라 'gpt-4o-mini' 로만 매칭).
    ② 날짜 접미사만 붙은 dated 스냅샷('gpt-4o-2024-08-06')은 base 모델('gpt-4o')로 매핑.
       codex_cli._price_for 와 동일한 규칙 — dated snapshot 이름이 비용 추정에서 누락돼
       cost 가 None 으로 떨어지던 문제를 막는다. (긴 키 우선 매칭으로 'gpt-4o-mini' 같은
       더 구체적인 base 가 'gpt-4o' 보다 먼저 시도된다.)
    ③ 그 외 알 수 없는 변형은 None(허위 비용 날조 금지).
    """
    m = (model or "").lower().strip()
    if not m:
        return None
    pricing = _openai_pricing()
    # ① 정확 매칭
    if m in pricing:
        return pricing[m]
    # ② 날짜 접미사 dated 스냅샷만 base 모델로 인정 (긴 키 우선)
    for key in sorted(pricing, key=len, reverse=True):
        if m.startswith(key) and _OPENAI_DATE_SUFFIX.match(m[len(key) :]):
            return pricing[key]
    # ③ 알 수 없는 변형 → 추정치 없음
    return None


def _estimate_openai_cost(model: str | None, in_tokens: int, out_tokens: int):
    """#8: 모델 단가 × 토큰으로 추정 비용(USD). 모델 미지정/단가표 미등록이면 None.

    정확 매칭을 우선하되, 날짜 스냅샷 접미사('-YYYY-MM-DD')만 붙은 dated 모델명은
    base 모델 단가로 폴백한다(#7, audit7) — codex_cli 와 동일. 임의 prefix 매칭은 하지
    않으므로 알 수 없는 변형을 오과금하지 않는다.
    """
    p = _openai_price_for(model)
    if not p:
        return None
    in_price, out_price = p
    cost = (in_tokens or 0) / 1e6 * in_price + (out_tokens or 0) / 1e6 * out_price
    return round(cost, 6)


# #1: 잠긴(locked-down) 배포에서 Bash 역할이라도 run_bash 노출을 끄는 정직한 옵트아웃.
# 기본은 켜짐(기존 동작 보존). 0/false/no/off 면 run_bash 를 툴셋에서 제거한다.
# (in-process FS 격리는 불가능한 근본 제약이므로 — 가짜 path-filter 대신 노출 자체를 차단.)
def _bash_enabled() -> bool:
    """ORCH_OPENAI_ALLOW_BASH 가 0/false/no/off 가 아니면 True (기본 활성)."""
    v = os.environ.get("ORCH_OPENAI_ALLOW_BASH")
    if v is None:
        return True
    return v.strip().lower() not in ("0", "false", "no", "off", "")


class _BashCapture:
    """배시 출력을 백그라운드 스레드에서 비우되, 보관량을 상한(bytes)으로 묶는 버퍼.

    #2/#36: stdout 을 별도 스레드가 계속 소비해 파이프 버퍼가 가득 차 자식이 블록되는 것을
    막는다. 동시에 메인 스레드는 proc.wait(timeout) 으로 wall-clock 데드라인을 강제할 수
    있다(출력이 전혀 없는 silent 명령도 타임아웃). 상한 초과분은 읽되 버려(drop) 메모리를
    묶는다(거대 출력 OOM 방지).
    """

    def __init__(self, max_bytes: int):
        self._max = max_bytes
        self._buf = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            if len(self._buf) >= self._max:
                self.truncated = True
                return  # 상한 도달 — 이후는 읽되 버린다(소비는 계속해 블록 방지)
            room = self._max - len(self._buf)
            if len(chunk) > room:
                self._buf.extend(chunk[:room])
                self.truncated = True
            else:
                self._buf.extend(chunk)

    def text(self) -> str:
        with self._lock:
            return bytes(self._buf).decode("utf-8", errors="replace")


def _kill_process_group(proc, grace: float = 2.0) -> None:
    """#3/#1: 셸이 spawn 한 자식까지 프로세스 그룹째 종료한다 (좀비/고아 방지).

    SIGTERM → 짧은 유예 → SIGKILL 순서. start_new_session=True 로 만든 새 그룹을
    os.killpg 로 통째 종료한다. 그룹 조회/전송 실패 시 단일 프로세스 kill 로 폴백.

    #1: 부모가 유예 안에 graceful 종료하더라도, SIGTERM 을 무시한 채 살아남은 그룹 내
    자식(부모 셸은 종료) 이 고아로 남을 수 있다. 그래서 마지막에 항상 그룹 SIGKILL 을 한 번
    더 쓸어(try/except) 잔존 자식을 일소한다 — 부모 graceful/무응답 두 경로 모두 그룹
    SIGKILL 으로 끝나야 호출 후 살아남는 자식이 없다(그룹이 이미 비었으면 무해).
    """
    if proc is None:
        return
    pid = getattr(proc, "pid", None)
    pgid = None
    if pid is not None:
        try:
            pgid = os.getpgid(pid)
        except Exception:
            pgid = None
    # 1) SIGTERM (그룹 우선, 실패 시 단일)
    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    # 2) 짧은 유예 동안 자발적 종료 대기 (예외/타임아웃이어도 아래 SIGKILL 스윕으로 진행)
    parent_exited = False
    try:
        proc.wait(timeout=grace)
        parent_exited = True
    except Exception:
        pass
    # 3) 부모가 아직 살아있으면 SIGKILL 로 그룹째 강제 종료
    if not parent_exited:
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=grace)
        except Exception:
            pass
    # 4) #1: 부모 종료 여부와 무관하게 그룹 SIGKILL 을 마지막으로 한 번 더 쓸어, SIGTERM 을
    #    무시한 채 살아남은 자식(straggler)을 반드시 일소한다. 그룹이 비었으면 무해(try/except).
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass


def _macos_sandbox_profile(root: str) -> str:
    """macOS sandbox-exec 용 'workspace-write' 프로파일: read/exec/network 는 허용하되 쓰기는
    프로젝트 루트(+시스템 임시 디렉터리)로 제한한다. codex 의 --sandbox workspace-write 와 동급."""

    def esc(p: str) -> str:
        return p.replace("\\", "\\\\").replace('"', '\\"')

    writable = [root, "/private/tmp", "/private/var/folders", "/tmp", "/var/tmp", "/dev"]
    allows = "\n".join(f'  (subpath "{esc(p)}")' for p in writable)
    return f"(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write*\n{allows}\n)\n"


def _bash_command_spec(command: str, root: str, full_access: bool) -> tuple[list[str], str]:
    """run_bash 권한 2-tier 의 argv/경고를 결정한다(부수효과는 which 조회뿐).

    - full_access=True  → 격리 없이 머신 전역으로 실행(진짜 컴퓨터 전체 접근, --full-access).
    - full_access=False → 프로젝트 폴더 밖 '쓰기'를 막는 OS 샌드박스로 감싼다(기본 정책).
        · macOS: sandbox-exec(workspace-write 프로파일)
        · Linux: bwrap(전체 ro-bind + 루트/tmp 만 rw) 가 있으면 사용
        · 둘 다 없으면 best-effort 로 그대로 실행하되 경고 note 를 출력에 접두한다.
    반환: (argv, note). note 가 비어있지 않으면 경계 강제가 불가능했음을 뜻한다.
    """
    base = ["/bin/sh", "-c", command]
    if full_access:
        return base, ""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return ["sandbox-exec", "-p", _macos_sandbox_profile(root), *base], ""
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            root,
            root,
            "--bind",
            "/tmp",
            "/tmp",
            "--die-with-parent",
            *base,
        ], ""
    return base, (
        "[warn] FS 샌드박스 미사용: 이 플랫폼에 sandbox-exec/bwrap 가 없어 프로젝트 폴더 쓰기 "
        "제한을 강제하지 못했습니다(best-effort). 진짜 격리는 컨테이너/OS 레벨에서만 가능.\n"
    )


# #audit15: run_bash 가 상속하는 환경에서 비밀 값을 제거한다. 에이전트 셸은 오케스트레이터의
# API 키/토큰이 필요 없고, 악성/오작동 명령이 env(또는 /proc/self/environ)로 키를 읽어 유출할
# 수 있다. PATH/HOME/TMPDIR 등 기본 동작 변수는 유지(allow), 비밀 패턴만 제거(block).
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_SECRET_KEY", "_PASSWORD", "_ACCESS_KEY")
_SECRET_ENV_PREFIXES = ("AWS_", "AZURE_", "GCP_")
_SECRET_ENV_EXACT = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"}


def _scrubbed_bash_env() -> dict[str, str]:
    """오케스트레이터 env 에서 비밀 값을 제거한 사본을 반환 (#audit15)."""
    env: dict[str, str] = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if (
            ku in _SECRET_ENV_EXACT
            or any(ku.endswith(s) for s in _SECRET_ENV_SUFFIXES)
            or any(ku.startswith(p) for p in _SECRET_ENV_PREFIXES)
        ):
            continue
        env[k] = v
    return env


def _run_bash_command(
    command: str, cwd: str, timeout: float, max_capture: int, full_access: bool = False
) -> str:
    """#2/#3: 셸 명령을 wall-clock 타임아웃·바운디드 버퍼·프로세스그룹 종료로 실행.

    권한 2-tier(확정 설계): 기본(full_access=False)은 프로젝트 폴더 밖 쓰기를 막는 OS
    샌드박스로 감싸 실행하고, full_access=True(--full-access)면 가두지 않고 머신 전역으로
    실행한다. argv/머신선택은 _bash_command_spec 가 결정한다.

    - start_new_session=True 로 새 프로세스 그룹을 만들어 자식까지 그룹째 정리한다(#3).
    - 백그라운드 스레드가 stdout 을 max_capture 까지만 보관(초과는 drop)하며 계속 소비해
      파이프 블록을 막는다(#36). 메인은 proc.wait(timeout) 으로 데드라인을 강제하므로
      출력이 전혀 없는 silent 명령(sleep 100 등)도 정확히 타임아웃된다(#2).
    - 반환: 정상 종료면 ``[exit <code>]\\n<본문>``, 타임아웃이면 ``[timeout]\\n<본문>``.
    """
    proc = None
    drainer = None
    cap = _BashCapture(max_capture)
    argv, sandbox_note = _bash_command_spec(command, cwd, full_access)
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            # #audit21: stdin 을 DEVNULL 로 명시. 미지정 시 자식이 부모 stdin 을 상속해
            # read 호출 시 부모 tty/오케스트레이터 stdin 에서 입력을 기다리며 hang 한다.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # stdout+stderr 를 한 스트림으로 합쳐 순서 보존
            start_new_session=True,  # #3: 새 프로세스 그룹 → 자식까지 그룹째 종료 가능
            env=_scrubbed_bash_env(),  # #audit15: 비밀 env 제거 후 실행(키 유출 표면 축소)
        )

        def _drain() -> None:
            # #2/#36: 바이너리로 청크 단위 소비 — 상한 도달 후에도 계속 읽어 버려 블록 방지.
            try:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    cap.feed(chunk)
            except Exception:
                pass

        drainer = threading.Thread(target=_drain, daemon=True)
        drainer.start()

        timed_out = False
        try:
            rc = proc.wait(timeout=timeout)  # #2: 출력 유무와 무관하게 데드라인 강제
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = None
            _kill_process_group(proc)  # #3: 그룹째 종료
        # #audit21: 정상/비정상 경로를 분리해 drainer 정리. (Codex 재검증 보정)
        # - 정상 종료: 자식이 stdout 을 close 했으므로 drainer 가 EOF 로 자연 종료한다.
        #   먼저 drainer.join 으로 데이터 캡처 손실 없이 마무리하고, 혹시(escaped child 가
        #   PIPE write-end 를 들고 있는 드문 케이스) hang 되면 그 때만 raw fd close 로 풀어준다.
        # - timeout: 부모는 죽었으나 escaped child(setsid 등)가 stdout fd 를 상속해 유지하면
        #   EOF 가 안 와 drainer 가 hang. 즉시 os.close(fileno) 로 강제 EBADF.
        #   BufferedReader.close() 는 lock 충돌로 자체가 block 될 수 있어 raw fd close 가
        #   결정적이다(python3 preexec_fn=os.setsid 재현: 1차 수정 25.79s → 보정 후 2.01s).
        if timed_out:
            try:
                if proc.stdout is not None:
                    os.close(proc.stdout.fileno())
            except Exception:
                pass
            drainer.join(timeout=1.0)
        else:
            drainer.join(timeout=2.0)
            if drainer.is_alive():  # escaped child PIPE 보유 — fallback
                try:
                    if proc.stdout is not None:
                        os.close(proc.stdout.fileno())
                except Exception:
                    pass
                drainer.join(timeout=1.0)

        captured = cap.text()
        out = captured[:4000]
        trunc = "\n<... output truncated>" if cap.truncated or len(captured) > 4000 else ""
        body = f"{sandbox_note}{out}{trunc}"
        if timed_out:
            return f"[timeout]\n{body}"
        return f"[exit {rc}]\n{body}"
    except Exception as e:
        _kill_process_group(proc)
        # #audit21: 예외 경로도 동일하게 raw fd close 로 drainer 를 풀어준다(위와 동일 사유).
        try:
            if proc is not None and proc.stdout is not None:
                os.close(proc.stdout.fileno())
        except Exception:
            pass
        if drainer is not None:
            drainer.join(timeout=1.0)
        return f"<bash error: {e}>"


# #3: edit_file 은 유일성 치환을 위해 파일 전체 내용이 필요하다. 거대 파일을 read_text() 로
# 통째 올리면 메모리가 폭주하므로, 읽기 전에 파일 크기를 먼저 검사한다. 상한(read 와 동일한
# ~200KB) 초과 파일은 로드하지 않고 거부하고 write_file 사용을 안내한다(편집은 전체 내용 필요).
_MAX_EDIT_BYTES = 200 * 1024


def _edit_too_large(size_bytes: int, cap: int = _MAX_EDIT_BYTES) -> bool:
    """#3: 편집 대상 파일 바이트 크기가 상한을 초과하면 True (읽기 전 size 가드, 순수 함수)."""
    return size_bytes > cap


def _edit_file_text(content: str, old_string: str, new_string: str) -> str:
    """#13: 타깃 부분 치환을 수행하고 새 파일 내용을 반환한다 (순수 함수, SDK 불필요).

    - old_string 이 정확히 1회만 등장해야 한다(patch 처럼 유일성 요구). 0회면 명확한 에러,
      2회 이상이면 모호하므로 거부한다 — 잘못된 위치 치환으로 파일이 깨지는 것을 방지.
    - 실패 시 ``ValueError`` 를 던진다(호출부가 에러 문자열로 변환).
    """
    if not old_string:
        # 빈 old_string 은 "전체를 치환"으로 오인되기 쉬워 거부(생성/덮어쓰기는 Write 사용).
        raise ValueError("old_string is empty — use write_file to create/overwrite")
    count = content.count(old_string)
    if count == 0:
        raise ValueError("old_string not found in file")
    if count > 1:
        raise ValueError(f"old_string is not unique ({count} matches) — add more context")
    return content.replace(old_string, new_string, 1)


# #4: list_dir 이 node_modules/.venv 처럼 항목이 수만 개인 디렉터리를 정렬해 통째 문자열로
# 반환하면 컨텍스트가 폭주한다. 정렬 후 앞쪽 일부(기본 500개)만 반환하고, 잘렸으면 남은 개수를
# "... (N more)" 로 안내한다.
_MAX_LIST_ENTRIES = 500


def _format_dir_listing(names: list[str], cap: int = _MAX_LIST_ENTRIES) -> str:
    """#4: 정렬된 항목 이름 리스트를 상한까지만 합쳐 반환(초과 시 '... (N more)' 안내).

    순수 함수(SDK 불필요): 입력 names 를 정렬해 앞쪽 cap 개만 줄바꿈 연결한다.
    """
    ordered = sorted(names)
    if len(ordered) <= cap:
        return "\n".join(ordered)
    head = ordered[:cap]
    remaining = len(ordered) - cap
    return "\n".join(head) + f"\n... ({remaining} more)"


# #1(audit7): symlink TOCTOU 방어용 순수 헬퍼.
# _safe() 가 resolve 시점에 root 안임을 확인하더라도, resolve 와 실제 open/write 사이에
# (같은 세션 run_bash 가 만든) symlink 가 경로를 root 밖으로 재지정할 수 있다. 그래서 실제
# 파일 접근 직전에 os.path.realpath(p) 를 root(역시 재-resolve) 기준으로 다시 검사한다.
# os.O_NOFOLLOW 까지 함께 쓰면 최종 컴포넌트가 symlink 면 open 자체가 거부된다(아래 read/write).
def _resolve_under_root(p, root) -> bool:
    """p 의 realpath 가 root(재-resolve) 안(또는 root 자신)이면 True. 탈출하면 False.

    순수 함수(SDK 불필요): TOCTOU 윈도우를 닫기 위해 syscall 직전에 다시 호출한다.
    중간 컴포넌트 symlink 까지 풀어 비교하므로 디렉터리 symlink 우회도 막는다.
    """
    try:
        real_p = Path(os.path.realpath(str(p)))
        real_root = Path(os.path.realpath(str(root)))
    except Exception:
        return False
    return real_p == real_root or real_root in real_p.parents


def _is_eloop(err: OSError) -> bool:
    return getattr(err, "errno", None) == errno.ELOOP


def _symlink_target_under_root(p: Path, root: Path) -> Path | None:
    """Return the fully resolved target if a final symlink still points inside root."""
    try:
        target = Path(os.path.realpath(str(p)))
        real_root = Path(os.path.realpath(str(root)))
    except Exception:
        return None
    if target == real_root or real_root in target.parents:
        return target
    return None


def _open_inside(path: Path, root: Path, flags: int, mode: int = 0o644) -> int:
    """Open path with O_NOFOLLOW; if it is an internal symlink, open its resolved target."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(str(path), flags | nofollow, mode)
    except OSError as e:
        if not _is_eloop(e):
            raise
        target = _symlink_target_under_root(path, root)
        if target is None:
            raise
        return os.open(str(target), flags | nofollow, mode)


def _read_file_bytes_under_root(path: Path, root: Path, max_bytes: int) -> bytes:
    fd = _open_inside(path, root, os.O_RDONLY)
    # 디렉터리 대상이면 os.fdopen(fd,"rb") 가 fd 인계 전에 IsADirectoryError 로 실패해
    # with 가 닫지 못한 fd 가 누수된다 → fdopen 성공 전 예외에서 fd 를 명시적으로 닫는다.
    try:
        fh = os.fdopen(fd, "rb")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    with fh:
        return fh.read(max_bytes + 1)


def _write_file_bytes_under_root(path: Path, root: Path, data: bytes) -> None:
    # #audit23: 원자 쓰기 — O_TRUNC 후 직접 write 는 크래시/ENOSPC 시 부분 파일이 남는다.
    # 같은 디렉터리에 tmp 로 쓰고 flush+fsync 후 os.replace 로 교체 (board._flush 와 동일 정책).
    #
    # 보안 동작 보존: path 가 root 안 symlink 면 _open_inside 의 ELOOP fallback 과 동일하게
    # _symlink_target_under_root 로 root 안 target 을 얻어 거기에 atomic 쓰기 한다(기존 동작).
    # root 밖 target 은 거부(ELOOP 재던짐).
    #
    # #audit23-amend (Codex 보안 검증 보정): tmp 경로에 _open_inside 의 ELOOP redirect 를
    # 적용하면, 공격자가 사전에 predictable ``path.tmp`` 위치에 root 내부 victim 을 가리키는
    # symlink 를 심어두면 _symlink_target_under_root 로 redirect 되어 victim 을 덮어쓰고
    # 최종 os.replace 가 target 을 그 symlink 로 만들었다(Codex 재현).
    # 해결: tmp 는 ``tempfile.mkstemp(dir=actual.parent)`` 로 random name + O_EXCL|O_NOFOLLOW
    # 동등 효과로 직접 생성 — predictable symlink 선점 공격 원천 차단.
    # actual.parent 가 root 안인지 별도로 검증(symlink redirect 가 root 밖으로 새는 일 차단).
    import errno as _errno
    import tempfile as _tempfile

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    actual = path
    try:
        probe_fd = os.open(str(path), os.O_RDONLY | nofollow)
        os.close(probe_fd)
    except OSError as e:
        if e.errno == _errno.ENOENT:
            pass  # 새 파일 — actual=path 유지
        elif _is_eloop(e):
            target = _symlink_target_under_root(path, root)
            if target is None:
                raise  # root 밖 symlink — 거부
            actual = target
        else:
            raise

    # actual.parent 가 root 안인지 명시 검증(redirect 후에도 root 밖에 tmp 생성 차단).
    if not _resolve_under_root(actual.parent, root):
        raise OSError(
            f"target parent escapes project root: {actual.parent} (root={root})"
        )

    fd, tmp_str = _tempfile.mkstemp(
        prefix=actual.name + ".", suffix=".tmp", dir=str(actual.parent)
    )
    tmp = Path(tmp_str)
    try:
        # mkstemp 은 0o600 으로 생성한다. 기존 _write_file_bytes_under_root 가 0o644 로
        # 만들던 동작과 일치시키려면 fchmod 가 필요하나, 단일 사용자 도구 전제상 0o600 도
        # 안전하고 더 보수적이라 그대로 유지(다른 사용자 read 차단).
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, actual)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    # 디렉터리 fsync (rename 메타데이터 영속화, best-effort)
    try:
        dir_fd = os.open(str(actual.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _resolve_tools(requested, tool_map: dict, read_list_fallback: list) -> list:
    """#2(audit7): 역할의 allowed_tools → 실제 노출할 툴 리스트로 해석한다 (순수 함수, SDK 불필요).

    핵심 규칙(권한 상승 방지): read/list 폴백은 allowed_tools 가 애초에 비어/미지정일 때만
    적용한다. allowlist 가 비어있지 않은데(예: ["Bash"]) 옵트아웃 등으로 결과 툴셋이 빈 경우,
    빈 채로 둔다 — 읽기 권한을 요청하지 않은 역할에 read/list 를 몰래 주입하지 않는다.

    - tool_map: 도구명 → [tool, ...] (Bash 옵트아웃이면 tool_map['Bash'] 는 빈 리스트).
    - read_list_fallback: allowlist 가 비어/미지정일 때만 쓰는 안전 폴백([read_file, list_dir]).
    """
    requested = list(requested or [])
    resolved: list = []
    for t in requested:
        for fn in tool_map.get(t, []):
            if fn not in resolved:
                resolved.append(fn)
    if not resolved and not requested:
        # allowlist 가 애초에 비어/미지정 → 안전 폴백(읽기/목록만, bash 제외).
        return list(read_list_fallback)
    # allowlist 가 비어있지 않은데 옵트아웃 등으로 빈 결과 → 폴백 없이 빈 채로(권한 상승 방지).
    return resolved


class OpenAIAgentsBackend(Backend):
    name = "openai-agents"

    def available(self) -> tuple[bool, str]:
        try:
            import agents  # noqa: F401
        except Exception:
            return False, "openai-agents 미설치 (pip install openai-agents)"
        if not os.environ.get("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY 미설정"
        return True, "ready"

    async def run_role(self, req: RoleRequest) -> RoleResult:
        try:
            from agents import Agent, Runner, function_tool
        except Exception as e:  # pragma: no cover
            return RoleResult(ok=False, error=f"import 실패: {e}")

        root = req.cwd.resolve()

        def _safe(rel: str) -> Path:
            p = (root / rel).resolve()
            if root != p and root not in p.parents:
                raise ValueError(f"path escapes project dir: {rel}")
            return p

        # #122/#123: 툴 출력/파일 쓰기 크기 상한 (컨텍스트 폭주·거대 파일 생성 방지).
        max_read_bytes = 200 * 1024  # read 는 ~200KB 까지 (초과 시 절단 안내)
        max_write_bytes = 5 * 1024 * 1024  # write 는 5MB 초과를 거부

        @function_tool
        def read_file(path: str) -> str:
            """파일 내용을 읽는다 (타깃 디렉터리 한정, 최대 ~200KB — 초과분은 절단)."""
            try:
                p = _safe(path)
            except ValueError as e:
                return f"<{e}>"
            if not p.exists():
                return f"<no file: {path}>"
            # #1(audit7): syscall 직전 realpath 재검사 — _safe 이후 symlink 가 끼어들어도 탈출 차단.
            if not _resolve_under_root(p, root):
                return f"<path escapes project dir: {path}>"
            # 디렉터리/특수파일은 읽기 대상이 아니다(디렉터리면 open 후 fd 누수 위험) → 명시적 거부.
            if not p.is_file():
                return f"<not a file: {path}>"
            # #35: 거대 파일을 통째로 메모리에 올린 뒤 자르면 200KB 상한이 메모리를 못 막는다.
            # 바이트 단위로 max_read_bytes+1 까지만 읽어, 초과 여부를 비싸지 않게 판정한다.
            # #1(audit7): O_NOFOLLOW 로 최종 컴포넌트가 symlink 면 open 자체를 거부(TOCTOU 차단).
            try:
                raw = _read_file_bytes_under_root(p, root, max_read_bytes)
            except OSError as e:
                # ELOOP: 최종 컴포넌트가 symlink → 명시적 거부 메시지.
                if _is_eloop(e):
                    return f"<path escapes project dir (symlink): {path}>"
                return f"<read error: {path}: {e}>"
            except Exception as e:
                return f"<read error: {path}: {e}>"
            truncated = len(raw) > max_read_bytes
            data = raw[:max_read_bytes].decode("utf-8", errors="replace")
            if truncated:
                return data + f"\n<... truncated: {path} exceeds {max_read_bytes} bytes>"
            return data

        @function_tool
        def write_file(path: str, content: str) -> str:
            """파일을 생성/'전체 덮어쓰기'한다 (타깃 디렉터리 한정, 5MB 초과 거부).

            Write 툴 전용(전체 내용 전송). 부분 수정은 edit_file(Edit) 을 사용한다.
            """
            try:
                p = _safe(path)
            except ValueError as e:
                return f"<{e}>"
            # #123: 비정상적으로 거대한 content 는 거부한다.
            if len(content.encode("utf-8")) > max_write_bytes:
                return f"<write rejected: content exceeds {max_write_bytes} bytes>"
            # #3(audit7): mkdir/write 를 try 로 감싸 권한/디스크 오류가 SDK 러너로 raise 되지 않고
            #   read_file 처럼 '<write error: ...>' 문자열로 반환되게 한다.
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                # #1(audit7): mkdir(parents=True) 와 실제 쓰기 사이에 symlink 가 끼어 root 밖으로
                #   재지정될 수 있으므로, 부모 디렉터리 생성 직후 realpath 를 다시 검사한다.
                if not _resolve_under_root(p, root):
                    return f"<path escapes project dir: {path}>"
                # #1(audit7): O_NOFOLLOW + 내부 symlink target open 으로 TOCTOU 탈출 차단.
                data = content.encode("utf-8")
                _write_file_bytes_under_root(p, root, data)
            except OSError as e:
                if _is_eloop(e):
                    return f"<path escapes project dir (symlink): {path}>"
                return f"<write error: {path}: {e}>"
            except Exception as e:
                return f"<write error: {path}: {e}>"
            # #11(audit9): len(content) 는 (UTF-8 다바이트 문자에서) 문자 수라 실제 바이트 수와
            # 다르다. 디스크에 쓴 UTF-8 바이트 수(len(data))를 보고한다.
            return f"wrote {path} ({len(data)} bytes)"

        @function_tool
        def edit_file(path: str, old_string: str, new_string: str) -> str:
            """#13: 파일에서 old_string(유일하게 등장) 1회를 new_string 으로 치환한다.

            진짜 부분 패치(타깃 디렉터리 한정, 5MB 초과 거부). old_string 이 없으면 에러,
            여러 번 등장하면 모호하므로 거부한다(더 많은 문맥을 포함하도록 안내). 전체
            생성/덮어쓰기는 write_file(Write) 을 쓴다.
            """
            try:
                p = _safe(path)
            except ValueError as e:
                return f"<{e}>"
            if not p.exists():
                return f"<no file: {path}>"
            # #1(audit7): syscall 직전 realpath 재검사 — _safe 이후 symlink 가 끼어들어도 탈출 차단.
            if not _resolve_under_root(p, root):
                return f"<path escapes project dir: {path}>"
            # 디렉터리/특수파일은 편집 대상이 아니다(디렉터리면 read 시 fd 누수 위험) → 명시적 거부.
            if not p.is_file():
                return f"<not a file: {path}>"
            # #3: read_text() 로 통째 올리기 전에 크기를 먼저 검사 — 거대 파일은 메모리를
            # 폭주시키므로 로드하지 않고 거부한다(편집은 유일성 치환 위해 전체 내용 필요).
            try:
                size = p.stat().st_size
            except Exception as e:
                return f"<read error: {path}: {e}>"
            if _edit_too_large(size):
                return (
                    f"<edit rejected: {path} too large to edit "
                    f"({size} > {_MAX_EDIT_BYTES} bytes); use write_file>"
                )
            try:
                raw = _read_file_bytes_under_root(p, root, _MAX_EDIT_BYTES)
                content = raw.decode("utf-8")
            except OSError as e:
                if _is_eloop(e):
                    return f"<path escapes project dir (symlink): {path}>"
                return f"<read error: {path}: {e}>"
            except Exception as e:
                return f"<read error: {path}: {e}>"
            try:
                updated = _edit_file_text(content, old_string, new_string)
            except ValueError as e:
                return f"<edit rejected: {path}: {e}>"
            # #123: 치환 후 비정상적으로 거대해지면 거부한다.
            if len(updated.encode("utf-8")) > max_write_bytes:
                return f"<edit rejected: result exceeds {max_write_bytes} bytes>"
            # #1(audit7): 쓰기 직전 다시 realpath 검사 + O_NOFOLLOW 로 최종 컴포넌트 symlink 거부.
            # #3(audit7): write 실패(권한/디스크)도 SDK 러너로 raise 되지 않고 에러 문자열로 반환.
            try:
                if not _resolve_under_root(p, root):
                    return f"<path escapes project dir: {path}>"
                data = updated.encode("utf-8")
                _write_file_bytes_under_root(p, root, data)
            except OSError as e:
                if _is_eloop(e):
                    return f"<path escapes project dir (symlink): {path}>"
                return f"<write error: {path}: {e}>"
            except Exception as e:
                return f"<write error: {path}: {e}>"
            # #11(audit9): 문자 수(len(updated)) 가 아니라 디스크에 쓴 UTF-8 바이트 수를 보고한다.
            return f"edited {path} ({len(data)} bytes)"

        @function_tool
        def list_dir(path: str = ".") -> str:
            """디렉터리 목록 (타깃 디렉터리 한정, 정렬 후 최대 ~500개 — 초과는 '... (N more)')."""
            try:
                p = _safe(path)
            except ValueError as e:
                return f"<{e}>"
            if not p.exists():
                return f"<no dir: {path}>"
            # #1(audit7): 목록 직전 realpath 재검사 — symlink 로 root 밖을 들여다보지 못하게.
            if not _resolve_under_root(p, root):
                return f"<path escapes project dir: {path}>"
            # #6(audit9): 항목 하나의 is_dir() 가 (깨진 symlink/권한 등으로) 실패해도 목록 전체가
            # '<list error>' 로 무너지지 않게 한다 — 가능한 항목은 나열하고, 판정 불가 항목은
            # 이름만 표기한다(접미사 '/' 없이). iterdir() 자체 실패만 치명적 에러로 처리한다.
            try:
                entries = list(p.iterdir())
            except Exception as e:
                return f"<list error: {path}: {e}>"
            names = []
            for x in entries:
                try:
                    names.append(x.name + ("/" if x.is_dir() else ""))
                except Exception:
                    names.append(x.name)
            # #4: node_modules/.venv 같은 거대 디렉터리도 상한까지만 반환해 컨텍스트 폭주 방지.
            return _format_dir_listing(names)

        # #7(audit9): req.timeout==0 은 falsy 라 예전엔 120 으로 둔갑했다. None(미지정)일 때만
        # 기본 120 을 쓰고, 0 을 포함한 명시값은 그대로 따른다.
        bash_timeout = req.timeout if req.timeout is not None else 120
        # #36: 반환 텍스트는 어차피 4000자로 자르므로, 보관하는 양도 상한선으로 묶는다.
        max_bash_capture = 64 * 1024  # 보관 상한(~64KB) — 4000자 절단보다 넉넉

        @function_tool
        def run_bash(command: str) -> str:
            """셸 명령 실행 (cwd=타깃).

            권한 2-tier(확정 설계): 기본은 프로젝트 폴더 밖 '쓰기'를 막는 OS 샌드박스
            (macOS sandbox-exec / Linux bwrap)로 감싸 실행한다 — 프로젝트 폴더 안에서는 자유롭게
            읽기/쓰기/실행하되 타깃 밖 파일은 변조할 수 없다. req.full_access(--full-access)면
            샌드박스 없이 머신 전역으로 실행한다(진짜 컴퓨터 전체 접근). 샌드박스 도구가 없는
            플랫폼에서는 best-effort 로 실행하되 출력에 [warn] 경고를 접두한다. 노출 자체는 역할의
            allowed_tools(Bash) + 환경변수 ORCH_OPENAI_ALLOW_BASH(기본 활성)로 제어된다.

            #2: 출력이 전혀 없는 silent 명령(sleep 100 등)도 wall-clock 타임아웃되고, #3: 셸이
            spawn 한 자식까지 프로세스 그룹째 종료된다(고아/좀비 방지). 정상 종료는 ``[exit N]``,
            타임아웃은 ``[timeout]`` 접두로 반환한다.
            """
            return _run_bash_command(
                command, str(root), bash_timeout, max_bash_capture, req.full_access
            )

        # 역할의 allowed_tools 만 노출 (다른 백엔드의 --allowedTools 와 동일한 격리).
        # #13: Edit → edit_file(진짜 부분 패치). Write → write_file(전체 덮어쓰기).
        tool_map = {
            "Read": [read_file, list_dir],
            "Write": [write_file],
            "Edit": [edit_file],
            "Bash": [run_bash],
        }
        # #1: ORCH_OPENAI_ALLOW_BASH 가 꺼져 있으면 Bash 역할이라도 run_bash 를 노출하지 않는다.
        if not _bash_enabled():
            tool_map["Bash"] = []
        # #2(audit7): 폴백(읽기/목록)은 allowed_tools 가 애초에 비어/미지정일 때만 적용한다.
        # allowlist 가 비어있지 않은데(예: ["Bash"]) bash 옵트아웃으로 빈 결과가 되면, read/list
        # 를 몰래 주입하지 않고 빈 채로 둔다 — 읽기를 요청하지 않은 역할에 권한 상승 금지.
        tools = _resolve_tools(req.allowed_tools, tool_map, [read_file, list_dir])

        kwargs = dict(name=req.role, instructions=req.system_prompt, tools=tools)
        if req.model:
            kwargs["model"] = req.model
        # #22(#114): OpenAI Agents SDK 에는 per-run 예산 캡 옵션이 없어 req.budget 강제는 미지원
        # — 검증 결과 SDK 에 해당 인자가 없는 근본 제약(KEEP-DOCUMENTED). max_turns 만 호출
        # 안전장치로 전달한다(SDK 가 지원). 누적 예산은 상위 runner 에서 사전 체크로 처리한다.
        agent = Agent(**kwargs)
        try:
            result = await asyncio.wait_for(
                Runner.run(agent, req.prompt, max_turns=req.max_turns), timeout=req.timeout
            )
        except asyncio.TimeoutError:
            return RoleResult(ok=False, error=f"openai-agents timed out after {req.timeout}s")
        except Exception as e:
            return RoleResult(ok=False, error=str(e))
        # #46: model/tokens/cost 를 best-effort 로 캡처 (Runner 결과 형태에 따라 guard).
        tokens = _extract_tokens(result)
        # #10: 호출부가 모델을 고정하지 않으면 SDK 가 고른 실제 모델을 결과에서 캡처해, 보고
        # 모델과 비용 추정 둘 다에 쓴다(fallback req.model). 끝내 알 수 없으면 None.
        model = _extract_model(result) or req.model
        # #8: input/output 토큰이 분리되고 모델 단가를 알면 비용을 추정한다(추정치 표시).
        cost = None
        cost_estimated = False
        io = _extract_io_tokens(result)
        if io is not None:
            est = _estimate_openai_cost(model, io[0], io[1])
            if est is not None:
                cost = est
                cost_estimated = True  # 토큰×단가 추정치 — 실청구액 아님
        elif tokens:
            # #4(audit9): in/out 분리가 안 되고 합산 토큰만 있는 응답은 예전엔 tokens>0 인데도
            # cost=None 으로 남아 보고가 불일치했다(토큰은 보이는데 비용은 0/미상). 분리값이
            # 없으니 정확 추정은 불가하지만, 합산 토큰 전체를 (더 비싼) output 단가로 환산해
            # 보수적 상한 추정치를 내고 cost_estimated=True 로 표기한다 — codex/openai 와 일관되게
            # 사용량이 보이는 호출이 비용 0 으로 과소표시되지 않게 한다. 모델 단가가 없으면 None.
            est = _estimate_openai_cost(model, 0, tokens)
            if est is not None:
                cost = est
                cost_estimated = True  # 합산 토큰 기반 보수적 상한 추정치 — 실청구액 아님
        return RoleResult(
            ok=True,
            final_message=str(getattr(result, "final_output", "")),
            model=model,
            tokens=tokens,
            cost_usd=cost,
            cost_estimated=cost_estimated,
        )
