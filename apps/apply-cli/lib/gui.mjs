// Local GUI: the binary serves a small control panel on 127.0.0.1 and opens it in the browser. You see
// your prepared applications with Fill / Fill all / Submit buttons — no terminal. Execution is the same
// CDP drive as the CLI (your own Chrome). Dependency-free (Node's http).
//
// Safety: bound to loopback only; a random per-session key is embedded in the served page and required on
// every /api call, so another local process or a website cannot silently drive it (it can't read the key).
import { createServer } from "node:http";
import { randomBytes } from "node:crypto";
import { spawn } from "node:child_process";
import { cfg, saveCfg, api, getResume } from "./api.mjs";
import { connectBrowser, fillApp, readyToFill } from "./drive.mjs";

const PAGE = (key) => `<!doctype html><html><head><meta charset="utf-8"><title>Roster Apply</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--p:#6c5ce7}
 body{font:14px/1.5 system-ui;margin:0;background:#0f1117;color:#e7e9ee}
 .wrap{max-width:720px;margin:0 auto;padding:20px 16px}
 h1{font-size:18px;margin:0 0 4px}.sub{color:#9aa0ab;font-size:13px;margin:0 0 16px}
 .card{background:#171a22;border:1px solid #262b36;border-radius:12px;padding:12px 14px;margin:10px 0}
 .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 button{font:600 13px system-ui;padding:7px 12px;border-radius:8px;border:1px solid var(--p);background:var(--p);color:#fff;cursor:pointer}
 button.ghost{background:transparent;color:var(--p)} button:disabled{opacity:.5;cursor:default}
 input{flex:1;min-width:180px;padding:7px 9px;border-radius:8px;border:1px solid #333a48;background:#0f1117;color:#e7e9ee}
 .app{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:8px 0;border-top:1px solid #232834}
 .app:first-child{border-top:0}.muted{color:#9aa0ab;font-size:12px}
 label.chk{display:flex;gap:6px;align-items:center;color:#9aa0ab;font-size:13px}
 pre#log{background:#0b0d12;border:1px solid #232834;border-radius:8px;padding:10px;max-height:220px;overflow:auto;font-size:12px;white-space:pre-wrap;margin:10px 0 0}
 a{color:var(--p)}
</style></head><body><div class="wrap">
 <h1>Roster Apply</h1><p class="sub">Fills your prepared applications in your own Chrome. It never submits unless you tick Submit — and never over a field that still needs you.</p>
 <div class="card" id="connect" hidden>
   <div class="muted" style="margin-bottom:6px">Paste your token — Roster → Account → “Roster Apply” → Copy token.</div>
   <div class="row"><input id="token" type="password" placeholder="Roster token"><button onclick="login()">Connect</button></div>
 </div>
 <div class="card" id="panel" hidden>
   <div class="row" style="justify-content:space-between"><div><b id="who">Connected</b> · <span id="count" class="muted"></span></div>
     <div class="row"><label class="chk"><input type="checkbox" id="submit"> also submit</label>
       <button onclick="batch()" id="batchbtn">Fill all</button>
       <button class="ghost" onclick="refresh()">Refresh</button>
       <button class="ghost" onclick="disconnect()">Change token</button></div></div>
   <div id="apps"></div>
 </div>
 <pre id="log" hidden></pre>
</div>
<script>
const KEY="${key}"; const $=s=>document.querySelector(s);
async function call(path,body){const r=await fetch(path,{method:body?"POST":"GET",headers:{"X-GUI-Key":KEY,"content-type":"application/json"},body:body?JSON.stringify(body):undefined});const d=await r.json().catch(()=>({}));if(!r.ok)throw new Error(d.error||("HTTP "+r.status));return d;}
function log(s){const el=$("#log");el.hidden=false;el.textContent+=s+"\\n";el.scrollTop=el.scrollHeight;}
function esc(s){return String(s==null?"":s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
async function refresh(){try{const d=await call("/api/state");render(d);}catch(e){log("Error: "+e.message);}}
function render(d){
 $("#connect").hidden=d.connected; $("#panel").hidden=!d.connected;
 if(!d.connected)return;
 $("#count").textContent=(d.apps||[]).length+" ready to fill";
 $("#apps").innerHTML=(d.apps||[]).length?"":'<div class="muted" style="padding:8px 0">Nothing ready to fill. In Roster, click 🚀 Apply on a job to prepare it, then Refresh.</div>';
 for(const a of d.apps||[]){const el=document.createElement("div");el.className="app";
   el.innerHTML='<div><b>'+esc(a.title||"role")+'</b> <span class="muted">@ '+esc(a.company||"")+' · '+esc(a.status)+'</span></div>';
   const b=document.createElement("button");b.textContent="Fill";b.onclick=()=>fill(a.id);el.appendChild(b);$("#apps").appendChild(el);}
}
async function login(){const t=$("#token").value.trim();if(!t)return;try{await call("/api/login",{token:t});await refresh();}catch(e){log("Error: "+e.message);}}
async function disconnect(){await call("/api/login",{token:""});await refresh();}
async function fill(id){busy(true);log("Filling #"+id+" …");try{const d=await call("/api/fill",{id:id,submit:$("#submit").checked});log("  #"+id+": filled "+d.filled+(d.review&&d.review.length?" · review: "+d.review.join(", "):"")+(d.submitted?" · submitted":""));}catch(e){log("  Error: "+e.message);}busy(false);}
async function batch(){busy(true);log("Filling all prepared …");try{const d=await call("/api/batch",{submit:$("#submit").checked});log("Done — "+d.total+" filled, "+d.needs+" need review.");}catch(e){log("Error: "+e.message);}busy(false);}
function busy(b){document.querySelectorAll("button").forEach(x=>x.disabled=b);}
refresh();
</script></body></html>`;

