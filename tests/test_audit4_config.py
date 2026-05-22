"""런타임 데이터 디렉터리 리졸버 테스트 (#5: 휠 설치에서 역할/템플릿 파일 탐색).

`orchestrator.config._resolve_runtime_dir` 가
  1) 저장소 루트 위치가 있으면 그걸 우선 선택하고,
  2) 저장소 위치가 없으면 설치 data 위치(share/...)로 폴백하며,
  3) 어디도 없으면 저장소 루트 경로를 기본값으로 돌려주는지 검증한다.

휠 설치를 실제로 만들지 않고 임시 경로 + monkeypatch 로 hermetic 하게 시뮬레이션한다.
"""

from __future__ import annotations

import importlib
from pathlib import Path

from orchestrator import config


def test_repo_root_dir_is_chosen_when_it_exists():
    # 소스 체크아웃에서는 저장소 루트의 .claude/agents 와 templates 가 실재한다 →
    # 모듈 임포트 시 그쪽이 선택되어야 한다(기존 동작/테스트 불변; 후방 호환).
    assert config.AGENTS_DIR == config.FRAMEWORK_ROOT / ".claude" / "agents"
    assert config.TEMPLATES_DIR == config.FRAMEWORK_ROOT / "templates"
    assert config.AGENTS_DIR.is_dir()
    assert config.TEMPLATES_DIR.is_dir()


def test_resolver_prefers_repo_root_over_data_location(tmp_path: Path, monkeypatch):
    # 저장소 루트 후보와 sys.prefix data 후보가 둘 다 존재해도 저장소 루트가 이긴다.
    repo_root = tmp_path / "repo"
    (repo_root / ".claude" / "agents").mkdir(parents=True)
    prefix = tmp_path / "prefix"
    (prefix / config._DATA_AGENTS_REL).mkdir(parents=True)

    monkeypatch.setattr(config, "FRAMEWORK_ROOT", repo_root)
    monkeypatch.setattr(config.sys, "prefix", str(prefix))

    resolved = config._resolve_runtime_dir(Path(".claude") / "agents", config._DATA_AGENTS_REL)
    assert resolved == repo_root / ".claude" / "agents"


def test_resolver_falls_back_to_sys_prefix_data_when_repo_missing(tmp_path: Path, monkeypatch):
    # 저장소 루트 위치가 없고(휠 설치 모사) sys.prefix 아래 data-files 만 존재하면
    # 그 설치 data 위치로 폴백해야 한다.
    repo_root = tmp_path / "repo"  # 생성하지 않음 → 저장소 후보 부재
    prefix = tmp_path / "prefix"
    data_dir = prefix / config._DATA_AGENTS_REL
    data_dir.mkdir(parents=True)

    monkeypatch.setattr(config, "FRAMEWORK_ROOT", repo_root)
    monkeypatch.setattr(config.sys, "prefix", str(prefix))
    # site-packages 후보가 끼어들지 않도록 비운다(테스트 hermetic 보장).
    monkeypatch.setattr(config, "_site_packages_roots", lambda: [])

    resolved = config._resolve_runtime_dir(Path(".claude") / "agents", config._DATA_AGENTS_REL)
    assert resolved == data_dir


def test_resolver_falls_back_to_site_packages_data(tmp_path: Path, monkeypatch):
    # 저장소·sys.prefix 후보가 모두 없고 <site-packages>/share/... 만 있으면 그쪽을 쓴다.
    repo_root = tmp_path / "repo"  # 부재
    bare_prefix = tmp_path / "bare-prefix"  # data-files 없음
    bare_prefix.mkdir()
    site_root = tmp_path / "site-packages"
    site_data = site_root / config._DATA_TEMPLATES_REL
    site_data.mkdir(parents=True)

    monkeypatch.setattr(config, "FRAMEWORK_ROOT", repo_root)
    monkeypatch.setattr(config.sys, "prefix", str(bare_prefix))
    monkeypatch.setattr(config, "_site_packages_roots", lambda: [site_root])

    resolved = config._resolve_runtime_dir(Path("templates"), config._DATA_TEMPLATES_REL)
    assert resolved == site_data


def test_resolver_defaults_to_repo_path_when_none_exist(tmp_path: Path, monkeypatch):
    # 어떤 후보도 존재하지 않으면 저장소 루트 경로를 기본값으로 반환한다(후방 호환).
    repo_root = tmp_path / "repo"  # 부재
    bare_prefix = tmp_path / "bare"  # 부재
    monkeypatch.setattr(config, "FRAMEWORK_ROOT", repo_root)
    monkeypatch.setattr(config.sys, "prefix", str(bare_prefix))
    monkeypatch.setattr(config, "_site_packages_roots", lambda: [])

    resolved = config._resolve_runtime_dir(Path(".claude") / "agents", config._DATA_AGENTS_REL)
    assert resolved == repo_root / ".claude" / "agents"
    assert not resolved.exists()


def test_module_level_dirs_are_paths_and_imports_stable():
    # agents.py / workspace.py 가 임포트하는 심볼이 그대로 살아 있어야 한다.
    assert isinstance(config.AGENTS_DIR, Path)
    assert isinstance(config.TEMPLATES_DIR, Path)
    # 재임포트해도 같은 결과(모듈 부작용 없음).
    reloaded = importlib.reload(config)
    assert reloaded.AGENTS_DIR == reloaded.FRAMEWORK_ROOT / ".claude" / "agents"
    assert reloaded.TEMPLATES_DIR == reloaded.FRAMEWORK_ROOT / "templates"
