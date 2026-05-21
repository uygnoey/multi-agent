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
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .backends import backend_status, resolve
from .config import BACKEND_INFO, FRAMEWORK_ROOT, ROLES, VALID_BACKENDS
from .monitor import _read_agent_log, _read_board


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip()).strip("-").lower()
    return s or "run"


def new_run_id(name: str) -> str:
    return f"{slugify(name)}-{time.strftime('%Y%m%d-%H%M%S')}"


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
        f = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 (lifetime = process)
        return subprocess.Popen(cmd, cwd=str(FRAMEWORK_ROOT), stdout=f, stderr=subprocess.STDOUT)

    def project_dir(self, run_id: str) -> Path:
        return self.base_dir / run_id

    def start(self, spec_text: str, opts: dict) -> str:
        run_id = new_run_id(opts.get("name", "run"))
        project = self.project_dir(run_id)
        project.mkdir(parents=True, exist_ok=True)
        spec_path = project / "_spec.md"
        spec_path.write_text(spec_text or "# (empty spec)\n", encoding="utf-8")
        cmd = build_command(sys.executable, spec_path, project, opts)
        proc = self._spawn(cmd, self.base_dir / f"{run_id}.log")
        with self._lock:
            self._procs[run_id] = proc
        return run_id

    def is_running(self, run_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(run_id)
        return bool(proc is not None and getattr(proc, "poll", lambda: 0)() is None)


# ----------------- HTTP -----------------


def _make_handler(manager: RunManager):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
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
                self._json({"runs": list_runs(manager.base_dir)})
            elif u.path == "/api/check":
                rows = backend_status()
                for r in rows:
                    r["info"] = BACKEND_INFO.get(r["name"], "")
                self._json({"backends": rows, "roles": list(ROLES)})
            elif u.path == "/api/state":
                run = (q.get("run") or [""])[0]
                board = _read_board(manager.project_dir(run) / ".orchestrator") if run else {}
                self._json(
                    {"roles": list(ROLES), "board": board, "running": manager.is_running(run)}
                )
            elif u.path == "/api/agent":
                run = (q.get("run") or [""])[0]
                role = (q.get("role") or [""])[0]
                orch = manager.project_dir(run) / ".orchestrator"
                board = _read_board(orch)
                self._json(
                    {
                        "agent": board.get("agents", {}).get(role, {}),
                        "log": _read_agent_log(orch, role),
                    }
                )
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            if u.path != "/api/run":
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "invalid json"}, 400)
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
  <!-- Launch -->
  <section id="launch" class="card">
    <h3 style="margin-top:0">새 실행 — 기획서 업로드</h3>
    <div class="row">
      <div><label>기획서 파일 (.md/.txt)</label><input type="file" id="specFile" accept=".md,.txt,.markdown"/></div>
      <div><label>실행 이름</label><input type="text" id="name" placeholder="my-app"/></div>
    </div>
    <div class="row">
      <div><label>백엔드</label><select id="backend"></select></div>
      <div><label>동시성</label><input type="text" id="concurrency" value="3"/></div>
      <div><label>max-units (선택)</label><input type="text" id="maxUnits" placeholder="전체"/></div>
      <div><label>max-attempts</label><input type="text" id="maxAttempts" value="2"/></div>
    </div>
    <div class="row">
      <div style="flex:4"><label>백엔드 우선순위 (콤마 · 비우면 위 단일 백엔드)</label>
        <input type="text" id="backends" placeholder="claude-cli,codex,claude-sdk,openai-agents"/></div>
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

  <!-- Monitor: list -->
  <section id="listView" class="card hide">
    <div class="row" style="align-items:center;margin-bottom:8px">
      <div class="muted">phase <b id="phase">—</b></div>
      <div class="muted">cost <b class="cost" id="cost">$0</b></div>
      <div class="muted">units <b id="units">0/0</b></div>
      <div class="muted">상태 <span class="pill" id="running">—</span></div>
    </div>
    <table><thead><tr><th>agent</th><th>state</th><th>$cost</th><th>calls</th><th>unit</th></tr></thead>
      <tbody id="agentRows"></tbody></table>
    <p class="muted">행을 클릭하면 그 에이전트가 실시간으로 무엇을 하는지 봅니다.</p>
  </section>

  <!-- Monitor: detail -->
  <section id="detailView" class="card hide">
    <button class="secondary" onclick="back()">← 뒤로 (리스트)</button>
    <h3 id="dRole" style="margin:12px 0 4px"></h3>
    <div class="row muted" style="margin-bottom:8px">
      <div>state <b id="dState">—</b></div><div>cost <b class="cost" id="dCost">$0</b></div>
      <div>calls <b id="dCalls">0</b></div><div>unit <b id="dUnit">—</b></div><div>backend <b id="dBackend">—</b></div>
    </div>
    <pre id="dLog">(활동 로그)</pre>
  </section>
