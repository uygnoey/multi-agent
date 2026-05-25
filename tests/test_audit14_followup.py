"""감사 14차 회귀 테스트: 교차검증 후속 합의 항목.

- scheduler: 검증 실패 시그니처에 source(test-engineer vs qa) 포함 → 서로 다른 검증 주체의
  실패를 같은 '반복 실패'로 과합산하지 않는다(같은 source 반복일 때만 count 증가).
- webui: 기획서 파일 업로드가 .md/.txt 뿐 아니라 .html 도 허용한다.

결정적·오프라인.
"""

from __future__ import annotations

from orchestrator.scheduler import Scheduler


def _bare_scheduler() -> Scheduler:
    s = object.__new__(Scheduler)
    s._verify_failure_signatures = {}
    return s


def test_verify_signature_distinguishes_te_and_qa_source():
    s = _bare_scheduler()
    fields = {"failure_kind": "x", "command": "pytest"}
    # 같은 안정 필드라도 source 가 다르면 별개 스트림 → count 가 합산되지 않는다.
    assert s._remember_verify_failure("U", dict(fields), "test-engineer") == 1
    assert s._remember_verify_failure("U", dict(fields), "qa") == 1


def test_verify_signature_increments_on_same_source_repeat():
    s = _bare_scheduler()
    fields = {"failure_kind": "x", "command": "pytest"}
    # 같은 source 가 같은 방식으로 반복되면 정상적으로 count 증가(에스컬레이션 발화 보장).
    assert s._remember_verify_failure("U", dict(fields), "qa") == 1
    assert s._remember_verify_failure("U", dict(fields), "qa") == 2
    assert s._remember_verify_failure("U", dict(fields), "qa") == 3


def test_index_html_accepts_html_spec_upload():
    from orchestrator import webui

    # 파일 입력 accept 에 html 계열이 포함되어야 한다.
    assert ".html" in webui.INDEX_HTML
    assert ".htm" in webui.INDEX_HTML
    assert 'accept=".md,.txt,.markdown,.html,.htm"' in webui.INDEX_HTML
