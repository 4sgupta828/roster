#!/usr/bin/env node
// Roster Apply — LOCAL RUNNER (single-binary, zero deps). Fills a reviewed, server-safety-gated
// application PLAN in the Chrome you already have, over the DevTools Protocol — your own IP and session.
// Double-click it (or run `gui`) for a local control panel; or use the commands below. It fills and
// STOPS; it submits only with --submit and never while a required/eligibility field is open.
//
//   roster-apply                            open the local GUI (also when double-clicked)
//   roster-apply login <token>              store your token (Roster → Account → Roster Apply → Copy token)
//   roster-apply list                       your prepared applications
//   roster-apply fill <id> [--submit]       open the form, fill it, stop before submit
//   roster-apply batch [id...] [--submit]   fill several (no ids = every prepared application)
//   roster-apply plan <apply-url> [--fill]  ask Roster to plan a URL, optionally fill it now
// Env: ROSTER_TOKEN, ROSTER_BASE (default prod).
import { cfg, saveCfg, api, getResume } from "./lib/api.mjs";
import { connectBrowser, fillApp } from "./lib/drive.mjs";

async function fillFlow(id, c, flags) {
  const app = await (await api(`/me/applications/${id}`, c)).json();
  const resume = await getResume(c);
  const cdp = await connectBrowser();
  try { await fillApp(cdp, app, resume, { submit: flags.has("--submit") }, console.log); }
  finally { cdp.close(); console.log("\nThe browser stays open — review and submit. Close it when you're done."); }
}

async function batchFlow(ids, c, flags) {
  const all = (await (await api("/me/applications", c)).json()).applications || [];
  const open = all.filter(a => a.status !== "submitted");
  const want = ids.length ? open.filter(a => ids.includes(String(a.id))) : open;
  if (!want.length) { console.log(ids.length ? "None of those ids are prepared/unsubmitted." : 'Nothing prepared. Click 🚀 Apply on jobs in Roster first.'); return; }
  console.log(`Batch: ${want.length} application(s)${flags.has("--submit") ? " (submitting each unless flagged)" : " — fill only, you submit"}.`);
  const resume = await getResume(c);
  const cdp = await connectBrowser();
  try {
    let filled = 0, needs = 0;
    for (const row of want) {
      try { const app = await (await api(`/me/applications/${row.id}`, c)).json(); const r = await fillApp(cdp, app, resume, { submit: flags.has("--submit") }, console.log); filled += r.filled; needs += r.needs; }
      catch (e) { console.log(`  [#${row.id}] error: ${e.message}`); }
      await new Promise(r => setTimeout(r, 900));
    }
    console.log(`\nBatch done — ${want.length} tab(s): filled ${filled}, ${needs} need review.`);
  } finally { cdp.close(); console.log("The browser stays open — review each tab and submit."); }
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const flags = new Set(rest.filter(a => a.startsWith("--")));
  const args = rest.filter(a => !a.startsWith("--"));
  const c = await cfg();
  try {
    // no command + not launched from a terminal (double-clicked) → open the GUI
    if ((!cmd && !process.stdin.isTTY) || cmd === "gui") { const { startGui } = await import("./lib/gui.mjs"); await startGui(); return; }
    if (cmd === "login") { if (!args[0]) throw new Error("Usage: roster-apply login <token>"); await saveCfg({ token: args[0] }); console.log("Saved. Try: roster-apply list  (or run with no arguments for the app)"); }
    else if (cmd === "list") {
      const apps = (await (await api("/me/applications", c)).json()).applications || [];
      const open = apps.filter(a => a.status !== "submitted");
      if (!open.length) { console.log('No prepared applications. In Roster, click 🚀 Apply on a job first.'); return; }
      for (const a of open) console.log(`  ${a.id}\t${a.status}\t${a.title || "role"} @ ${a.company || ""}`);
      console.log("\nFill one: roster-apply fill <id>   ·   fill all: roster-apply batch   ·   or run with no args for the app");
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
    else console.log("Roster Apply — run with no arguments for the app, or:\n  login <token> | list | fill <id> [--submit] | batch [id...] [--submit] | plan <url> [--fill]");
  } catch (e) { console.error("Error:", e.message); process.exit(1); }
}
main();
