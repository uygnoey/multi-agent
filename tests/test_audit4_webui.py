"""4차 감사(audit) 회귀 테스트: orchestrator/webui.py.

대상 이슈:
  #12 — /api/run 의 숫자 검증 루프가 poll_interval 을 int(v) 로 검증했다. 그러나
        poll_interval 은 CLI(--poll-interval type=float, default=20.0)와 RunConfig
        (poll_interval: float)에서 모두 float 다 — 1.5 같은 소수도 유효한 값이다.
        그래서 웹 클라이언트가 poll_interval=1.5 를 보내면 불필요하게 400 을 받았다
        (같은 값이 CLI 에선 통과하는데 웹에선 거부되는 정책 불일치).
        FIX: poll_interval 을 float 로 검증(>=0)하고, build_command 도 _coerce_float
        로 raw 값을 그대로 CLI 에 전달해 두 진입점의 타입 정책을 일치시킨다.
        concurrency/max_units/max_attempts/retries 는 여전히 정수 검증,
        timeout/budget 은 이미 float 검증.

모두 오프라인·결정적이며 실제 백엔드/네트워크를 쓰지 않는다. HTTP 엔드포인트는
임시 포트의 stdlib ThreadingHTTPServer 를 fake spawn 으로 띄워 검증한다.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from orchestrator import webui


@pytest.fixture
def server(tmp_path):
    """fake spawn 으로 실제 서브프로세스 없이 HTTP 핸들러를 띄운다."""
    spawned = []

    def fake_spawn(cmd, log_path):
        spawned.append(cmd)

        class _P:
            pid = 44444

            def poll(self):
                return None  # 실행 중인 척

            def wait(self):
                return 0

        return _P()

    manager = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._make_handler(manager))
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"base": f"http://127.0.0.1:{port}", "manager": manager, "spawned": spawned}
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, body_obj):
    data = json.dumps(body_obj).encode("utf-8")
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _poll_arg(cmd):
    """build_command 결과에서 --poll-interval 의 값을 꺼낸다."""
    i = cmd.index("--poll-interval")
    return cmd[i + 1]


# ----------------- #12: /api/run float poll_interval 검증 -----------------


def test_run_accepts_float_poll_interval(server):
    """#12: poll_interval=1.5(소수)는 CLI/RunConfig 에서 유효하므로 더 이상 400 이 아니다."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "poll_interval": 1.5},
    )
    assert code == 200, j
    assert "run_id" in j
    # CLI 로 전달된 명령에도 소수가 보존되어야 한다 (int 로 깎이지 않음).
    assert _poll_arg(server["spawned"][-1]) == "1.5"


def test_run_accepts_float_poll_interval_string(server):
    """#12: 폼은 문자열로 보낸다 ('2.5') — float 로 파싱돼 통과하고 명령에 보존된다."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "poll_interval": "2.5"},
    )
    assert code == 200, j
    assert _poll_arg(server["spawned"][-1]) == "2.5"


def test_run_accepts_zero_poll_interval(server):
    """#12: poll_interval=0 은 여전히 허용(>=0). RunConfig __post_init__ 가 안전 하한 클램프."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "poll_interval": 0},
    )
    assert code == 200, j


def test_run_accepts_integer_poll_interval(server):
    """#12: 정수 poll_interval 도 여전히 통과 (회귀 없음)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "poll_interval": 600},
    )
    assert code == 200, j


def test_run_rejects_negative_poll_interval(server):
    """#12: 음수 poll_interval 은 float 검증에서도 400 (>=0)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "poll_interval": -1.5},
    )
    assert code == 400, j
    assert "poll_interval must be >= 0" in j["error"]


