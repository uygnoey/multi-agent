# 멀티 에이전트 개발팀 오케스트레이터

**한국어** · [English](README.en.md)

기획서(spec) 하나를 입력하면, **10개 역할 에이전트로 구성된 가상 개발팀**이 협업하여
별도 타깃 디렉터리에 웹서비스/플랫폼을 만들어내는 멀티 에이전트 오케스트레이터.
(패키지명: `web-team-orchestrator`)

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
  (`claude-cli` = **Claude Code**. 별칭 허용: `claude-code`→claude-cli, `openai-sdk`→openai-agents.
  가용성은 `--check` / TUI `c` 키 / 웹 상태 패널에서 확인.)
- **4종 동시 사용 + 우선순위 + 폴오버**: 한 런에서 백엔드를 풀로 묶어 동시에 쓴다.
  ```bash
  # 우선순위(폴오버): claude-cli 우선, 실패 시 codex→claude-sdk→openai-agents
  --backends claude-cli,codex,claude-sdk,openai-agents
  # 분산: 역할마다 다른 백엔드를 1순위로 라운드로빈 → 4종 동시 가동(+폴오버 유지)
  --backends claude-cli,codex,claude-sdk,openai-agents --distribute
  # 교차: 역할을 풀에 번갈아(교차) 배정 → 두 모델이 섞여 서로 검증 (그룹 하드코딩 없음).
  #   --role-backend 로 지정한 역할은 그대로 따르고, 나머지는 자동 교차.
  --backends codex,claude-cli --cross-check
  --role-backend qa=codex --cross-check        # qa=codex 고정, 나머지 자동 교차
  # 역할별 우선순위 지정도 가능
  --role-backend frontend-developer=codex,claude-cli --role-backend dba=claude-sdk
  ```
  선택 규칙: 역할별 우선순위 > `--backends` 풀(분산 시 회전) > `--backend` 단일. 가용하지 않은
  백엔드는 자동 스킵, 호출 실패 시 다음 우선순위로 폴오버한다(모니터/`events.log`에 표시).
- **Team Agents (네이티브 서브에이전트, 두 방식)**: 같은 `.claude/agents/*.md` 정의를 실제 Claude Code 서브에이전트로도 활용한다.
  - **리드 디스패치 (`--backend claude-team`)**: 리드 세션이 `Task` 툴로 각 역할 서브에이전트를 네이티브 디스패치.
  - **역할 내 위임 (`--delegate`)**: 역할 세션이 동료(예: backend→dba)를 서브에이전트로 호출(깊이 1). claude-sdk 는 `ClaudeAgentOptions(agents=...)`, CLI 계열은 타깃 `.claude/agents/`(스캐폴딩 시 노출) + `Task` 툴로 동작.
- **조정 = 공유 보드**: `<project-dir>/.orchestrator/board.json` 의 단일 writer 는 오케스트레이터.
  역할 세션은 타깃 파일을 편집하고 결과 JSON 만 남긴다(4종 공통 "cwd 파일 편집" 계약).

## 워크플로우

