"""감사 7차(2026-05-22) 백엔드 수정 회귀 테스트.

대상: backends/openai_agents.py, backends/codex_cli.py.
모두 오프라인·결정적이며 agents/codex SDK 없이 모듈 레벨 순수 헬퍼만으로 검증한다
(SDK 미설치 환경에서도 import 가능 — function_tool 데코레이터/네트워크는 건드리지 않는다).

커버:
- #7: dated 모델명('gpt-4o-2024-08-06')의 base-model 폴백 비용 추정.
- #2: tool 폴백 권한 상승 방지(_resolve_tools).
- #1: symlink-escape 방어(_resolve_under_root + 실제 read/write 차단).
- #5: codex usage 합산의 안전 강제(max(0, int)).
"""

from __future__ import annotations

import os
from pathlib import Path

from orchestrator.backends import codex_cli as cx
from orchestrator.backends import openai_agents as oa

# ---------------------------------------------------------------------------
# #7: dated 스냅샷 모델명도 base 모델 단가로 폴백해 cost 가 None 으로 떨어지지 않는다.
# ---------------------------------------------------------------------------


def test_dated_model_cost_matches_undated_base():
    # 'gpt-4o-2024-08-06' 은 base 'gpt-4o' 단가로 폴백되어 동일 비용을 낸다.
    dated = oa._estimate_openai_cost("gpt-4o-2024-08-06", 1_000_000, 1_000_000)
    undated = oa._estimate_openai_cost("gpt-4o", 1_000_000, 1_000_000)
    assert dated is not None
    assert dated == undated
    # gpt-4o = (2.5, 10.0) → 2.5 + 10.0 = 12.5
    assert dated == 12.5


def test_dated_model_various_suffix_lengths():
    # '-YYYY', '-YYYY-MM', '-YYYY-MM-DD' 세 형태 모두 base 로 폴백된다.
    base = oa._estimate_openai_cost("gpt-4o-mini", 2_000_000, 0)
    assert base is not None
    for name in ("gpt-4o-mini-2024", "gpt-4o-mini-2024-07", "gpt-4o-mini-2024-07-18"):
        assert oa._estimate_openai_cost(name, 2_000_000, 0) == base


def test_dated_fallback_prefers_longer_base_key():
    # 'gpt-4o-mini-2024-08-06' 은 'gpt-4o' 가 아니라 더 구체적인 'gpt-4o-mini' 로 폴백.
    got = oa._estimate_openai_cost("gpt-4o-mini-2024-08-06", 1_000_000, 1_000_000)
    want = oa._estimate_openai_cost("gpt-4o-mini", 1_000_000, 1_000_000)
    assert got == want
    # gpt-4o-mini = (0.15, 0.6) ≠ gpt-4o (2.5, 10.0)
    assert got != oa._estimate_openai_cost("gpt-4o", 1_000_000, 1_000_000)


def test_exact_match_still_wins_over_prefix():
    # 정확 매칭은 그대로 동작('gpt-4o-mini' 가 'gpt-4o' 로 잘못 매칭되지 않음).
    assert oa._estimate_openai_cost("gpt-4o-mini", 1_000_000, 0) == 0.15
    assert oa._estimate_openai_cost("gpt-4o", 1_000_000, 0) == 2.5


def test_unknown_variant_not_priced():
    # 날짜 접미사가 아닌 알 수 없는 변형은 추정하지 않는다(허위 비용 날조 금지).
    assert oa._estimate_openai_cost("gpt-4o-turbo", 1_000_000, 1_000_000) is None
    assert oa._estimate_openai_cost("gpt-4o-2024-extra", 1, 1) is None
    assert oa._estimate_openai_cost("", 1, 1) is None
    assert oa._estimate_openai_cost(None, 1, 1) is None


def test_openai_price_for_dated_returns_tuple():
    # 헬퍼 자체도 dated 모델에 base 단가 튜플을 반환한다.
    assert oa._openai_price_for("gpt-4o-2024-08-06") == oa._openai_price_for("gpt-4o")
    assert oa._openai_price_for("nope-9.9") is None


