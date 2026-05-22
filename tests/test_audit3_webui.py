"""3차 감사(audit) 회귀 테스트: orchestrator/webui.py.

대상 이슈:
  #136 (#37) — /api/run 이 backend / backends 엔트리 / role_backends 값을 resolve() 에
               넘기기 전에 타입을 검증하지 않아, JSON 배열/객체(unhashable)가 오면
               ALIASES.get() 이 TypeError 를 내고 400 대신 핸들러가 죽는다.
               타입을 먼저 검증해 400 을 돌려준다.
  #137 (#38) — name 이 slugify().strip() 으로 흘러가므로 비문자열이면 raise 한다.
               핸들러에서 400 으로 거부하고, slugify() 자체도 비문자열에 견고하다.
  #136 (#31) — 임베디드 대시보드 JS 가 비숫자 cost/tokens 에 toFixed/toLocaleString 을
               호출해 throw 하면 tick() 의 catch 가 삼키고 대시보드가 조용히 멈춘다.
               num() 헬퍼로 NaN→0 강제 후 렌더한다 (INDEX_HTML 정적 검증).
  #61 (#11)  — 웹 poll-interval 기본 600 vs CLI 20 의 divergence 를 명시. 폼 입력칸과
               라벨이 노출되어 사용자가 직접 지정할 수 있다.

모두 오프라인·결정적이며 실제 백엔드/네트워크를 쓰지 않는다. HTTP 엔드포인트는
임시 포트의 stdlib ThreadingHTTPServer 를 fake spawn 으로 띄워 검증한다.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from orchestrator import webui


@pytest.fixture
def server(tmp_path):
    """fake spawn 으로 실제 서브프로세스 없이 HTTP 핸들러를 띄운다."""
    spawned = []

    def fake_spawn(cmd, log_path):
        spawned.append(cmd)

        class _P:
            pid = 33333

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


# ----------------- #37: backend 타입 검증을 resolve() 전에 -----------------


def test_run_rejects_non_string_backend(server):
    """#37: backend 가 list/dict(unhashable)면 resolve() 가 TypeError 를 내기 전에 400."""
    for bad in ([], {}, ["claude-cli"], {"x": 1}, 5, True):
        code, j = _post(server["base"], "/api/run", {"spec_text": "# s", "backend": bad})
        assert code == 400, (bad, j)
        assert j["error"] == "backend must be a string"
    # 핸들러가 죽지 않았으므로 정상 요청은 여전히 200
    code, j = _post(server["base"], "/api/run", {"spec_text": "# s", "backend": "mock"})
    assert code == 200 and "run_id" in j


def test_run_rejects_non_string_backends_entry(server):
    """#37: backends 우선순위 리스트의 엔트리가 비문자열이면 resolve() 전에 400."""
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "backends": ["mock", ["nested"]]},
    )
    assert code == 400
    assert "must be a string" in j["error"]
    # dict 엔트리도 거부
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "backends": ["mock", {"k": "v"}]},
    )
    assert code == 400
    assert "must be a string" in j["error"]


def test_run_rejects_non_string_role_backend_value(server):
    """#37: role_backends 값이 dict/숫자면 resolve() 전에 400 (unhashable TypeError 방지)."""
    role = next(iter(webui.ROLES))
    # dict 값
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "role_backends": {role: {"x": 1}}},
    )
    assert code == 400
    assert "string or list" in j["error"]
    # 숫자 값
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "role_backends": {role: 7}},
    )
    assert code == 400
    assert "string or list" in j["error"]
    # list 안의 비문자열 엔트리
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "role_backends": {role: ["mock", 9]}},
    )
    assert code == 400
    assert "must be a string" in j["error"]


def test_run_accepts_valid_role_backend_str_and_list(server):
    """#37: 정상 role_backends(str/list-of-str)는 여전히 통과 (회귀 없음)."""
    role = next(iter(webui.ROLES))
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "role_backends": {role: "mock"}},
    )
    assert code == 200 and "run_id" in j
    code, j = _post(
        server["base"],
        "/api/run",
        {"spec_text": "# s", "backend": "mock", "role_backends": {role: ["mock", "claude-cli"]}},
    )
    assert code == 200 and "run_id" in j


# ----------------- #38: run name 타입 검증 -----------------


