#!/usr/bin/env node
// Roster Apply — LOCAL RUNNER (single-binary, zero dependencies). Fills a reviewed, server-safety-gated
// application PLAN in the Chrome you already have, driven over the DevTools Protocol — your own IP and
// session, which is why it works where a datacenter headless browser is blocked. It fills, reports what
// needs you, and STOPS. It submits only with --submit, never while a required/eligibility field is open.
//
//   roster-apply login <token>              store your token (Roster → Account → Roster Apply → Copy token)
//   roster-apply list                       your prepared applications
//   roster-apply fill <id> [--submit]       open the form, fill it, stop before submit
//   roster-apply batch [id...] [--submit]   fill several (no ids = every prepared application)
//   roster-apply plan <apply-url> [--fill]  ask Roster to plan a URL, optionally fill it now
// Env: ROSTER_TOKEN, ROSTER_BASE (default prod).
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";
import { classifyField, reviewFields, submitBlockedReason } from "./lib/plan.mjs";
import { launchChrome } from "./lib/chrome.mjs";
import { CDP } from "./lib/cdp.mjs";
import { FILL_SRC } from "./lib/fill-inject.mjs";

const DEFAULT_BASE = "https://roster-api-production-3405.up.railway.app";
const CFG = join(homedir(), ".roster", "apply-cli.json");

async function cfg() {
  let s = {}; try { s = JSON.parse(await readFile(CFG, "utf8")); } catch { /* first run */ }
  return { token: process.env.ROSTER_TOKEN || s.token || "", base: (process.env.ROSTER_BASE || s.base || DEFAULT_BASE).replace(/\/+$/, "") };
}
async function saveCfg(patch) {
  let s = {}; try { s = JSON.parse(await readFile(CFG, "utf8")); } catch { /* none */ }
  await mkdir(join(homedir(), ".roster"), { recursive: true });
  await writeFile(CFG, JSON.stringify({ ...s, ...patch }, null, 2));
}
async function api(path, { token, base }, opts = {}) {
  if (!token) throw new Error("Not connected. Run: roster-apply login <token>  (Roster → Account → Roster Apply → Copy token)");
  const r = await fetch(base + path, { ...opts, headers: { "X-Roster-Token": token, ...(opts.headers || {}) } });
  if (r.status === 401) throw new Error("Token rejected (401). Copy a fresh one from Roster → Account → Roster Apply, then: roster-apply login <token>");
  if (!r.ok) throw new Error(`Roster ${path} → ${r.status}: ${(await r.text().catch(() => "")).slice(0, 200)}`);
  return r;
}
async function getResume(c) {
  try {
    const rr = await api("/me/resume", c);
    const buf = Buffer.from(await rr.arrayBuffer());
    const cd = rr.headers.get("content-disposition") || "";
    const m = /filename="([^"]+)"/.exec(cd);
    return { b64: buf.toString("base64"), name: (m && m[1]) || "resume.pdf", type: rr.headers.get("content-type") || "application/pdf" };
  } catch { console.log("(no résumé on file — the file field will be left for you)"); return null; }
}

const SUBMIT_SRC = `(function(){var b=[].slice.call(document.querySelectorAll('button,input[type=submit],[role=button]')).find(function(el){var t=((el.textContent||'')+' '+(el.value||'')).toLowerCase().trim();return !el.disabled&&/(submit|apply|send application|finish|complete application)/.test(t)&&!/(save|cancel|back|previous|draft|sign in|log in|search)/.test(t);});if(b){try{b.scrollIntoView({block:'center'});}catch(e){}b.click();return true;}return false;})()`;

async function fillApp(cdp, app, resume, { submit }) {
  const url = app.form_url || app.url;
  console.log(`\n→ [#${app.id}] ${app.title || "role"} @ ${app.company || ""}\n  ${url}`);
  const sid = await cdp.openTab(url);
  const expr = `(${FILL_SRC})(${JSON.stringify(app.plan || [])}, ${JSON.stringify(resume)})`;
  const res = await cdp.evaluate(sid, expr).catch(e => ({ filled: [], needs: [], missing: ["(page error: " + e.message + ")"] }));
  console.log(`  filled ${res.filled.length}` + (res.needs.length ? `, ${res.needs.length} need you` : "") + (res.missing.length ? `, ${res.missing.length} not set` : ""));
  const rv = reviewFields(app.plan || []);
  if (rv.all.length) console.log(`  review before submit: ${rv.all.slice(0, 6).join(" · ")}`);
  if (res.missing.length) console.log(`  not auto-set: ${res.missing.slice(0, 6).join(" · ")}`);
  const blocked = submitBlockedReason(app.plan || []);
  if (submit && blocked) console.log(`  ⚠ not submitting — ${blocked}`);
  else if (submit) { const ok = await cdp.evaluate(sid, SUBMIT_SRC).catch(() => false); console.log(ok ? "  submitted (confirm on the page)" : "  couldn't find submit — submit on the page"); }
  return { filled: res.filled.length, needs: res.needs.length, missing: res.missing.length };
}