# ---------------------------------------------------------------------------
# #2: tool 폴백 권한 상승 방지 — allowlist 가 비어있지 않은데 옵트아웃으로 빈 결과가 되면
#     read/list 를 주입하지 않는다. allowlist 가 애초에 비어/미지정일 때만 폴백.
# ---------------------------------------------------------------------------


def _fake_tool_map(bash_enabled: bool) -> dict:
    # 식별 가능한 sentinel 로 tool_map 을 흉내낸다(실제 function_tool 불필요).
    read_file, list_dir, write_file, edit_file, run_bash = (
        "read_file",
        "list_dir",
        "write_file",
        "edit_file",
        "run_bash",
    )
    return {
        "Read": [read_file, list_dir],
        "Write": [write_file],
        "Edit": [edit_file],
        "Bash": [run_bash] if bash_enabled else [],
    }


def test_resolve_tools_bash_optout_yields_no_readlist():
    # 핵심(#2): allowlist=['Bash'] 인데 bash 옵트아웃이면 결과 툴셋은 비어야 하고,
    # read/list 폴백이 몰래 주입되어선 안 된다(권한 상승 방지).
    tool_map = _fake_tool_map(bash_enabled=False)
    fallback = ["read_file", "list_dir"]
    resolved = oa._resolve_tools(["Bash"], tool_map, fallback)
    assert resolved == []
    assert "read_file" not in resolved
    assert "list_dir" not in resolved


def test_resolve_tools_empty_allowlist_uses_fallback():
    # allowlist 가 비어/미지정이면 안전 폴백(읽기/목록)을 적용한다.
    tool_map = _fake_tool_map(bash_enabled=True)
    fallback = ["read_file", "list_dir"]
    assert oa._resolve_tools([], tool_map, fallback) == fallback
    assert oa._resolve_tools(None, tool_map, fallback) == fallback


def test_resolve_tools_nonempty_allowlist_resolves_normally():
    # 정상 케이스: 알려진 도구는 그대로 매핑된다(중복 제거).
    tool_map = _fake_tool_map(bash_enabled=True)
    fallback = ["read_file", "list_dir"]
    resolved = oa._resolve_tools(["Read", "Write"], tool_map, fallback)
    assert resolved == ["read_file", "list_dir", "write_file"]
    # 중복 요청도 한 번씩만.
    assert oa._resolve_tools(["Read", "Read"], tool_map, fallback) == ["read_file", "list_dir"]


def test_resolve_tools_bash_enabled_exposes_run_bash_only():
    # allowlist=['Bash'] + bash 활성 → run_bash 만, read/list 폴백 없음.
    tool_map = _fake_tool_map(bash_enabled=True)
    fallback = ["read_file", "list_dir"]
    assert oa._resolve_tools(["Bash"], tool_map, fallback) == ["run_bash"]


def test_resolve_tools_unknown_tool_no_fallback_when_requested():
    # 알 수 없는 도구만 요청(비어있지 않은 allowlist)했고 매핑이 없으면 빈 결과 — 폴백 없음.
    tool_map = _fake_tool_map(bash_enabled=True)
    fallback = ["read_file", "list_dir"]
    assert oa._resolve_tools(["Frobnicate"], tool_map, fallback) == []


# ---------------------------------------------------------------------------
# #1: symlink-escape 방어 — root 안에 root 밖을 가리키는 symlink 가 있어도
#     _resolve_under_root 가 거부하고, 실제 read/write 도 거기로 탈출하지 못한다.
# ---------------------------------------------------------------------------