def test_run_rejects_non_numeric_poll_interval(server):
    """#12: 숫자로 파싱 불가한 poll_interval('abc')은 400."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "poll_interval": "abc"},
    )
    assert code == 400, j
    assert "invalid poll_interval" in j["error"]


# ----------------- #12: 정수 필드들은 여전히 정수 검증 -----------------


def test_run_rejects_float_concurrency_string(server):
    """#12: concurrency 는 진짜 int 다 — 폼이 보내는 소수 문자열('1.5')은 int(v) 가
    ValueError 를 내 400 으로 거부된다 (poll_interval 처럼 float 검증을 받지 않음)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "concurrency": "1.5"},
    )
    assert code == 400, j
    assert "invalid concurrency" in j["error"]


def test_run_rejects_zero_concurrency(server):
    """#12: concurrency 는 >=1 — 0 은 400 (정수 검증 유지)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "concurrency": 0},
    )
    assert code == 400, j
    assert "concurrency must be >= 1" in j["error"]


def test_run_rejects_zero_max_attempts(server):
    """#12: max_attempts 는 >=1 정수 검증 유지 — 0 은 400."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "max_attempts": 0},
    )
    assert code == 400, j
    assert "max_attempts must be >= 1" in j["error"]


def test_run_accepts_zero_retries(server):
    """#12: retries 는 >=0 정수 검증 유지 — 0 은 통과."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "retries": 0},
    )
    assert code == 200, j


def test_run_rejects_negative_retries(server):
    """#12: retries 음수는 400 (>=0)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "retries": -1},
    )
    assert code == 400, j
    assert "retries must be >= 0" in j["error"]


# ----------------- #12: timeout/budget 은 여전히 float 검증 -----------------


def test_run_accepts_float_timeout_and_budget(server):
    """#12: timeout/budget 은 이미 float 검증 — 소수는 통과 (회귀 없음)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "timeout": 12.5, "budget": 1.25},
    )
    assert code == 200, j


def test_run_rejects_negative_budget(server):
    """#12: budget 음수는 400 (>=0)."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "budget": -0.5},
    )
    assert code == 400, j
    assert "budget must be >= 0" in j["error"]


# ----------------- #12: build_command float pass-through (단위) -----------------


def test_build_command_preserves_float_poll_interval():
    """#12: build_command 가 poll_interval 을 int 로 강제하지 않고 float 로 보존한다."""
    cmd = webui.build_command("py", Path("/tmp/spec.md"), Path("/tmp/proj"), {"poll_interval": 1.5})
    assert _poll_arg(cmd) == "1.5"
    # 문자열로 와도 동일
    cmd = webui.build_command(
        "py", Path("/tmp/spec.md"), Path("/tmp/proj"), {"poll_interval": "3.5"}
    )
    assert _poll_arg(cmd) == "3.5"


def test_build_command_poll_interval_default_when_missing():
    """#12: poll_interval 미지정이면 웹 기본 600 으로 폴백."""
    cmd = webui.build_command("py", Path("/tmp/spec.md"), Path("/tmp/proj"), {})
    assert _poll_arg(cmd) == "600"


def test_build_command_poll_interval_corrupt_falls_back():
    """#12/#38: 손상값('abc')은 raise 하지 않고 600 으로 폴백 (rerun 견고성)."""
    cmd = webui.build_command(
        "py", Path("/tmp/spec.md"), Path("/tmp/proj"), {"poll_interval": "abc"}
    )
    assert _poll_arg(cmd) == "600"


# ----------------- #12: 웹/CLI 정책 일치 (end-to-end via RunConfig) -----------------


def test_web_float_poll_interval_accepted_by_runconfig():
    """#12: 웹이 통과시키는 float poll_interval(1.5)을 RunConfig 도 받아들인다 (정책 일치).

    웹 검증은 RunConfig/CLI 타입과 같아야 한다 — 같은 값이 두 진입점에서 모두 유효.
    """
    from orchestrator.config import RunConfig

    cfg = RunConfig(spec_path=Path("/tmp/s.md"), project_dir=Path("/tmp/proj"), poll_interval=1.5)
    # __post_init__ 가 float 로 유지 (1.0 하한 위라 그대로 보존).
    assert isinstance(cfg.poll_interval, float)
    assert cfg.poll_interval == 1.5