async function readBody(req) {
  const chunks = []; for await (const c of req) chunks.push(c);
  try { return JSON.parse(Buffer.concat(chunks).toString() || "{}"); } catch { return {}; }
}

export async function startGui() {
  const key = randomBytes(16).toString("hex");
  let c = await cfg();
  let cdp = null, resume = undefined, busy = false;
  const browser = async () => { if (!cdp) { cdp = await connectBrowser(); } return cdp; };
  const state = async () => {
    if (!c.token) return { connected: false };
    try { const apps = (await (await api("/me/applications", c)).json()).applications || []; return { connected: true, apps: readyToFill(apps) }; }
    catch (e) { return { connected: false, error: e.message }; }
  };
  const json = (res, code, obj) => { res.writeHead(code, { "content-type": "application/json" }); res.end(JSON.stringify(obj)); };

  const server = createServer(async (req, res) => {
    const host = (req.headers.host || "").split(":")[0];
    if (host !== "127.0.0.1" && host !== "localhost") { res.writeHead(403); return res.end("forbidden"); }
    const url = req.url.split("?")[0];
    if (req.method === "GET" && url === "/") { res.writeHead(200, { "content-type": "text/html" }); return res.end(PAGE(key)); }
    if (!url.startsWith("/api/")) { res.writeHead(404); return res.end("not found"); }
    if (req.headers["x-gui-key"] !== key) { res.writeHead(403); return res.end("bad key"); }   // blocks blind cross-site POSTs
    try {
      if (url === "/api/state") return json(res, 200, await state());
      const body = await readBody(req);
      if (url === "/api/login") { c = { ...c, token: String(body.token || "") }; await saveCfg({ token: c.token }); return json(res, 200, { ok: true }); }
      if (busy) return json(res, 409, { error: "A fill is already running — wait for it to finish." });
      if (url === "/api/fill" || url === "/api/batch") {
        busy = true;
        try {
          if (resume === undefined) resume = await getResume(c);
          const cdpc = await browser();
          if (url === "/api/fill") {
            const app = await (await api(`/me/applications/${body.id}`, c)).json();
            return json(res, 200, await fillApp(cdpc, app, resume, { submit: !!body.submit }, () => {}, c));
          }
          const apps = (await (await api("/me/applications", c)).json()).applications || [];
          const open = readyToFill(apps);
          const t = { total: 0, needs: 0 };
          for (const row of open) { const app = await (await api(`/me/applications/${row.id}`, c)).json(); const r = await fillApp(cdpc, app, resume, { submit: !!body.submit }, () => {}, c); t.total += r.filled; t.needs += r.needs; await new Promise(r => setTimeout(r, 900)); }
          return json(res, 200, t);
        } finally { busy = false; }
      }
      json(res, 404, { error: "unknown endpoint" });
    } catch (e) { json(res, 500, { error: e.message }); }
  });

  const port = await new Promise(r => { server.listen(0, "127.0.0.1", () => r(server.address().port)); });
  const gurl = `http://127.0.0.1:${port}/`;
  console.log(`Roster Apply is running at ${gurl}`);
  if (!process.env.ROSTER_GUI_NO_OPEN) openBrowser(gurl);
  console.log("Leave this window open. Press Ctrl+C to quit.");
}

function openBrowser(url) {
  const cmd = process.platform === "darwin" ? "open" : process.platform === "win32" ? "cmd" : "xdg-open";
  const args = process.platform === "win32" ? ["/c", "start", "", url] : [url];
  try { spawn(cmd, args, { stdio: "ignore", detached: true }).unref(); } catch (e) { console.log("Open this in your browser:", url); }
}