async function withBrowser(fn) {
  const { wsUrl } = await launchChrome();
  const cdp = await CDP.connect(wsUrl);
  try { await fn(cdp); } finally { cdp.close(); }   // the Chrome window stays open (detached) for review
  console.log("\nThe browser stays open — review each tab and submit. Close it when you're done.");
}

async function fillFlow(id, c, flags) {
  const app = await (await api(`/me/applications/${id}`, c)).json();
  const resume = await getResume(c);
  await withBrowser(cdp => fillApp(cdp, app, resume, { submit: flags.has("--submit") }));
}

async function batchFlow(ids, c, flags) {
  const all = (await (await api("/me/applications", c)).json()).applications || [];
  const open = all.filter(a => a.status !== "submitted");
  const want = ids.length ? open.filter(a => ids.includes(String(a.id))) : open;
  if (!want.length) { console.log(ids.length ? "None of those ids are prepared/unsubmitted." : 'Nothing prepared. Prepare roles in Roster first.'); return; }
  console.log(`Batch: ${want.length} application(s)${flags.has("--submit") ? " (submitting each unless flagged)" : " — fill only, you submit"}.`);
  const resume = await getResume(c);
  const t = { filled: 0, needs: 0, missing: 0 };
  await withBrowser(async cdp => {
    for (const row of want) {
      try { const app = await (await api(`/me/applications/${row.id}`, c)).json(); const r = await fillApp(cdp, app, resume, { submit: flags.has("--submit") }); t.filled += r.filled; t.needs += r.needs; t.missing += r.missing; }
      catch (e) { console.log(`  [#${row.id}] error: ${e.message}`); }
      await new Promise(r => setTimeout(r, 1000));
    }
    console.log(`\nBatch done — ${want.length} tab(s): filled ${t.filled}, ${t.needs} need review, ${t.missing} not auto-set.`);
  });
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const flags = new Set(rest.filter(a => a.startsWith("--")));
  const args = rest.filter(a => !a.startsWith("--"));
  const c = await cfg();
  try {
    if (cmd === "login") { if (!args[0]) throw new Error("Usage: roster-apply login <token>"); await saveCfg({ token: args[0] }); console.log("Saved. Try: roster-apply list"); }
    else if (cmd === "list") {
      const apps = (await (await api("/me/applications", c)).json()).applications || [];
      const open = apps.filter(a => a.status !== "submitted");
      if (!open.length) { console.log('No prepared applications. In Roster, press "Prepare application" on a job card first.'); return; }
      for (const a of open) console.log(`  ${a.id}\t${a.status}\t${a.title || "role"} @ ${a.company || ""}`);
      console.log("\nFill one: roster-apply fill <id>   ·   fill all: roster-apply batch");
    }
    else if (cmd === "fill") { if (!args[0]) throw new Error("Usage: roster-apply fill <id> [--submit]"); await fillFlow(args[0], c, flags); }
    else if (cmd === "batch") { await batchFlow(args, c, flags); }
    else if (cmd === "plan") {
      if (!args[0]) throw new Error("Usage: roster-apply plan <apply-url> [--fill]");
      const d = await (await api("/me/applications", c, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ job_url: args[0] }) })).json();
      if (d.already_applied) { console.log("You already applied to this posting."); return; }
      console.log(`Planned #${d.id} — ${d.n_questions} questions, ${(d.blocking || []).length} need you.`);
      if (flags.has("--fill")) await fillFlow(d.id, c, flags); else console.log(`Fill it: roster-apply fill ${d.id}`);
    }
    else console.log("Roster Apply — commands:\n  login <token> | list | fill <id> [--submit]\n  batch [id...] [--submit]   (no ids = every prepared application)\n  plan <url> [--fill]");
  } catch (e) { console.error("Error:", e.message); process.exit(1); }
}
main();
