# Multi-Agent Dev-Team Orchestrator

[한국어](README.md) · **English**

Give it one planning spec, and a **virtual dev team of 10 role agents** collaborates to build a
web service/platform into a separate target directory — a multi-agent orchestrator.
(package name: `web-team-orchestrator`)

> **This repository is the framework (the tool).** The web-service output is not created here; it
> is generated inside the `--project-dir <target>` you pass at run time.

See [`docs/PLAN.md`](docs/PLAN.md) for the full design and [`docs/architecture.html`](docs/architecture.html) for the structure diagram.

## Core concepts

- **Hybrid**: roles are defined as `.claude/agents/*.md` (single source of truth for prompts, also
  usable from interactive Claude Code), and on top of that a Python asyncio orchestrator handles
  continuous supervision, concurrent execution, and phase triggering.
- **Multi-backend (2×2 + mock)**: role execution is hidden behind a `Backend` abstraction, so the
  four providers below can be freely mixed.

  | | API-key SDK | Subscription CLI |
  |---|---|---|
  | Anthropic | `claude-sdk` (Claude Agent SDK) | `claude-cli` (`claude -p`) |
  | OpenAI | `openai-agents` (OpenAI Agents SDK) | `codex` (`codex exec`) |

  Plus `claude-team` for native Team-Agents lead dispatch, and `mock` for zero-cost validation.
  (`claude-cli` = **Claude Code**. Aliases: `claude-code`→claude-cli, `openai-sdk`→openai-agents.
  Check availability via `--check` / TUI `c` key / web status panel.)
- **Use all four at once + priority + failover**: pool the backends in one run.
  ```bash
  # Priority (failover): claude-cli first, then codex→claude-sdk→openai-agents on failure
  --backends claude-cli,codex,claude-sdk,openai-agents
  # Distribute: round-robin a different backend as each role's first choice (all four active)
  --backends claude-cli,codex,claude-sdk,openai-agents --distribute
  # Cross-check: alternate roles across the pool so two models mix and verify each other
  #   (no hardcoded groups). Pinned roles (--role-backend) are kept; the rest auto-alternate.
  --backends codex,claude-cli --cross-check
  --role-backend qa=codex --cross-check        # pin qa=codex, the rest auto-cross
  # Per-role priority is also possible
  --role-backend frontend-developer=codex,claude-cli --role-backend dba=claude-sdk
  ```
  Selection rule: per-role priority > `--backends` pool (rotated when distributing) > single
  `--backend`. Unavailable backends are auto-skipped; on call failure it fails over to the next
  priority (shown in the monitor / `events.log`).
- **Team Agents (native subagents, two modes)**: the same `.claude/agents/*.md` definitions are also
  used as real Claude Code subagents.
  - **Lead dispatch (`--backend claude-team`)**: a lead session natively dispatches each role
    subagent via the `Task` tool.
  - **In-role delegation (`--delegate`)**: a role session calls a teammate (e.g. backend→dba) as a
    subagent (depth 1). claude-sdk uses `ClaudeAgentOptions(agents=...)`; CLI backends use the
    target's `.claude/agents/` (exposed during scaffolding) + the `Task` tool.
