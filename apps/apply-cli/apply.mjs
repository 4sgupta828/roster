#!/usr/bin/env node
// Roster Apply — LOCAL RUNNER. Fills a reviewed application PLAN (built and safety-gated on the Roster
// server: eligibility facts walled off, answers span-checked, blacklist/rate-limit/knock-out applied) in
// a REAL browser on YOUR machine — your own IP and session, which is why it works where a datacenter
// headless browser is blocked. It fills, outlines what needs you, and STOPS. It submits only if you pass
// --submit, and never while a required or eligibility field is still open.
//
// Usage:
//   roster-apply login <token>                 store your token (Roster → Account → Roster Apply → Copy token)
//   roster-apply list                          your prepared applications
//   roster-apply fill <id>                     open the form, fill it, stop before submit
//   roster-apply fill <id> --submit            fill, then submit (only if nothing is flagged)
//   roster-apply plan <apply-url> [--fill]     ask Roster to plan a URL, optionally fill it right away
// Env: ROSTER_TOKEN, ROSTER_BASE (default prod).
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";
import { classifyField, selectorsOf, pickOption, reviewFields, submitBlockedReason, norm } from "./lib/plan.mjs";

const DEFAULT_BASE = "https://roster-api-production-3405.up.railway.app";
const CFG = join(homedir(), ".roster", "apply-cli.json");

async function cfg() {
  let stored = {};
  try { stored = JSON.parse(await readFile(CFG, "utf8")); } catch { /* first run */ }
  return {
    token: process.env.ROSTER_TOKEN || stored.token || "",
    base: (process.env.ROSTER_BASE || stored.base || DEFAULT_BASE).replace(/\/+$/, ""),
  };
}
async function saveCfg(patch) {
  let stored = {};
  try { stored = JSON.parse(await readFile(CFG, "utf8")); } catch { /* none */ }
  await mkdir(join(homedir(), ".roster"), { recursive: true });
  await writeFile(CFG, JSON.stringify({ ...stored, ...patch }, null, 2));
}
async function api(path, { token, base }, opts = {}) {
  if (!token) throw new Error("Not connected. Run: roster-apply login <token>  (Roster → Account → Roster Apply → Copy token)");
  const r = await fetch(base + path, { ...opts, headers: { "X-Roster-Token": token, ...(opts.headers || {}) } });
  if (r.status === 401) throw new Error("Token rejected (401). Copy a fresh one from Roster → Account → Roster Apply, then: roster-apply login <token>");
  if (!r.ok) throw new Error(`Roster ${path} → ${r.status}: ${(await r.text().catch(() => "")).slice(0, 200)}`);
  return r;
}

// ── the browser driver ────────────────────────────────────────────────────────────────────────────
// A field may live in the top document or in an embedded ATS iframe (Greenhouse embed). Try every
// selector across every frame and return the first VISIBLE, editable match.
async function locate(page, selectors) {
  for (const frame of page.frames()) {
    for (const sel of selectors) {
      try {
        const loc = frame.locator(sel).first();
        if (await loc.count() && await loc.isVisible().catch(() => false)) return loc;
      } catch { /* bad selector in this frame */ }
    }
  }
  return null;
}
// A field with no stable selector (Ashby's Location): the single editable input inside the box whose
// visible label starts with the question text.
async function locateByLabel(page, label) {
  const want = norm(label).slice(0, 50);
  if (!want) return null;
  for (const frame of page.frames()) {
    try {
      const lab = frame.locator(`text=/^${want.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/i`).first();
      if (!(await lab.count())) continue;
      const box = lab.locator("xpath=ancestor::*[.//input or .//textarea or .//select][1]");
      const input = box.locator("input:not([type=hidden]):not([type=file]), textarea, select").first();
      if (await input.count() && await input.isVisible().catch(() => false)) return input;
    } catch { /* keep trying frames */ }
  }
  return null;
}

async function fillCombo(loc, value) {
  // type into the search box, wait for the option, click the one that matches — a combobox is NOT set
  // by typing alone (its committed value lives elsewhere); the OPTION CLICK commits it.
  await loc.click();
  await loc.fill("");
  await loc.pressSequentially(value, { delay: 40 });   // human-paced keystrokes, so React & anti-bot are happy
  const page = loc.page();
  await page.waitForTimeout(600);
  const want = norm(value);
  for (const frame of page.frames()) {
    const opts = frame.locator("[role=option], li[role=option], .select__option, [class*='option']");
    const n = await opts.count().catch(() => 0);
    for (let i = 0; i < Math.min(n, 20); i++) {
      const o = opts.nth(i);
      const t = norm(await o.innerText().catch(() => ""));
      if (t && (t === want || t.startsWith(want) || t.includes(want))) { await o.click().catch(() => {}); return true; }
    }
  }
  await loc.press("Enter").catch(() => {});   // some accept the top match on Enter
  return false;
}

async function fillChoice(page, q) {
  const want = pickOption(q.options || [], q.answer) || q.answer;
  // native <select> first
  const sel = await locate(page, selectorsOf(q).map(s => s.startsWith("#") || s.includes("[") ? s : s));
  if (sel) {
    const tag = await sel.evaluate(el => el.tagName).catch(() => "");
    if (tag === "SELECT") { try { await sel.selectOption({ label: want }); return "filled"; } catch { /* fall through */ } }
  }
  // otherwise click the option by its label text, scoped to the question's box when we can find it
  for (const frame of page.frames()) {
    const opt = frame.getByText(want, { exact: false }).first();
    try { if (await opt.count() && await opt.isVisible()) { await opt.click(); return "filled"; } } catch { /* next */ }
  }
  return "missing";
}

