"""웹 UI: 브라우저에서 기획서를 업로드해 실행하고, 멀티에이전트 진행을 실시간으로 본다.

TUI(monitor.py)와 동일한 데이터 소스(`<project-dir>/.orchestrator/board.json`,
`agents/<role>.log`)를 읽으므로 기능이 일치한다 — 에이전트 리스트 → 클릭 상세
(실시간 활동·비용) → 뒤로. 의존성 0(stdlib http.server). 기본 127.0.0.1 바인딩.

사용:
  python -m orchestrator.webui --port 8765 [--base-dir ~/agent-runs]
  python -m orchestrator --web --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backends import backend_status, resolve
from .config import BACKEND_INFO, FRAMEWORK_ROOT, ROLES, VALID_BACKENDS
from .monitor import _read_agent_log, _read_board

MAX_BODY_BYTES = 4 * 1024 * 1024  # 요청 바디 상한 (메모리 고갈 방지)
MAX_SPEC_BYTES = 1024 * 1024  # 기획서 텍스트 상한


def _read_events(orch_dir, n: int = 300) -> str:
    """events.log 의 최근 n 줄 (통합 로그 패널용)."""
    p = orch_dir / "events.log"
    if not p.exists():
        return ""
    try:
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def _read_agent_logs(orch_dir, roles, n: int = 600) -> dict:
    """역할별 실시간 로그 tail {role: text} (활동 있는 역할만). 파일엔 전체가 저장됨."""
    out = {}
    ad = orch_dir / "agents"
    for role in roles:
        p = ad / f"{role}.log"
        if not p.exists():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
            if lines:
                out[role] = "\n".join(lines)
        except Exception:
            pass
    return out


def _run_alive(orch_dir) -> bool:
    """run.pid 의 프로세스가 살아있는지 (웹 서버 재시작/외부 실행에도 정확)."""
    pf = orch_dir / "run.pid"
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)  # signal 0 = 존재/권한 확인 (죽었으면 OSError)
    except OSError:
        return False
    return True


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return s or "run"


def new_run_id(name: str) -> str:
    # 같은 초 충돌 방지를 위해 짧은 랜덤 suffix 추가
    return f"{slugify(name)}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def build_command(py: str, spec_path: Path, project_dir: Path, opts: dict) -> list[str]:
    cmd = [
        py,
        "-m",
        "orchestrator",
        "--spec",
        str(spec_path),
        "--project-dir",
        str(project_dir),
        "--backend",
        str(opts.get("backend", "mock")),
        "--concurrency",
        str(int(opts.get("concurrency", 3))),
        "--poll-interval",
        "600",
    ]
    backends = opts.get("backends")
    if backends:
        cmd += ["--backends", ",".join(backends) if isinstance(backends, list) else str(backends)]
    for role, prov in (opts.get("role_backends") or {}).items():
        if prov:
            cmd += ["--role-backend", f"{role}={prov}"]
    if opts.get("distribute"):
        cmd.append("--distribute")
    if opts.get("cross_check"):
        cmd.append("--cross-check")
    if opts.get("mock"):
        cmd.append("--mock")
    if opts.get("delegate"):
        cmd.append("--delegate")
    if opts.get("max_units"):
        cmd += ["--max-units", str(int(opts["max_units"]))]
    if opts.get("max_attempts"):
        cmd += ["--max-attempts", str(int(opts["max_attempts"]))]
    return cmd


def list_runs(base_dir: Path) -> list[dict]:
    out = []
    if base_dir.exists():
        for d in sorted(base_dir.iterdir(), reverse=True):
            if (d / ".orchestrator" / "board.json").exists():
                out.append({"id": d.name, "project_dir": str(d)})
    return out


class RunManager:
    """기획서 텍스트로 오케스트레이터 서브프로세스를 띄우고 추적."""

    def __init__(self, base_dir: Path, spawn=None):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._spawn = spawn or self._default_spawn
        self._procs: dict[str, object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _default_spawn(cmd: list[str], log_path: Path):
        f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 (closed on reap)
        # start_new_session=True → 새 프로세스 그룹 → stop 시 자식까지 killpg 가능
        proc = subprocess.Popen(
            cmd, cwd=str(FRAMEWORK_ROOT), stdout=f, stderr=subprocess.STDOUT, start_new_session=True
        )
        proc._logfile = f  # is_running 에서 reap 시 close
        return proc

    def project_dir(self, run_id: str) -> Path:
        """run_id 를 base_dir 안으로 한정 (경로 traversal 차단)."""
        base = self.base_dir.resolve()
        p = (base / str(run_id)).resolve()
        if p != base and base not in p.parents:
            raise ValueError(f"invalid run id: {run_id!r}")
        return p

    def start(self, spec_text: str, opts: dict) -> str:
        run_id = new_run_id(opts.get("name", "run"))
        project = self.project_dir(run_id)
        project.mkdir(parents=True, exist_ok=True)
        spec_path = project / "_spec.md"
        spec_path.write_text(spec_text or "# (empty spec)\n", encoding="utf-8")
        # 재실행(rerun)용으로 옵션 저장
        try:
            (project / "_run_opts.json").write_text(
                json.dumps(opts, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
        cmd = build_command(sys.executable, spec_path, project, opts)
        proc = self._spawn(cmd, self.base_dir / f"{run_id}.log")
        with self._lock:
            self._procs[run_id] = proc
        return run_id

    def stop(self, run_id: str) -> bool:
        """run 의 프로세스 그룹을 종료. SIGTERM(유예) 후 SIGKILL(강제)로 확실히 종료.

        오케스트레이터가 SIGTERM 을 트랩(graceful)해 안 죽는 경우가 있어 SIGKILL 폴백 필수.
        """
        pid = None
        orch = self.project_dir(run_id) / ".orchestrator"
        pf = orch / "run.pid"
        if pf.exists():
            try:
                pid = int(pf.read_text(encoding="utf-8").strip())
            except Exception:
                pid = None
        with self._lock:
            proc = self._procs.get(run_id)
        if pid is None and proc is not None:
            pid = getattr(proc, "pid", None)
        if not pid:
            return False
        try:
            pgid = os.getpgid(pid)
        except Exception:
            pgid = None

        def _kill(sig):
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    os.kill(pid, sig)
            except Exception:
                pass

        _kill(signal.SIGTERM)
        threading.Timer(4.0, lambda: _kill(signal.SIGKILL)).start()  # 트랩 대비 강제 종료
        try:
            pf.unlink()  # 상태를 즉시 stopped 로 반영
        except Exception:
            pass
        return True

    def rerun(self, run_id: str) -> str:
        """저장된 spec + opts 로 새 run 을 시작."""
        project = self.project_dir(run_id)
        spec_text = ""
        sp = project / "_spec.md"
        if sp.exists():
            spec_text = sp.read_text(encoding="utf-8")
        opts = {"mock": True}
        op = project / "_run_opts.json"
        if op.exists():
            try:
                opts = json.loads(op.read_text(encoding="utf-8"))
            except Exception:
                pass
        return self.start(spec_text, opts)

    def is_running(self, run_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(run_id)
        if proc is not None:
            if getattr(proc, "poll", lambda: 0)() is None:
                return True
            # 종료됨 → 좀비 reap + 로그 핸들 close
            try:
                if hasattr(proc, "wait"):
                    proc.wait()
            except Exception:
                pass
            fh = getattr(proc, "_logfile", None)
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass
        # 이 서버가 띄우지 않은(고아/외부) run 도 PID 파일로 생존 확인
        try:
            return _run_alive(self.project_dir(run_id) / ".orchestrator")
        except ValueError:
            return False


# ----------------- HTTP -----------------


def _make_handler(manager: RunManager):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")  # 실시간: 캐시 금지
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(
                code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json"
            )

        def do_GET(self):
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/runs":
                runs = list_runs(manager.base_dir)
                for r in runs:
                    r["running"] = manager.is_running(r["id"])
                self._json({"runs": runs})
            elif u.path == "/api/check":
                rows = backend_status()
                for r in rows:
                    r["info"] = BACKEND_INFO.get(r["name"], "")
                self._json({"backends": rows, "roles": list(ROLES)})
            elif u.path == "/api/state":
                run = (q.get("run") or [""])[0]
                proj = None
                board = {}
                events = ""
                agent_logs = {}
                if run:
                    try:
                        proj = manager.project_dir(run)
                    except ValueError:
                        self._json({"error": "invalid run id"}, 400)
                        return
                    orch = proj / ".orchestrator"
                    board = _read_board(orch)
                    events = _read_events(orch)
                    agent_logs = _read_agent_logs(orch, list(ROLES))
                running = manager.is_running(run) if run else False
                # 런이 죽었는데 board 에 "running" 으로 남은 에이전트는 stopped 로 표시
                # (정상 종료 중 취소되거나 강제 종료된 경우 — 실제로는 안 돌고 있음)
                if run and not running:
                    for a in board.get("agents", {}).values():
                        if a.get("status") == "running":
                            a["status"] = "stopped"
                self._json(
                    {
                        "roles": list(ROLES),
                        "board": board,
                        "running": running,
                        "project_dir": str(proj) if proj else "",
                        "events": events,
                        "agent_logs": agent_logs if run else {},
                    }
                )
            elif u.path == "/api/agent":
                run = (q.get("run") or [""])[0]
                role = (q.get("role") or [""])[0]
                if role and role not in ROLES:  # 경로 traversal/임의 파일 읽기 차단
                    self._json({"error": "invalid role"}, 400)
                    return
                try:
                    orch = manager.project_dir(run) / ".orchestrator"
                except ValueError:
                    self._json({"error": "invalid run id"}, 400)
                    return
                board = _read_board(orch)
                self._json(
                    {
                        "agent": board.get("agents", {}).get(role, {}),
                        "log": _read_agent_log(orch, role) if role else "",
                    }
                )
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path not in ("/api/run", "/api/stop", "/api/rerun"):
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_BODY_BYTES:
                self._json({"error": f"body too large (> {MAX_BODY_BYTES} bytes)"}, 413)
                return
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "invalid json"}, 400)
                return

            if u.path == "/api/stop":
                run = data.get("run", "")
                try:
                    ok = manager.stop(run) if run else False
                except ValueError:
                    self._json({"error": "invalid run id"}, 400)
                    return
                self._json({"stopped": ok})
                return
            if u.path == "/api/rerun":
                run = data.get("run", "")
                try:
                    rid = manager.rerun(run)
                except ValueError:
                    self._json({"error": "invalid run id"}, 400)
                    return
                except Exception as e:
                    self._json({"error": str(e)}, 400)
                    return
                self._json({"run_id": rid})
                return

            # /api/run
            if len(data.get("spec_text", "") or "") > MAX_SPEC_BYTES:
                self._json({"error": f"spec too large (> {MAX_SPEC_BYTES} chars)"}, 413)
                return
            backend = data.get("backend", "mock")
            if backend not in VALID_BACKENDS:
                self._json({"error": f"invalid backend: {backend}"}, 400)
                return
            for b in data.get("backends") or []:
                if b not in VALID_BACKENDS:
                    self._json({"error": f"invalid backend in priority list: {b}"}, 400)
                    return
            for role, prov in (data.get("role_backends") or {}).items():
                if role not in ROLES:
                    self._json({"error": f"invalid role: {role}"}, 400)
                    return
                if prov and resolve(prov) not in VALID_BACKENDS:
                    self._json({"error": f"invalid backend for {role}: {prov}"}, 400)
                    return
            run_id = manager.start(data.get("spec_text", ""), data)
            self._json({"run_id": run_id})

    return Handler


def serve(port: int = 8765, base_dir: Path | None = None, host: str = "127.0.0.1") -> None:
    base = Path(base_dir) if base_dir else (Path.home() / "agent-runs")
    manager = RunManager(base)
    httpd = ThreadingHTTPServer((host, port), _make_handler(manager))
    print(f"web ui: http://{host}:{port}   (runs → {base})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="orchestrator.webui", description="멀티에이전트 웹 UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--base-dir", type=Path, help="실행 결과 디렉터리 (기본 ~/agent-runs)")
    a = p.parse_args(argv)
    serve(a.port, a.base_dir, a.host)
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Multi-Agent Console</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Apple SD Gothic Neo",sans-serif;
       background:#0d1117;color:#e6edf3}
  header{padding:12px 20px;border-bottom:1px solid #30363d;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
  h1{font-size:16px;margin:0}
  .muted{color:#8b949e;font-size:13px}
  main{padding:16px 20px;max-width:1000px;margin:0 auto}
  .card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;margin-bottom:16px}
  label{display:block;font-size:12px;color:#8b949e;margin:8px 0 4px}
  input,select{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:6px;padding:7px 9px;font-size:13px}
  input[type=text],select{width:100%}
  .row{display:flex;gap:12px;flex-wrap:wrap}
  .row>div{flex:1;min-width:140px}
  button{background:#238636;color:#fff;border:0;border-radius:6px;padding:9px 16px;font-size:14px;cursor:pointer}
  button.secondary{background:#21262d;border:1px solid #30363d}
  button:disabled{opacity:.5;cursor:default}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d}
  th{color:#8b949e;font-weight:600}
  tr.agent{cursor:pointer}
  tr.agent:hover{background:#1c2330}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:8px;background:#3d444d}
  .dot.run{background:#2ea043;box-shadow:0 0 7px #2ea043}
  .pill{font-size:11px;padding:2px 8px;border-radius:999px;background:#21262d;border:1px solid #30363d}
  pre{background:#010409;border:1px solid #30363d;border-radius:6px;padding:12px;overflow:auto;max-height:52vh;
      font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
  .hide{display:none}
  a{color:#58a6ff}
  .cost{color:#d29922;font-variant-numeric:tabular-nums}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px}
  .agent-card{background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px}
  .agent-card.run{border-color:#2ea043}
  .agent-card h5{margin:0 0 4px;font-size:14px}
  .agent-card .meta{font-size:11px;color:#8b949e;margin-bottom:6px}
  .agent-card pre{max-height:30vh;margin:0;font-size:11px}
  .badge{font-size:10px;padding:1px 6px;border-radius:999px;background:#21262d;border:1px solid #30363d;margin-left:6px}
</style></head>
<body>
<header>
  <h1>🤖 Multi-Agent Console</h1>
  <span class="muted" id="hdr">기획서를 업로드해 실행하세요.</span>
  <span style="flex:1"></span>
  <label style="margin:0">run</label>
  <select id="runSel" style="width:auto" onchange="selectRun(this.value)"></select>
  <button class="secondary" onclick="showLaunch()">+ 새 실행</button>
</header>
<main>
  <!-- Run picker (자동선택 안 함 — 사용자가 선택) -->
  <section id="picker" class="card hide">
    <h3 style="margin-top:0">실행(run) 선택</h3>
    <div id="runList" class="muted">불러오는 중…</div>
    <button class="secondary" style="margin-top:10px" onclick="showLaunch()">+ 새 실행</button>
  </section>

  <!-- Launch -->
  <section id="launch" class="card">
    <h3 style="margin-top:0">새 실행 — 기획서 업로드</h3>
    <div class="row">
      <div><label>기획서 파일 (.md/.txt)</label><input type="file" id="specFile" accept=".md,.txt,.markdown"/></div>
      <div><label>실행 이름</label><input type="text" id="name" placeholder="my-app"/></div>
    </div>
    <div class="row">
      <div style="flex:2"><label>백엔드 (우선순위 순, 콤마 · 1개=단일 / 여러 개=폴오버·분산·교차)</label>
        <input type="text" id="backends" placeholder="claude-cli   또는   claude-cli,codex"/></div>
      <div><label>동시성</label><input type="text" id="concurrency" value="3"/></div>
      <div><label>max-units (선택)</label><input type="text" id="maxUnits" placeholder="전체"/></div>
      <div><label>max-attempts</label><input type="text" id="maxAttempts" value="2"/></div>
    </div>
    <div id="backendStatus" class="muted" style="margin-top:8px;font-size:12px">백엔드 상태 확인 중…</div>
    <details style="margin-top:8px">
      <summary class="muted" style="cursor:pointer">역할별 프로바이더 직접 지정 (auto = 미지정 → cross-check 시 교차 배치)</summary>
      <div id="roleGrid" class="row" style="flex-wrap:wrap;margin-top:8px"></div>
    </details>
    <div class="row" style="margin-top:10px;align-items:center">
      <label style="margin:0"><input type="checkbox" id="mock" checked/> mock (무비용)</label>
      <label style="margin:0"><input type="checkbox" id="delegate"/> delegate (팀 위임)</label>
      <label style="margin:0"><input type="checkbox" id="distribute"/> distribute (풀 분산)</label>
      <label style="margin:0"><input type="checkbox" id="crossCheck"/> cross-check (교차 검증)</label>
      <span style="flex:1"></span>
      <button id="runBtn" onclick="startRun()">▶ 실행</button>
    </div>
    <p class="muted" id="launchMsg"></p>
  </section>

  <!-- Monitor: 단일 대시보드 (클릭 불필요, 모두 항상 표시) -->
  <section id="dash" class="card hide">
    <div class="row" style="align-items:center;margin-bottom:8px">
      <button class="secondary" onclick="showPicker()">← run 목록</button>
      <span style="flex:1"></span>
      <button id="stopBtn" class="secondary" onclick="stopRun()">■ 정지</button>
      <button id="rerunBtn" class="secondary" onclick="rerunRun()">↻ 재실행</button>
    </div>
    <div class="muted" style="margin-bottom:6px">
      📁 결과물 저장 위치: <b id="projDir" style="user-select:all">—</b>
    </div>
    <div class="row" style="align-items:center;margin-bottom:8px">
      <div class="muted">phase <b id="phase">—</b></div>
      <div class="muted">cost <b class="cost" id="cost">$0</b></div>
      <div class="muted">tokens <b id="tok">0</b></div>
      <div class="muted">units <b id="units">0/0</b></div>
      <div class="muted">동시 실행 <b id="runCount">0</b></div>
      <div class="muted">상태 <span class="pill" id="running">—</span></div>
    </div>

    <h4 style="margin:6px 0">에이전트 (카드 · 실시간 로그)</h4>
    <div id="agentCards" class="cards"></div>

    <h4 style="margin:14px 0 6px">통합 로그 (이벤트)</h4>
    <pre id="liveLog" style="max-height:26vh">(로그 대기 중…)</pre>

    <h4 style="margin:14px 0 6px">산출물 (생성된 파일)</h4>
    <div id="artifacts" class="muted" style="font-size:12px">(아직 없음)</div>
  </section>
</main>
<script>
let CUR=null;
const $=id=>document.getElementById(id);
async function loadChecks(){
  let data={backends:[],roles:[]};
  try{data=await (await fetch("/api/check")).json()}catch(e){}
  const rows=data.backends||[];
  const bad=rows.filter(r=>!r.ok);
  $("backendStatus").innerHTML="백엔드: "+rows.map(r=>(r.ok?"✅":"❌")+" "+r.name).join("&nbsp;&nbsp;")+
    (bad.length?"<br>미가용 — "+bad.map(r=>r.name+": "+r.reason).join(" · "):"");
  // 백엔드 입력칸이 비어있으면 가용한 첫 백엔드를 기본값으로 채움
  const okOnes=rows.filter(r=>r.ok).map(r=>r.name);
  if(!$("backends").value && okOnes.length) $("backends").value=okOnes[0];
  // 역할별 프로바이더 그리드 (auto + 백엔드들)
  const names=rows.map(r=>r.name);
  const grid=$("roleGrid");grid.innerHTML="";
  (data.roles||[]).forEach(role=>{
    const d=document.createElement("div");d.style.minWidth="210px";
    const s=document.createElement("select");s.dataset.role=role;s.style.width="100%";
    const a=document.createElement("option");a.value="";a.text="auto";s.appendChild(a);
    names.forEach(n=>{const o=document.createElement("option");o.value=n;o.text=n;s.appendChild(o)});
    const lab=document.createElement("label");lab.textContent=role;
    d.appendChild(lab);d.appendChild(s);grid.appendChild(d);
  });
}

async function startRun(){
  const f=$("specFile").files[0];
  if(!f){$("launchMsg").textContent="기획서 파일을 선택하세요.";return}
  const spec_text=await f.text();
  $("runBtn").disabled=true;$("launchMsg").textContent="실행 시작 중…";
  const blist=$("backends").value.split(",").map(s=>s.trim()).filter(Boolean);
  const role_backends={};
  document.querySelectorAll("#roleGrid select").forEach(s=>{if(s.value)role_backends[s.dataset.role]=s.value});
  const body={spec_text,name:$("name").value||f.name.replace(/\.[^.]+$/,""),
    backend:blist[0]||"mock",backends:blist.length>1?blist:null,role_backends,
    distribute:$("distribute").checked,cross_check:$("crossCheck").checked,
    concurrency:+$("concurrency").value||3,
    max_units:$("maxUnits").value?+$("maxUnits").value:null,
    max_attempts:+$("maxAttempts").value||2,mock:$("mock").checked,delegate:$("delegate").checked};
  const r=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const j=await r.json();$("runBtn").disabled=false;
  if(j.error){$("launchMsg").textContent="오류: "+j.error;return}
  await refreshRuns(); selectRun(j.run_id);
}
async function refreshRuns(){
  let runs=[];
  try{runs=((await (await fetch("/api/runs")).json()).runs)||[]}catch(e){}
  const sel=$("runSel");const prev=CUR;sel.innerHTML="";
  if(!runs.length){const o=document.createElement("option");o.value="";o.text="(실행 없음)";sel.appendChild(o)}
  runs.forEach(r=>{const o=document.createElement("option");o.value=r.id;
    o.text=r.id+(r.running?" ● running":"");sel.appendChild(o)});
  if(prev&&runs.some(r=>r.id===prev))sel.value=prev;
  return runs;
}
function showLaunch(){CUR=null;$("launch").classList.remove("hide");$("dash").classList.add("hide");$("picker").classList.add("hide")}
function showPicker(){CUR=null;$("picker").classList.remove("hide");$("launch").classList.add("hide");$("dash").classList.add("hide")}
function selectRun(id){if(!id)return;CUR=id;$("runSel").value=id;
  $("launch").classList.add("hide");$("picker").classList.add("hide");$("dash").classList.remove("hide");tick();}
function renderPicker(runs){
  $("runList").innerHTML=runs.length
    ? runs.map(r=>'<div style="padding:7px 0;cursor:pointer;border-bottom:1px solid #21262d" onclick="selectRun(\''+r.id+'\')">'+(r.running?"🟢 ":"⚪️ ")+esc(r.id)+'</div>').join("")
    : "(실행 없음 — 새 실행을 시작하세요)";
}
async function stopRun(){
  if(!CUR)return;
  if(!confirm("이 run 을 정지할까요?"))return;
  try{await fetch("/api/stop",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({run:CUR})})}catch(e){}
}
async function rerunRun(){
  if(!CUR)return;
  let j={};
  try{j=await (await fetch("/api/rerun",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({run:CUR})})).json()}catch(e){}
  if(j.run_id){await refreshRuns();selectRun(j.run_id)}else if(j.error){alert("재실행 오류: "+j.error)}
}

function statusDot(s){return '<span class="dot'+(s==="running"?" run":"")+'"></span>'}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function tick(){
  if(!CUR)return;
  try{
    const s=await (await fetch("/api/state?run="+encodeURIComponent(CUR))).json();
    const b=s.board||{};const ag=b.agents||{};const proj=s.project_dir||"";
    const done=(b.units||[]).filter(u=>u.status==="done").length;
    $("projDir").textContent=proj||"—";
    $("phase").textContent=b.phase||"—";
    $("cost").textContent="$"+(b.total_cost_usd||0).toFixed(4);
    $("tok").textContent=(b.total_tokens||0).toLocaleString();
    $("units").textContent=done+"/"+((b.units||[]).length);
    const runN=(s.roles||[]).filter(r=>(ag[r]||{}).status==="running").length;
    $("runCount").textContent=runN+"개";
    $("running").textContent=s.running?"running":"stopped";
    $("stopBtn").style.display=s.running?"":"none";
    // 에이전트 카드 — 각 카드에 모델·비용·유닛 + 실시간 로그(프롬프트·스트리밍·결과)
    const logs=s.agent_logs||{};
    $("agentCards").innerHTML=(s.roles||[]).map(r=>{const a=ag[r]||{};
      const run=a.status==="running";
      const meta=[(a.model||a.backend||"—"),"$"+(+(a.cost_usd||0)).toFixed(4),
        (a.tokens?(+a.tokens).toLocaleString()+" tok":null),"calls "+(a.calls||0),
        (a.current_unit&&a.current_unit!=="global")?("unit "+a.current_unit):null].filter(Boolean).join(" · ");
      return '<div class="agent-card'+(run?" run":"")+'"><h5>'+statusDot(a.status)+esc(r)+
        '<span class="badge">'+(a.status||"idle")+'</span></h5><div class="meta">'+esc(meta)+
        '</div><pre>'+esc(logs[r]||"(대기 중)")+'</pre></div>'}).join("");
    document.querySelectorAll("#agentCards pre").forEach(p=>{p.scrollTop=p.scrollHeight});
    // 통합 로그 — 자동 스크롤
    const log=$("liveLog");const atBottom=log.scrollTop+log.clientHeight>=log.scrollHeight-30;
    log.textContent=s.events||"(로그 대기 중…)";
    if(atBottom)log.scrollTop=log.scrollHeight;
    // 산출물 — 설계·공통 + unit별 (저장 경로)
    let html="<div>보드/런상태: <b style='user-select:all'>"+esc(proj)+"/.orchestrator/</b></div>";
    const g=b.artifacts||[];
    if(g.length)html+="<div style='margin-top:6px'><b>설계·공통</b><br>"+g.map(a=>"&nbsp;&nbsp;"+esc(proj)+"/"+esc(a)).join("<br>")+"</div>";
    (b.units||[]).forEach(u=>{const arts=u.artifacts||[];if(arts.length){
      html+="<div style='margin-top:6px'><b>"+esc(u.id)+"</b> ("+esc(u.status)+", "+arts.length+" files)<br>"+
        arts.map(a=>"&nbsp;&nbsp;"+esc(proj)+"/"+esc(a)).join("<br>")+"</div>"}});
    $("artifacts").innerHTML=html;
  }catch(e){}
}
let _tk=0;
async function loop(){
  if(_tk++%3===0){const runs=await refreshRuns();if(!CUR)renderPicker(runs);}  // 목록/picker 갱신
  if(CUR)tick();                                                                // 매 초 실시간 갱신
}
async function init(){
  await loadChecks();
  const runs=await refreshRuns();
  renderPicker(runs);
  showPicker();   // 자동선택 안 함 — 사용자가 run 을 선택해야 보인다
  setInterval(loop,1000);
}
init();
</script>
</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
