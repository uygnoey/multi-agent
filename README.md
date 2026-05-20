# web-team-orchestrator

기획서(spec) 하나를 입력하면, **10개 역할 에이전트로 구성된 가상 개발팀**이 협업하여
별도 타깃 디렉터리에 웹서비스/플랫폼을 만들어내는 멀티 에이전트 오케스트레이터.

> **이 저장소는 프레임워크(도구)다.** 웹서비스 결과물은 여기 안에 생기지 않고,
> 실행 시 지정한 `--project-dir <타깃>` 안에 생성된다.

전체 설계는 [`docs/PLAN.md`](docs/PLAN.md), 구조 다이어그램은 [`docs/architecture.html`](docs/architecture.html) 참고.

## 핵심 개념

- **하이브리드**: 역할은 `.claude/agents/*.md`(프롬프트 단일 출처, 대화형 Claude Code 에서도 사용)로 정의하고,
  그 위에서 Python asyncio 오케스트레이터가 상시 감독·동시 실행·단계 트리거를 담당.
- **멀티 백엔드 (2×2 + mock)**: 역할 실행을 `Backend` 추상화 뒤로 숨겨, 아래 4종을 자유롭게 섞어 쓴다.

  | | API 키 SDK | 구독형 CLI |
  |---|---|---|
  | Anthropic | `claude-sdk` (Claude Agent SDK) | `claude-cli` (`claude -p`) |
  | OpenAI | `openai-agents` (OpenAI Agents SDK) | `codex` (`codex exec`) |

  무비용 검증용 `mock` 백엔드 포함.
- **조정 = 공유 보드**: `<project-dir>/.orchestrator/board.json` 의 단일 writer 는 오케스트레이터.
  역할 세션은 타깃 파일을 편집하고 결과 JSON 만 남긴다(4종 공통 "cwd 파일 편집" 계약).

## 워크플로우

```
스캐폴딩 → 보드 초기화 → PM/PL 상시 감독(백그라운드)
  → Phase A:  architect ‖ testsheet-creator           (병렬)
  → Phase B:  frontend ‖ backend ‖ dba   (unit별 동시) → dev_done
  → Phase C:  test-engineer → qa          (unit 완료 시 트리거) → tested/done
  → Phase D:  cicd
```

## 설치

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # 코어 (mock 검증까지 가능)
pip install -e ".[claude]"       # + Claude Agent SDK 백엔드
pip install -e ".[openai]"       # + OpenAI Agents SDK 백엔드
pip install -e ".[all]"          # 둘 다
```

CLI 백엔드는 별도 설치/로그인 필요:
- `claude-cli`: `npm i -g @anthropic-ai/claude-code` 후 로그인(구독) 또는 `ANTHROPIC_API_KEY`
- `codex`:      `npm i -g @openai/codex` 후 `codex login`(ChatGPT 구독) 또는 `CODEX_API_KEY`

## 실행

```bash
# 1) 백엔드 가용성 진단
python -m orchestrator --check

# 2) 무비용 스모크 (API 키 없이 전체 배선 검증)
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web --mock

# 3) 실모드 (가용 백엔드로)
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web \
    --backend claude-cli --max-units 2 --concurrency 3 --budget 5

# 역할별 백엔드 혼합도 가능
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web \
    --backend claude-sdk \
    --role-backend frontend-developer=codex \
    --role-backend backend-developer=openai-agents
```

## 주요 옵션

| 옵션 | 설명 |
|---|---|
| `--spec PATH` | 기획서 마크다운 (필수) |
| `--project-dir PATH` | 산출물 타깃 디렉터리 (필수). 새 디렉터리 권장 |
| `--backend NAME` | 전역 기본 백엔드 (`claude-sdk`/`claude-cli`/`openai-agents`/`codex`/`mock`) |
| `--role-backend ROLE=BACKEND` | 역할별 override (반복 가능) |
| `--mock` | 모든 역할을 mock 으로 (무비용) |
| `--concurrency N` | 동시 처리 unit 수 (기본 3) |
| `--max-units N` | 처리할 unit 수 상한 |
| `--budget USD` | 세션당 예산 상한 (지원 백엔드) |
| `--model NAME` | 모델 override |
| `--poll-interval SEC` | PM/PL 감독 주기 (기본 20초) |
| `--check` | 백엔드 가용성만 진단 후 종료 |

## 산출물 위치 (타깃)

```
<project-dir>/
  CLAUDE.md  AGENTS.md          # 스캐폴딩된 공유 지침
  docs/design/  docs/test/      # 설계 / 테스트 시트
  backend/  frontend/  db/  tests/
  .github/workflows/ci.yml
  .orchestrator/                # 런 상태 (board.json, events.log, results/, directives.md)
```

## 안전장치

세션별 `max_turns`·예산, 전역 동시성 세마포어, `--max-units`,
경로 스코프(타깃 cwd 한정), 결과파일 단일 writer(보드는 오케스트레이터만 갱신).