async function fillOne(page, q, resumePath) {
  const kind = classifyField(q);
  if (kind === "skip" || kind === "none") return kind;
  if (kind === "file") {
    if (!resumePath) return "missing";
    for (const frame of page.frames()) {
      const fi = frame.locator("input[type=file]").first();
      try { if (await fi.count()) { await fi.setInputFiles(resumePath); return "filled"; } } catch { /* next */ }
    }
    return "missing";
  }
  const loc = (await locate(page, selectorsOf(q))) || (await locateByLabel(page, q.label));
  if (kind === "combo") {
    if (!loc) return "missing";
    return (await fillCombo(loc, q.answer)) ? "filled" : "entered";
  }
  if (kind === "choice") return await fillChoice(page, q);
  // text
  if (!loc) return "missing";
  try { await loc.click({ timeout: 2000 }); } catch { /* not clickable, still try to fill */ }
  try { await loc.fill(String(q.answer)); }
  catch { try { await loc.pressSequentially(String(q.answer), { delay: 20 }); } catch { return "missing"; } }
  return "filled";
}

async function runFill(app, resumePath, { submit }) {
  const { chromium } = await import("playwright");
  const url = app.form_url || app.url;
  const browser = await chromium.launch({ headless: false });
  const page = await browser.newPage();
  console.log(`\n→ Opening ${url}`);
  await page.goto(url, { waitUntil: "domcontentloaded" }).catch(e => console.log("  (navigation warning:", e.message, ")"));
  await page.waitForTimeout(1500);
  const filled = [], missing = [];
  for (const q of app.plan || []) {
    const r = await fillOne(page, q, resumePath).catch(() => "missing");
    if (r === "filled") { filled.push(q.label); process.stdout.write("."); }
    else if (r === "entered" || r === "missing") { missing.push(`${q.label} (${r})`); process.stdout.write("!"); }
  }
  console.log(`\n\nFilled ${filled.length} field(s).`);
  const rv = reviewFields(app.plan || []);
  if (rv.all.length) console.log(`Review before submitting (${rv.all.length}): ${rv.all.slice(0, 8).join(" · ")}`);
  if (missing.length) console.log(`Not set automatically (${missing.length}): ${missing.slice(0, 8).join(" · ")}`);
  const blocked = submitBlockedReason(app.plan || []);
  if (submit && blocked) console.log(`\n⚠ Not submitting — ${blocked}. Resolve these in the open window, then submit yourself.`);
  else if (submit) {
    console.log("\nSubmitting…");
    const btn = await locate(page, ['button[type=submit]', 'input[type=submit]', 'button:has-text("Submit")', 'button:has-text("Apply")']);
    if (btn) { await btn.click().catch(() => {}); console.log("Clicked submit. Confirm on the page."); }
    else console.log("Couldn't find a submit button — submit in the open window.");
  } else {
    console.log("\nReview the open window and submit it yourself (or re-run with --submit).");
  }
  console.log("The browser stays open. Close it when you're done.");
  // deliberately do NOT close the browser — the person finishes in it.
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const flags = new Set(rest.filter(a => a.startsWith("--")));
  const args = rest.filter(a => !a.startsWith("--"));
  const c = await cfg();
  try {
    if (cmd === "login") {
      if (!args[0]) throw new Error("Usage: roster-apply login <token>");
      await saveCfg({ token: args[0] });
      console.log("Saved. Try: roster-apply list");
    } else if (cmd === "list") {
      const apps = (await (await api("/me/applications", c)).json()).applications || [];
      const open = apps.filter(a => a.status !== "submitted");
      if (!open.length) { console.log('No prepared applications. In Roster, press "Prepare application" on a job card first.'); return; }
      for (const a of open) console.log(`  ${a.id}\t${a.status}\t${(a.title || "role")} @ ${a.company || ""}\n     \t${a.form_url || a.url}`);
      console.log("\nFill one with: roster-apply fill <id>");
    } else if (cmd === "plan") {
      if (!args[0]) throw new Error("Usage: roster-apply plan <apply-url> [--fill]");
      const d = await (await api("/me/applications", c, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ job_url: args[0] }) })).json();
      if (d.already_applied) { console.log("You already applied to this posting."); return; }
      console.log(`Planned application #${d.id} — ${d.n_questions} questions, ${(d.blocking || []).length} need you.`);
      if (flags.has("--fill")) await fillFlow(d.id, c, flags);
      else console.log(`Fill it with: roster-apply fill ${d.id}`);
    } else if (cmd === "fill") {
      if (!args[0]) throw new Error("Usage: roster-apply fill <id> [--submit]");
      await fillFlow(args[0], c, flags);
    } else {
      console.log("Roster Apply — commands: login <token> | list | fill <id> [--submit] | plan <url> [--fill]");
    }
  } catch (e) {
    console.error("Error:", e.message);
    process.exit(1);
  }
}

async function fillFlow(id, c, flags) {
  const app = await (await api(`/me/applications/${id}`, c)).json();
  // download the résumé to a temp file for the file upload
  let resumePath = null;
  try {
    const rr = await api("/me/resume", c);
    const buf = Buffer.from(await rr.arrayBuffer());
    const cd = rr.headers.get("content-disposition") || "";
    const m = /filename="([^"]+)"/.exec(cd);
    resumePath = join(tmpdir(), (m && m[1]) || "resume.pdf");
    await writeFile(resumePath, buf);
  } catch { console.log("(no résumé on file — the file field will be left for you)"); }
  await runFill(app, resumePath, { submit: flags.has("--submit") });
}

main();