</main>
<script>
let CUR=null, AGENT=null;
const $=id=>document.getElementById(id);
async function loadChecks(){
  let data={backends:[],roles:[]};
  try{data=await (await fetch("/api/check")).json()}catch(e){}
  const rows=data.backends||[];
  const sel=$("backend");sel.innerHTML="";
  rows.forEach(r=>{const o=document.createElement("option");o.value=r.name;
    o.text=r.name+" — "+r.info+(r.ok?"  ✅":"  ❌");sel.appendChild(o)});
  const bad=rows.filter(r=>!r.ok);
  $("backendStatus").innerHTML="백엔드: "+rows.map(r=>(r.ok?"✅":"❌")+" "+r.name).join("&nbsp;&nbsp;")+
    (bad.length?"<br>미가용 — "+bad.map(r=>r.name+": "+r.reason).join(" · "):"");
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
    backend:$("backend").value,backends:blist.length?blist:null,role_backends,
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
  const j=await (await fetch("/api/runs")).json();
  const sel=$("runSel");sel.innerHTML="";
  j.runs.forEach(r=>{const o=document.createElement("option");o.value=r.id;o.text=r.id;sel.appendChild(o)});
  if(CUR)sel.value=CUR;
}
function showLaunch(){CUR=null;AGENT=null;$("launch").classList.remove("hide");
  $("listView").classList.add("hide");$("detailView").classList.add("hide")}
function selectRun(id){if(!id)return;CUR=id;AGENT=null;$("runSel").value=id;
  $("launch").classList.add("hide");$("detailView").classList.add("hide");$("listView").classList.remove("hide")}
function openAgent(role){AGENT=role;$("listView").classList.add("hide");$("detailView").classList.remove("hide")}
function back(){AGENT=null;$("detailView").classList.add("hide");$("listView").classList.remove("hide")}

function statusDot(s){return '<span class="dot'+(s==="running"?" run":"")+'"></span>'}
async function tick(){
  if(!CUR)return;
  try{
    const s=await (await fetch("/api/state?run="+encodeURIComponent(CUR))).json();
    const b=s.board||{};const ag=b.agents||{};
    const done=(b.units||[]).filter(u=>u.status==="done").length;
    $("phase").textContent=b.phase||"—";
    $("cost").textContent="$"+(b.total_cost_usd||0).toFixed(4);
    $("units").textContent=done+"/"+((b.units||[]).length);
    $("running").textContent=s.running?"running":"stopped";
    if(!AGENT){
      $("agentRows").innerHTML=(s.roles||[]).map(r=>{const a=ag[r]||{};
        return '<tr class="agent" onclick="openAgent(\''+r+'\')"><td>'+statusDot(a.status)+r+
          '</td><td>'+(a.status||"idle")+'</td><td class="cost">$'+(+(a.cost_usd||0)).toFixed(4)+
          '</td><td>'+(a.calls||0)+'</td><td>'+(a.current_unit||"-")+'</td></tr>'}).join("");
    }else{
      const j=await (await fetch("/api/agent?run="+encodeURIComponent(CUR)+"&role="+AGENT)).json();
      const a=j.agent||{};
      $("dRole").textContent=AGENT;$("dState").textContent=a.status||"idle";
      $("dCost").textContent="$"+(+(a.cost_usd||0)).toFixed(4);$("dCalls").textContent=a.calls||0;
      $("dUnit").textContent=a.current_unit||"-";$("dBackend").textContent=a.backend||"-";
      let log=j.log||"(아직 활동 없음)";
      if(a.last_message)log+="\n\n── last message ──\n"+a.last_message;
      $("dLog").textContent=log;
    }
  }catch(e){}
}
loadChecks();refreshRuns();setInterval(tick,1000);
</script>
</body></html>
"""


if __name__ == "__main__":
    sys.exit(main())