- **Coordination = shared board**: the single writer of `<project-dir>/.orchestrator/board.json` is
  the orchestrator. Role sessions edit target files and only leave a result JSON (a common "edit
  files in cwd" contract across all four backends).

## Workflow

```
scaffold → init board → PM/PL continuous supervision (background)
  → Phase A:  architect ‖ testsheet-creator              (parallel)
  → Phase B:  frontend ‖ backend ‖ dba   (concurrent per unit) → dev_done
              when dev finishes, test/qa run immediately as a separate task
              (the dev slot is released → development moves to the next unit)
  → Phase C:  test-engineer → qa   (rework within max_attempts on QA failure) → tested/done
  → Phase D:  cicd
  → Phase E:  docs-writer — deliverable docs (ERD, sequence, DB, API, manual, deploy, run, architecture; EN/KO)
  → done after supervisors (PM/PL) shut down gracefully   (done = the moment all agents have stopped)
```

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                 # core (enough for mock validation)
pip install -e ".[claude]"       # + Claude Agent SDK backend
pip install -e ".[openai]"       # + OpenAI Agents SDK backend
pip install -e ".[all]"          # both
```

CLI backends need separate install/login:
- `claude-cli`: `npm i -g @anthropic-ai/claude-code`, then log in (subscription) or set `ANTHROPIC_API_KEY`
- `codex`:      `npm i -g @openai/codex`, then `codex login` (ChatGPT subscription) or set `CODEX_API_KEY`

## Run

```bash
# 1) Diagnose backend availability
python -m orchestrator --check

# 2) Zero-cost smoke (validates the whole wiring without API keys)
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web --mock

# 3) Real mode (with available backends)
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web \
    --backend claude-cli --max-units 2 --concurrency 3 --budget 5

# Mixing backends per role is fine too
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web \
    --backend claude-sdk \
    --role-backend frontend-developer=codex \
    --role-backend backend-developer=openai-agents
```

## Key options

| Option | Description |
|---|---|
| `--spec PATH` | Planning spec markdown (required) |
| `--project-dir PATH` | Target output directory (required). A fresh directory is recommended |
| `--backend NAME` | Global default backend (single). Used when `--backends` is not given |
| `--backends B1,B2,...` | **Priority pool**: use available backends front-to-back, failing over on error |
| `--distribute` | **Distribute** roles round-robin across the pool (all four active) |
| `--cross-check` | **Alternate** roles across the pool (pinned roles kept) → two models mix and cross-verify |
| `--role-backend ROLE=B1[,B2,...]` | Per-role backend (priority list) override (repeatable) |
| `--delegate` | Role session delegates to teammates as native subagents (Claude backends) |
| `--mock` | Run every role with mock (zero cost) |
| `--concurrency N` | Number of units processed concurrently (default 3) |
| `--max-units N` | Cap on the number of units processed |
| `--max-attempts N` | Per-unit dev→test→qa rework attempts (default 2) |
| `--retries N` | Retries for transient role-call failures (default 1) |
| `--budget USD` | Per-session budget cap (supported backends) |
| `--model NAME` | Model override |
| `--poll-interval SEC` | PM/PL supervision interval (default 20s) |
| `--check` | Diagnose backend availability and exit |
| `--watch` | Instead of running, watch a `--project-dir` live via the monitor TUI |
| `--web [--port N]` | Run the web UI server (upload spec, run, and monitor in the browser) |

## Live monitor (TUI)

Watch the multi-agent run in real time (zero deps, stdlib `curses`). Typically used with **two
terminals** — one runs the build, the other monitors:

```bash
# Terminal A: run the build
python -m orchestrator --spec examples/specs/sample-spec.md --project-dir /tmp/demo-web --mock

# Terminal B: watch the same project-dir live
python -m orchestrator --watch --project-dir /tmp/demo-web
#  or:  python -m orchestrator.monitor --project-dir /tmp/demo-web
#  or (after install):  web-team-monitor --project-dir /tmp/demo-web
```

- **List view**: status (● running / ○ idle), cumulative cost, call count, current unit for all 10 roles
- **↑/↓** (or j/k) to move, **Enter** to open an agent's detail
- **Detail view**: what that agent is doing in real time (activity log) · cost · backend. The log
  **auto-follows the latest (tail)** and **soft-wraps** on narrow screens. **↑** pauses follow,
  **G** re-follows. **b/Esc** back, **q** quit
- **a**: artifacts (files) view · **c**: backend check · **s**: stop (while running) · **r**: rerun (only when stopped)
- Header shows phase · cost · tokens · units · **concurrent-running count** · status (running/done/stopped),
  and each agent's **model**
- Headless/CI: `python -m orchestrator.monitor --project-dir <dir> --once` prints a one-shot text snapshot

## Web UI

Upload a spec file in the browser to **run it**, and watch progress live without clicking.
Zero deps (stdlib `http.server`), binds `127.0.0.1` by default.

```bash
python -m orchestrator --web --port 8765          # or: web-team-web --port 8765
# open http://localhost:8765 in the browser
```

- **Backend status panel**: each backend's availability (✅/❌) and description before running (`/api/check`)
- **New run** panel: upload a spec (.md/.txt) + backend (one = single / comma-separated = failover·distribute·cross-check) ·
  concurrency · mock · delegate · max-units → ▶ Run
- **Run picker**: no auto-selection — you pick a run to show the dashboard
- **Dashboard (no clicking)**: phase · cost (`est.` on subscription) · **tokens** · units ·
  **concurrent-running count** · status (**running / done / stopped**)
- **Agent cards**: per-role cards with model · cost · tokens · current unit + **embedded live log
  (prompt · thinking · response streaming)**
- **Stop/Rerun**: ■ Stop while running (terminates the process group); ↻ Rerun **only when stopped/done**
- Outputs are created under `--base-dir` (default `~/agent-runs`)/`<run-id>/`
- Reads the same data (`board.json` + `agents/<role>.log`) as the TUI, so the displays match
- ⚠️ Real backends incur cost (subscriptions show a token-derived `est.`) — default is `mock`.
  When exposing externally, set up auth/firewall yourself

## Output location (target)

```
<project-dir>/
  CLAUDE.md  AGENTS.md          # scaffolded shared instructions
  .claude/agents/*.md           # exposed team agents (native subagents)
  docs/design/  docs/test/      # design / test sheets
  docs/                          # deliverable docs — each English (.md) + Korean (.ko.md):
    index ERD SEQUENCE DB_TABLES API USER_MANUAL DEPLOY RUN_GUIDE ARCHITECTURE DELIVERABLES
    # ERD/SEQUENCE/ARCHITECTURE include mermaid diagrams (human-readable)
  backend/  frontend/  db/  tests/
  .github/workflows/ci.yml
  .orchestrator/                # run state (board.json, events.log, results/, directives.md, report.md)
```

## Safety (production hardening)

- **Session timeout** `--timeout` (default 1200s): cuts off a stuck backend call and fails over (all backends)
- **Failover/retry**: pick only available backends, retry transient failures, fail over to the next priority
- **deps progress-based wait**: keep waiting while a dependency is progressing (status/agent activity
  changes); fast-fail on failed/blocked; fail only after a stall window if progress fully stops
  (so long reworks don't get blocked prematurely)
- **Exception isolation**: one role's failure does not cancel other concurrent roles (run_role never propagates)
- **Result integrity**: a leftover result file is not mistaken for success when the backend failed; the board is single-writer
- **Web security**: path-traversal blocking (run id confined to base_dir · role validation), request body size cap
- Per-session `max_turns`/budget, global concurrency semaphore, `--max-units`, path scoping (confined to target cwd)

## Deploy (Docker)

Run the web UI in a container (mock works instantly without keys):

```bash
docker build -t web-team .
docker run --rm -p 8765:8765 -v "$PWD/runs:/data/runs" web-team
# browser: http://localhost:8765
```

Production notes:
- **Real backends** (claude-cli/codex/openai-agents/claude-sdk) need each CLI installed/logged-in or an API key.
  Inject keys into the container with `-e OPENAI_API_KEY=… -e ANTHROPIC_API_KEY=…`, or mount the CLI auth directories.
- The web UI has no auth — expose it only on a trusted network, or put auth behind a reverse proxy and restrict the `--host` binding.
- Outputs/run state are created under `/data/runs` (volume).
- CI: `.github/workflows/ci.yml` runs lint (ruff) + test (pytest, 3.10–3.12).