```
스캐폴딩 → 보드 초기화 → PM/PL 상시 감독(백그라운드)
  → Phase A:  architect ‖ testsheet-creator              (병렬)
  → Phase B:  frontend ‖ backend ‖ dba   (unit별 동시) → dev_done
              dev 끝나면 test/qa 는 별도 태스크로 즉시(개발 슬롯 반납 → 다음 unit 진행)
  → Phase C:  test-engineer → qa   (QA 실패 시 max_attempts 내 재작업) → tested/done
  → Phase D:  cicd
  → Phase E:  docs-writer — 산출물 문서(ERD·시퀀스·DB·API·매뉴얼·배포·실행·아키텍처, EN/KO)
  → 감독(PM/PL) graceful 종료 후 done   (done = 모든 에이전트 종료 시점)
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
| `--backend NAME` | 전역 기본 백엔드 (단일). `--backends` 미지정 시 사용 |
| `--backends B1,B2,...` | **우선순위 풀**: 앞에서부터 가용 백엔드 사용, 실패 시 다음으로 폴오버 |
| `--distribute` | 역할들을 풀에 라운드로빈 **분산**(4종 동시 가동) |
| `--cross-check` | 역할을 풀에 **번갈아 교차** 배정(핀한 역할은 그대로) → 두 모델이 섞여 상호 검증 |
| `--role-backend ROLE=B1[,B2,...]` | 역할별 백엔드(우선순위 리스트) override (반복 가능) |
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
- **a**: 산출물(파일) 뷰 · **c**: 백엔드 체크 · **s**: 정지(실행 중) · **r**: 재실행(정지 상태에서만)
- 헤더에 phase·비용·units·**동시 실행 수**·상태(running/stopped), 에이전트별 **모델** 표시
- 헤드리스/CI: `python -m orchestrator.monitor --project-dir <dir> --once` 로 1회 텍스트 스냅샷

## 웹 UI

브라우저에서 **기획서 파일을 업로드해 실행**하고, 클릭 없이 진행을 실시간으로 본다.
의존성 0(stdlib `http.server`), 기본 `127.0.0.1`.

```bash
python -m orchestrator --web --port 8765          # 또는: web-team-web --port 8765
# 브라우저에서 http://localhost:8765 접속
```

- **백엔드 상태 패널**: 각 백엔드 가용성(✅/❌)·설명을 실행 전에 확인 (`/api/check`)
- **새 실행** 패널: 기획서(.md/.txt) 업로드 + 백엔드(1개=단일 / 콤마로 여러 개=폴오버·분산·교차)·
  동시성·mock·delegate·max-units → ▶ 실행
- **run picker**: 자동선택 없이 사용자가 run 을 골라야 대시보드 표시
- **대시보드(클릭 불필요)**: phase·비용(구독은 `est.`)·**토큰**·units·**동시 실행 수**·
  상태(**running / done / stopped**)
- **에이전트 카드**: 역할별 카드에 모델·비용·토큰·현재 unit + **실시간 로그(프롬프트·생각·응답 스트리밍)** 내장
- **정지/재실행**: 실행 중 ■ 정지(프로세스 그룹 종료), **정지/완료 후에만** ↻ 재실행
- 실행 결과물은 `--base-dir`(기본 `~/agent-runs`)/`<run-id>/` 에 생성됨
- 같은 데이터(`board.json` + `agents/<role>.log`)를 읽으므로 TUI와 표시가 일치
- ⚠️ 실 백엔드 선택 시 비용 발생(구독은 토큰 환산 `est.`) — 기본값은 `mock`. 외부 노출 시 인증/방화벽 직접 구성

## 산출물 위치 (타깃)

```
<project-dir>/
  CLAUDE.md  AGENTS.md          # 스캐폴딩된 공유 지침
  .claude/agents/*.md           # 노출된 팀 에이전트 (네이티브 서브에이전트)
  docs/design/  docs/test/      # 설계 / 테스트 시트
  docs/                          # 산출물 문서 — 각 영문(.md) + 한글(.ko.md):
    index ERD SEQUENCE DB_TABLES API USER_MANUAL DEPLOY RUN_GUIDE ARCHITECTURE DELIVERABLES
    # ERD/SEQUENCE/ARCHITECTURE 는 mermaid 다이어그램 포함 (사람이 보기 쉬움)
  backend/  frontend/  db/  tests/
  .github/workflows/ci.yml
  .orchestrator/                # 런 상태 (board.json, events.log, results/, directives.md, report.md)
```

## 안전장치 (프로덕션 하드닝)

- **세션 타임아웃** `--timeout`(기본 1200초): 멈춘 백엔드 호출을 끊고 폴오버 (모든 백엔드 적용)
- **폴오버/재시도**: 가용 백엔드만 선택, 전이성 실패 재시도, 다음 우선순위로 폴오버
- **deps 진행기반 대기**: 의존 unit 이 진행 중이면(상태/에이전트 활동 변화) 계속 대기,
  실패/blocked 면 즉시 차단, 진행이 완전히 멈추면 stall 윈도 후 실패 (재작업으로 오래 걸려도 안 막힘)
- **예외 격리**: 한 역할 실패가 다른 동시 역할을 취소시키지 않음 (run_role 비전파)
- **결과 무결성**: 백엔드 실패 시 남은 결과파일을 성공으로 오탐하지 않음, 보드는 단일 writer
- **웹 보안**: 경로 traversal 차단(run id 를 base_dir 로 한정·role 검증), 요청 바디 크기 상한
- 세션별 `max_turns`·예산, 전역 동시성 세마포어, `--max-units`, 경로 스코프(타깃 cwd 한정)

## 배포 (Docker)

웹 UI를 컨테이너로 띄운다 (mock 은 키 없이 즉시 동작):

```bash
docker build -t web-team .
docker run --rm -p 8765:8765 -v "$PWD/runs:/data/runs" web-team
# 브라우저: http://localhost:8765
```

프로덕션 주의:
- **실 백엔드**(claude-cli/codex/openai-agents/claude-sdk)는 각 CLI 설치·로그인 또는 API 키가 필요하다.
  컨테이너에 키를 `-e OPENAI_API_KEY=… -e ANTHROPIC_API_KEY=…` 로 주입하거나, CLI 인증 디렉터리를 마운트한다.
- 웹 UI는 인증이 없다 — 신뢰된 네트워크에서만 노출하거나 리버스 프록시 뒤에 인증을 두고, `--host` 바인딩을 제한한다.
- 산출물·런 상태는 `/data/runs`(볼륨)에 생성된다.
- CI: `.github/workflows/ci.yml` 가 lint(ruff)+test(pytest, 3.10–3.12)를 실행한다.
