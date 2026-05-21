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

  추가로 네이티브 Team Agents 리드 디스패치용 `claude-team`, 무비용 검증용 `mock` 백엔드 포함.
- **Team Agents (네이티브 서브에이전트, 두 방식)**: 같은 `.claude/agents/*.md` 정의를 실제 Claude Code 서브에이전트로도 활용한다.
  - **리드 디스패치 (`--backend claude-team`)**: 리드 세션이 `Task` 툴로 각 역할 서브에이전트를 네이티브 디스패치.
  - **역할 내 위임 (`--delegate`)**: 역할 세션이 동료(예: backend→dba)를 서브에이전트로 호출(깊이 1). claude-sdk 는 `ClaudeAgentOptions(agents=...)`, CLI 계열은 타깃 `.claude/agents/`(스캐폴딩 시 노출) + `Task` 툴로 동작.
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
| `--backend NAME` | 전역 기본 백엔드 (`claude-sdk`/`claude-cli`/`claude-team`/`openai-agents`/`codex`/`mock`) |
| `--role-backend ROLE=BACKEND` | 역할별 override (반복 가능) |
| `--delegate` | 역할 세션이 팀원을 네이티브 서브에이전트로 위임 호출 (Claude 백엔드) |
| `--mock` | 모든 역할을 mock 으로 (무비용) |
| `--concurrency N` | 동시 처리 unit 수 (기본 3) |
| `--max-units N` | 처리할 unit 수 상한 |
| `--max-attempts N` | unit별 dev→test→qa 재작업 횟수 (기본 2) |
| `--retries N` | 역할 호출 전이성 실패 재시도 횟수 (기본 1) |
| `--budget USD` | 세션당 예산 상한 (지원 백엔드) |
| `--model NAME` | 모델 override |
| `--poll-interval SEC` | PM/PL 감독 주기 (기본 20초) |
| `--check` | 백엔드 가용성만 진단 후 종료 |
| `--watch` | 실행 대신 `--project-dir` 진행을 실시간 모니터 TUI 로 본다 |
| `--web [--port N]` | 웹 UI 서버 실행 (브라우저에서 기획서 업로드·실행·모니터링) |

## 실시간 모니터 (TUI)

멀티에이전트가 도는 걸 실시간으로 본다 (의존성 0, stdlib `curses`). 보통 **터미널 2개**로 쓴다 —
하나는 빌드 실행, 다른 하나는 모니터:

```bash
# 터미널 A: 빌드 실행
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web --mock

# 터미널 B: 같은 project-dir 를 실시간 감시
python -m orchestrator --watch --project-dir /tmp/demo-web
#  또는:  python -m orchestrator.monitor --project-dir /tmp/demo-web
#  또는(설치 후):  web-team-monitor --project-dir /tmp/demo-web
```

- **리스트 뷰**: 10개 역할의 상태(● 실행중 / ○ 대기)·누적 비용·호출수·현재 unit
- **↑/↓**(또는 j/k) 이동, **Enter** 로 해당 에이전트 상세 진입
- **상세 뷰**: 그 에이전트가 실시간으로 무엇을 하는지(활동 로그)·비용·백엔드. **b/Esc** 로 리스트 복귀, **q** 종료
- 헤드리스/CI: `python -m orchestrator.monitor --project-dir <dir> --once` 로 1회 텍스트 스냅샷

## 웹 UI

브라우저에서 **기획서 파일을 업로드해 실행**하고, TUI와 동일한 기능(에이전트 리스트 →
클릭 상세(실시간 활동·비용) → 뒤로)을 본다. 의존성 0(stdlib `http.server`), 기본 `127.0.0.1`.

```bash
python -m orchestrator --web --port 8765          # 또는: web-team-web --port 8765
# 브라우저에서 http://localhost:8765 접속
```

- **새 실행** 패널: 기획서(.md/.txt) 업로드 + 백엔드·동시성·mock·delegate·max-units 설정 → ▶ 실행
- **모니터**: phase·총비용·units, 에이전트 표(● 실행중/○ 대기·$비용·calls·unit) → 행 클릭 시 상세
- **상세**: 그 에이전트의 실시간 활동 로그·비용·백엔드 → 뒤로 버튼으로 리스트 복귀
- 실행 결과물은 `--base-dir`(기본 `~/agent-runs`)/`<run-id>/` 에 생성됨
- 같은 데이터(`board.json` + `agents/<role>.log`)를 읽으므로 TUI와 표시가 일치
- ⚠️ 실 백엔드 선택 시 LLM 비용 발생 — 기본값은 `mock`. 외부 노출 시 인증/방화벽 직접 구성

## 산출물 위치 (타깃)

```
<project-dir>/
  CLAUDE.md  AGENTS.md          # 스캐폴딩된 공유 지침
  .claude/agents/*.md           # 노출된 팀 에이전트 (네이티브 서브에이전트)
  docs/design/  docs/test/      # 설계 / 테스트 시트
  backend/  frontend/  db/  tests/
  .github/workflows/ci.yml
  .orchestrator/                # 런 상태 (board.json, events.log, results/, directives.md, report.md)
```

## 안전장치

세션별 `max_turns`·예산, 전역 동시성 세마포어, `--max-units`,
경로 스코프(타깃 cwd 한정), 결과파일 단일 writer(보드는 오케스트레이터만 갱신).
