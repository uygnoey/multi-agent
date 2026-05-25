"""감사 9차(2026-05-25) 회귀 테스트: config/agents/prompts/workspace 동작 수정.

전부 순수·오프라인이며 mock 백엔드도 호출하지 않는다(설정/파싱 로직 + 파일 스캐폴딩만).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.agents import _parse_meta, load_agent
from orchestrator.config import (
    RunConfig,
    _coerce_int,
    _coerce_optional_positive_int,
    normalize_completion_level,
)
from orchestrator.prompts import compose_prompt
from orchestrator.workspace import _fmt_stack, scaffold


def _cfg(**kw) -> RunConfig:
    return RunConfig(spec_path=Path("spec.md"), project_dir=Path("proj"), **kw)


# ---------------------------------------------------------------------------
# #audit9-4: _coerce_int 가 float-유사 문자열을 float 와 동일하게 절단
# ---------------------------------------------------------------------------


def test_coerce_int_float_truncates():
    assert _coerce_int(3.7, 9) == 3


def test_coerce_int_float_like_string_truncates_consistently():
    # 예전엔 "3.7" 이 int("3.7") ValueError 로 default 로 빠졌다 → 이제 float 처럼 3.
    assert _coerce_int("3.7", 9) == 3
    assert _coerce_int("  -2.9 ", 9) == -2


def test_coerce_int_garbage_falls_to_default():
    assert _coerce_int("abc", 9) == 9
    assert _coerce_int(None, 9) == 9
    assert _coerce_int(float("nan"), 9) == 9
    assert _coerce_int(float("inf"), 9) == 9


def test_coerce_int_bool_is_default():
    # bool 은 int 의 서브클래스지만 옵션값으로 의미 없으므로 default.
    assert _coerce_int(True, 9) == 9


# ---------------------------------------------------------------------------
# #audit9-2: max_units malformed AND <=0 모두 None(무제한)으로 일관 정규화
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["bad", "not-an-int", 0, -1, -100, 0.0, "0", "-3"])
def test_max_units_invalid_or_nonpositive_all_unlimited(raw):
    assert _cfg(max_units=raw).max_units is None


def test_max_units_none_is_unlimited():
    assert _cfg(max_units=None).max_units is None


def test_max_units_positive_preserved():
    assert _cfg(max_units=5).max_units == 5
    assert _cfg(max_units="7").max_units == 7
    # float-유사 문자열/실수도 절단해 양수면 보존.
    assert _cfg(max_units=3.7).max_units == 3
    assert _cfg(max_units="3.7").max_units == 3


def test_coerce_optional_positive_int_helper_directly():
    assert _coerce_optional_positive_int("bad") is None
    assert _coerce_optional_positive_int(0) is None
    assert _coerce_optional_positive_int(-5) is None
    assert _coerce_optional_positive_int(True) is None
    assert _coerce_optional_positive_int(4) == 4


# ---------------------------------------------------------------------------
# #audit9-3: budget<=0 은 유한값 그대로 보존(기존 계약 유지), 깨진 값만 None
# ---------------------------------------------------------------------------


def test_budget_zero_preserved():
    assert _cfg(budget=0.0).budget == 0.0


def test_budget_negative_preserved_as_finite():
    assert _cfg(budget=-1.0).budget == -1.0


def test_budget_nan_inf_become_none():
    assert _cfg(budget=float("nan")).budget is None
    assert _cfg(budget=float("inf")).budget is None
    assert _cfg(budget="oops").budget is None


# ---------------------------------------------------------------------------
# #audit9-10: normalize_completion_level 단일 소스 (config/prompts 일치)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mvp", "mvp"),
        ("MVP", "mvp"),
        ("production", "production"),
        ("prod", "production"),
        ("production-ready", "production"),
        ("production_ready", "production"),
        ("  Prod  ", "production"),
        ("garbage", "mvp"),
        (None, "mvp"),
        ("", "mvp"),
    ],
)
def test_normalize_completion_level(raw, expected):
    assert normalize_completion_level(raw) == expected


def test_runconfig_uses_normalize_completion_level():
    assert _cfg(completion_level="production_ready").completion_level == "production"
    assert _cfg(completion_level="nonsense").completion_level == "mvp"


def test_prompts_reuses_completion_normalization():
    # prompts 도 동일 헬퍼를 쓰므로 production_ready 가 production 블록을 낸다.
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit={"id": "U1"},
        directives="",
        result_rel="r.json",
        spec_excerpt="",
        completion_level="production_ready",
    )
    assert "Production:" in out


# ---------------------------------------------------------------------------
# #audit9-1: _cross_pool 단일 소스 (base 생략 시 _base_pool 사용)
# ---------------------------------------------------------------------------


def test_cross_pool_default_base_matches_explicit():
    cfg = _cfg(default_backend="claude-cli", role_backend={"qa": "codex"})
    assert cfg._cross_pool() == cfg._cross_pool(cfg._base_pool())
    assert set(cfg._cross_pool()) == {"claude-cli", "codex"}


def test_cross_pool_default_base_from_priority():
    cfg = _cfg(backend_priority=["claude-cli", "codex"])
    assert cfg._cross_pool() == ["claude-cli", "codex"]


# ---------------------------------------------------------------------------
# #audit9-5: model_for 는 backend 인자를 무시하고 전역 모델을 돌려준다(시그니처 유지)
# ---------------------------------------------------------------------------


def test_model_for_ignores_backend_arg():
    cfg = _cfg(model="gpt-x")
    assert cfg.model_for("claude-cli") == "gpt-x"
    assert cfg.model_for("codex") == "gpt-x"
    assert _cfg().model_for("anything") is None


# ---------------------------------------------------------------------------
# #audit9-6: load_agent 가 역할을 정규화 → supervisor 별칭에 DEV_TOOLS 미부여
# ---------------------------------------------------------------------------


def test_load_agent_normalizes_supervisor_alias_no_priv_escalation():
    # "pm" 별칭은 project-manager(읽기 전용). 정규화 없이는 .md 미스로 DEV_TOOLS 가 샜다.
    agent = load_agent("pm")
    assert agent.name == "project-manager"
    assert agent.tools == ["Read"]
    assert "Write" not in agent.tools
    assert "Bash" not in agent.tools


def test_load_agent_normalizes_pl_alias():
    agent = load_agent("pl")
    assert agent.name == "project-leader"
    assert agent.tools == ["Read"]


def test_load_agent_normalizes_dev_alias():
    # backend 별칭 → backend-developer(DEV_TOOLS) 정상 매핑.
    agent = load_agent("backend")
    assert agent.name == "backend-developer"
    assert "Write" in agent.tools


# ---------------------------------------------------------------------------
# #audit9-7: yaml 성공·non-dict → 빈 dict (경량 폴백으로 garbage 생성 금지)
# ---------------------------------------------------------------------------


def test_parse_meta_non_dict_yaml_returns_empty():
    # frontmatter 가 리스트/스칼라면 yaml 은 list/str 을 돌려준다 → {} 여야 한다.
    assert _parse_meta("- a\n- b") == {}
    assert _parse_meta("just a scalar") == {}
    assert _parse_meta("") == {}


def test_parse_meta_dict_yaml_preserved():
    meta = _parse_meta("name: foo\ntools: Read, Write")
    assert meta.get("name") == "foo"


# ---------------------------------------------------------------------------
# #audit9-8: 경량 폴백이 따옴표 제거 + 인라인 리스트 처리 (pyyaml 없을 때)
# ---------------------------------------------------------------------------


def test_parse_meta_fallback_strips_quotes_and_lists(monkeypatch):
    # pyyaml 을 강제로 import 실패시켜 경량 폴백 경로를 탄다.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("simulated: no pyyaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    meta = _parse_meta('name: "foo"\ntools: [Read, "Write"]\ndesc: \'bar\'')
    assert meta["name"] == "foo"
    assert meta["desc"] == "bar"
    assert meta["tools"] == ["Read", "Write"]


# ---------------------------------------------------------------------------
# #audit9-9: prompts 의 non-dict unit 은 entire-spec 경로로 (빈 블록 금지)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_unit", [["a", "b"], "stringy", 42, {"id": "U1"} and ["x"]])
def test_compose_prompt_non_dict_unit_uses_spec_scope(bad_unit):
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit=bad_unit,
        directives="",
        result_rel="r.json",
        spec_excerpt="SPECBODY",
    )
    assert "## Scope\nThe entire spec." in out
    assert "## Target work unit" not in out
    assert "SPECBODY" in out


def test_compose_prompt_dict_unit_uses_target_block():
    out = compose_prompt(
        role="backend-developer",
        phase="dev",
        unit={"id": "U7", "title": "T", "description": "D", "deps": []},
        directives="",
        result_rel="r.json",
        spec_excerpt="ignored",
    )
    assert "## Target work unit" in out
    assert "id: U7" in out


# ---------------------------------------------------------------------------
# #audit9-13: _fmt_stack / spec_text None-safety
# ---------------------------------------------------------------------------


def test_fmt_stack_none_safe():
    assert _fmt_stack(None) == ""
    assert _fmt_stack("not a dict") == ""
    assert _fmt_stack({"a": 1}) == "a=1"


def test_scaffold_none_spec_does_not_crash(tmp_path):
    target = tmp_path / "proj"
    # spec_text=None 이어도 CLAUDE.md 렌더(spec_text[:1200])가 죽지 않아야 한다.
    scaffold(target, None, {"backend": "FastAPI"})
    assert (target / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# #audit9-11: 시스템 디렉터리 블랙리스트
# ---------------------------------------------------------------------------


def test_scaffold_rejects_system_dir(monkeypatch):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    with pytest.raises(ValueError, match="시스템 디렉터리"):
        scaffold(Path("/etc"), "spec", {})


def test_scaffold_rejects_usr(monkeypatch):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    with pytest.raises(ValueError):
        scaffold(Path("/usr"), "spec", {})


def test_scaffold_allows_subdir_of_system_dir(tmp_path, monkeypatch):
    # /etc/myproj 처럼 한 단계 더 아래는 정상 타깃(블랙리스트는 루트 바로 아래만).
    # 실제 /etc 에 쓰지 않도록 tmp 하위에 'etc/myproj' 를 흉내내어 검증.
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    target = tmp_path / "etc" / "myproj"
    scaffold(target, "spec", {"db": "SQLite"})
    assert (target / ".orchestrator").is_dir()


def test_scaffold_system_dir_bypass_with_env(monkeypatch, tmp_path):
    # ORCH_ALLOW_UNSAFE_PROJECT_DIR=1 이면 가드를 우회한다(여기선 tmp 디렉터리명만 'var').
    monkeypatch.setenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", "1")
    # 실제 시스템 디렉터리를 건드리지 않도록 tmp 하위 디렉터리로 검증(우회 플래그 동작만 확인).
    target = tmp_path / "ok"
    scaffold(target, "spec", {})
    assert (target / ".orchestrator").is_dir()


# ---------------------------------------------------------------------------
# #audit9-12: 심볼릭 링크를 통한 외부 쓰기 방어
# ---------------------------------------------------------------------------


def test_scaffold_rejects_orchestrator_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.delenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    # .orchestrator 를 project 밖(outside)으로 향하는 심링크로 심어둔다.
    (project / ".orchestrator").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="심볼릭 링크|밖"):
        scaffold(project, "spec", {})
    # 방어가 동작했으니 outside 에 results/qa 가 생기지 않아야 한다.
    assert not (outside / "results").exists()


def test_scaffold_symlink_bypass_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCH_ALLOW_UNSAFE_PROJECT_DIR", "1")
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".orchestrator").symlink_to(outside, target_is_directory=True)
    # 우회 플래그가 켜지면 심링크를 따라 outside 에 쓴다(거부하지 않음).
    scaffold(project, "spec", {})
    assert (outside / "results").is_dir()


# ---------------------------------------------------------------------------
# #audit9-14: 미치환 템플릿 토큰 경고
# ---------------------------------------------------------------------------


def test_render_template_warns_on_leftover_tokens(capsys):
    from orchestrator.workspace import _render_template_once

    out = _render_template_once("hello {{KNOWN}} and {{unknown}}", {"KNOWN": "x"})
    assert "hello x and {{unknown}}" == out
    captured = capsys.readouterr().out
    assert "미치환" in captured
    assert "{{unknown}}" in captured