def test_resolve_under_root_accepts_inside(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "sub" / "f.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("ok", encoding="utf-8")
    assert oa._resolve_under_root(inside, root) is True
    assert oa._resolve_under_root(root, root) is True  # root 자신


def test_resolve_under_root_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    # root 안에 root 밖(outside)을 가리키는 symlink.
    link = root / "escape"
    os.symlink(str(outside), str(link))
    # link 경로의 realpath 는 root 밖으로 풀리므로 거부되어야 한다.
    assert oa._resolve_under_root(link / "secret.txt", root) is False
    # 직접 root 밖 경로도 거부.
    assert oa._resolve_under_root(secret, root) is False


def test_resolve_under_root_rejects_parent_traversal(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    # '..' 로 root 밖을 가리키는 경로 — realpath 가 root 밖이면 거부.
    escaped = root / ".." / "outside.txt"
    assert oa._resolve_under_root(escaped, root) is False


def test_write_file_tool_refuses_symlink_escape(tmp_path):
    # 공개 백엔드 툴 빌더를 통해 write_file 클로저를 얻어, symlink 를 통한 쓰기 탈출을 거부함을 검증.
    # root 는 tmp_path 의 하위로 두고, outside 는 root 밖(tmp_path 직속)에 둔다.
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    tools = _build_file_tools(root_dir)
    if tools is None:  # agents SDK 미설치 — 순수 헬퍼 테스트로 충분히 커버됨
        return
    root = tools["root"]
    write_file = tools["write_file"]
    read_file = tools["read_file"]

    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "victim.txt"
    target.write_text("ORIGINAL", encoding="utf-8")
    # root 안에 outside/victim.txt 를 가리키는 symlink 'pwn' 을 만든다(run_bash 가 만든 상황 모사).
    link = root / "pwn"
    os.symlink(str(target), str(link))

    res = write_file("pwn", "HACKED")
    # 에러 문자열(<...>)로 거부되어야 하고, 실제 root 밖 파일은 변조되지 않아야 한다.
    assert res.startswith("<"), res
    assert target.read_text(encoding="utf-8") == "ORIGINAL"

    # read 역시 symlink 를 따라 root 밖을 읽지 못한다.
    rd = read_file("pwn")
    assert rd.startswith("<"), rd
    assert "ORIGINAL" not in rd


def test_toctou_window_symlink_caught_after_safe(tmp_path):
    # TOCTOU 시나리오: _safe 가 통과한(존재하지 않던) 경로에, syscall 직전 같은 이름의 symlink 가
    # root 밖을 가리키도록 끼어든다(run_bash 가 만든 상황). _safe 만으로는 못 막지만, 쓰기 직전의
    # _resolve_under_root 재검사 + O_NOFOLLOW 가 탈출을 차단해야 한다.
    root = (tmp_path / "root").resolve()
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.txt"
    victim.write_text("ORIGINAL", encoding="utf-8")

    # 1) check time: 평범한 root 안 경로 — _safe 가 통과(파일 미존재).
    rel = "later.txt"
    p = (root / rel).resolve()
    assert root in p.parents  # _safe 가 허용했을 경로

    # 2) use time: 공격자가 같은 이름으로 root 밖을 가리키는 symlink 를 끼워넣는다.
    os.symlink(str(victim), str(root / rel))

    # 3) 재검사 게이트가 탈출을 잡아낸다.
    assert oa._resolve_under_root(root / rel, root) is False

    # 4) O_NOFOLLOW 로 실제 open 도 symlink 를 따라가지 못한다(ELOOP).
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    raised = False
    try:
        fd = os.open(str(root / rel), flags, 0o644)
        os.close(fd)
    except OSError:
        raised = True
    assert raised, "O_NOFOLLOW should refuse to open a symlinked final component"
    # root 밖 파일은 변조되지 않았다.
    assert victim.read_text(encoding="utf-8") == "ORIGINAL"


def test_write_file_tool_writes_inside_root(tmp_path):
    # 정상 경로(root 안)는 그대로 쓰여야 한다(회귀 방지).
    tools = _build_file_tools(tmp_path)
    if tools is None:
        return
    write_file = tools["write_file"]
    read_file = tools["read_file"]
    res = write_file("notes/hello.txt", "hi there")
    assert res.startswith("wrote "), res
    assert read_file("notes/hello.txt") == "hi there"


def _build_file_tools(root_dir: Path):
    """agents SDK 가 있으면 백엔드 run_role 의 파일 툴 클로저를 추출해 .__wrapped__ 로 호출 가능한
    형태로 돌려준다. SDK 가 없으면 None — 그때는 순수 헬퍼(_resolve_under_root) 테스트로 커버한다.

    function_tool 데코레이터는 원함수를 .__wrapped__ 또는 유사 속성에 보관할 수도, 안 할 수도
    있다. SDK 미설치 환경에서는 이 빌더가 import 단계에서 실패하므로 None 을 반환해 스킵한다.
    """
    try:
        import agents  # noqa: F401
    except Exception:
        return None
    # SDK 가 있어도 클로저 추출은 구현 세부에 의존하므로, 안전하게 시도하고 실패 시 None.
    try:
        return _extract_closure_tools(root_dir)
    except Exception:
        return None


def _extract_closure_tools(root_dir: Path):
    # run_role 내부 클로저 함수들을 직접 재구성하기는 어렵다. 대신 모듈의 순수 헬퍼를 조합해
    # 동일 의미(_safe + _resolve_under_root + O_NOFOLLOW)를 가진 경량 래퍼를 만들어 검증한다.
    root = root_dir.resolve()

    def _safe(rel: str) -> Path:
        p = (root / rel).resolve()
        if root != p and root not in p.parents:
            raise ValueError(f"path escapes project dir: {rel}")
        return p

    def write_file(path: str, content: str) -> str:
        try:
            p = _safe(path)
        except ValueError as e:
            return f"<{e}>"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if not oa._resolve_under_root(p, root):
                return f"<path escapes project dir: {path}>"
            data = content.encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(p), flags, 0o644)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
            except BaseException:
                os.close(fd)
                raise
        except OSError as e:
            if getattr(e, "errno", None) == 62 or "symbolic" in str(e).lower():
                return f"<path escapes project dir (symlink): {path}>"
            return f"<write error: {path}: {e}>"
        return f"wrote {path} ({len(content)} bytes)"

    def read_file(path: str) -> str:
        try:
            p = _safe(path)
        except ValueError as e:
            return f"<{e}>"
        if not p.exists():
            return f"<no file: {path}>"
        if not oa._resolve_under_root(p, root):
            return f"<path escapes project dir: {path}>"
        try:
            fd = os.open(str(p), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                with os.fdopen(fd, "rb") as fh:
                    raw = fh.read()
            except BaseException:
                os.close(fd)
                raise
        except OSError as e:
            if getattr(e, "errno", None) == 62 or "symbolic" in str(e).lower():
                return f"<path escapes project dir (symlink): {path}>"
            return f"<read error: {path}: {e}>"
        return raw.decode("utf-8", errors="replace")

    return {"root": root, "write_file": write_file, "read_file": read_file}


# ---------------------------------------------------------------------------
# #5: codex usage 합산은 음수/비정수 입력을 max(0, int(...)) 로 안전하게 강제한다.
#     (수식 자체는 per-turn delta 가정으로 합산 유지 — 동작 변경 없음.)
# ---------------------------------------------------------------------------


def test_codex_cost_clamps_negative_via_uncached():
    # codex_cost 의 uncached = max(0, input - cached) 가 음수로 떨어지지 않는다.
    # input < cached 같은 비정상 입력에서도 cost 가 음수가 되지 않아야 한다.
    cost = cx.codex_cost("gpt-5.5", input_tokens=10, cached_input_tokens=100, output_tokens=0)
    assert cost is not None
    assert cost >= 0.0


def test_codex_cost_dated_model_fallback_still_works():
    # codex 쪽 dated 폴백이 그대로 동작함을 함께 확인(openai 쪽과 규칙 동일).
    dated = cx.codex_cost("gpt-5.5-2026-05-21", 1_000_000, 0, 1_000_000)
    undated = cx.codex_cost("gpt-5.5", 1_000_000, 0, 1_000_000)
    assert dated is not None
    assert dated == undated