def test_run_rejects_non_string_name(server):
    """#38: name 이 비문자열이면 slugify().strip() 이 raise 하기 전에 400."""
    for bad in (123, [], {}, 4.5, True):
        code, j = _post(server["base"], "/api/run", {"spec_text": "# s", "name": bad})
        assert code == 400, (bad, j)
        assert j["error"] == "name must be a string"


def test_run_accepts_string_or_missing_name(server):
    """#38: 문자열 name 또는 미지정(None)은 정상 동작."""
    code, j = _post(server["base"], "/api/run", {"spec_text": "# s", "name": "my-app"})
    assert code == 200 and j["run_id"].startswith("my-app-")
    # name 미지정 → 기본 "run"
    code, j = _post(server["base"], "/api/run", {"spec_text": "# s"})
    assert code == 200 and j["run_id"].startswith("run-")
    # name=null → 기본 "run"
    code, j = _post(server["base"], "/api/run", {"spec_text": "# s", "name": None})
    assert code == 200 and j["run_id"].startswith("run-")


def test_slugify_robust_to_non_string():
    """#38: slugify() 가 비문자열(숫자/None/list)에도 raise 없이 'run' 으로 폴백한다.

    rerun 경로에서 손상된 _run_opts.json 의 비문자열 name 이 흘러와도 안전.
    """
    assert webui.slugify(None) == "run"
    assert webui.slugify(123) == "run"
    assert webui.slugify([]) == "run"
    assert webui.slugify({"a": 1}) == "run"
    # 정상 문자열은 그대로
    assert webui.slugify("My App!") == "my-app"


def test_start_with_non_string_name_does_not_raise(tmp_path):
    """#38: RunManager.start 가 비문자열 name(예: 손상 opts)에도 raise 없이 run 을 만든다."""

    def fake_spawn(cmd, log_path):
        class _P:
            def poll(self):
                return None

        return _P()

    m = webui.RunManager(tmp_path / "runs", spawn=fake_spawn)
    rid = m.start("# s", {"name": 12345, "backend": "mock", "mock": True})
    assert rid.startswith("run-")  # 비문자열 name → 기본 slug


# ----------------- #31: 대시보드 JS 비숫자 cost/tokens 강제 변환 -----------------


def test_index_html_has_numeric_coercion_helper():
    """#31: num() 헬퍼가 존재하고 NaN 을 0 으로 강제한다 (toFixed/toLocaleString throw 방지)."""
    html = webui.INDEX_HTML
    assert "function num(" in html
    assert "Number.isFinite" in html


def test_index_html_numeric_renders_use_num_helper():
    """#31: cost/tokens(전체·역할별) 렌더가 모두 num(...) 으로 감싸져 있다."""
    html = webui.INDEX_HTML
    # 전체 cost/tokens
    assert "num(b.total_cost_usd).toFixed(4)" in html
    assert "num(b.total_tokens).toLocaleString()" in html
    # 역할별 cost/tokens
    assert "num(a.cost_usd).toFixed(4)" in html
    assert "num(a.tokens).toLocaleString()" in html
    # 옛 비강제 패턴이 남아있지 않은지 (회귀 방지)
    assert "(b.total_cost_usd||0).toFixed" not in html
    assert "(b.total_tokens||0).toLocaleString" not in html
    assert "(+(a.cost_usd||0)).toFixed" not in html
    assert "(+a.tokens).toLocaleString" not in html


# ----------------- #11: poll-interval 기본/폼 노출 -----------------


def test_poll_interval_web_default_is_600_and_configurable():
    """#11: 웹 기본 poll-interval 은 600(미지정 시), 폼에서 지정하면 그 값을 쓴다."""
    from pathlib import Path

    cmd = webui.build_command("py", Path("/s.md"), Path("/p"), {"backend": "mock"})
    assert cmd[cmd.index("--poll-interval") + 1] == "600"
    cmd2 = webui.build_command(
        "py", Path("/s.md"), Path("/p"), {"backend": "mock", "poll_interval": 15}
    )
    assert cmd2[cmd2.index("--poll-interval") + 1] == "15"


def test_index_html_exposes_poll_interval_field_and_divergence():
    """#11: poll-interval 입력칸이 폼에 있고, 웹 600 / CLI 20 divergence 가 라벨에 명시된다."""
    html = webui.INDEX_HTML
    assert 'id="pollInterval"' in html  # 입력 필드 존재
    assert 'raw("pollInterval")' in html  # JS 가 값을 읽어 전송
    # divergence 가 조용하지 않게 라벨/툴팁에 노출
    assert "600" in html and "20" in html
    assert "CLI" in html
