// Config + Roster API helpers, shared by the CLI and the GUI.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

export const DEFAULT_BASE = "https://roster-api-production-3405.up.railway.app";
const CFG = join(homedir(), ".roster", "apply-cli.json");

export async function cfg() {
  let s = {}; try { s = JSON.parse(await readFile(CFG, "utf8")); } catch { /* first run */ }
  return { token: process.env.ROSTER_TOKEN || s.token || "", base: (process.env.ROSTER_BASE || s.base || DEFAULT_BASE).replace(/\/+$/, "") };
}
export async function saveCfg(patch) {
  let s = {}; try { s = JSON.parse(await readFile(CFG, "utf8")); } catch { /* none */ }
  await mkdir(join(homedir(), ".roster"), { recursive: true });
  await writeFile(CFG, JSON.stringify({ ...s, ...patch }, null, 2));
}
export async function api(path, c, opts = {}) {
  if (!c.token) throw new Error("Not connected — paste your Roster token (Account → Roster Apply → Copy token).");
  const r = await fetch(c.base + path, { ...opts, headers: { "X-Roster-Token": c.token, ...(opts.headers || {}) } });
  if (r.status === 401) throw new Error("Token rejected (401). Copy a fresh one from Roster → Account → Roster Apply.");
  if (!r.ok) throw new Error(`Roster ${path} → ${r.status}: ${(await r.text().catch(() => "")).slice(0, 200)}`);
  return r;
}
export async function getResume(c) {
  try {
    const rr = await api("/me/resume", c);
    const buf = Buffer.from(await rr.arrayBuffer());
    const cd = rr.headers.get("content-disposition") || "";
    const m = /filename="([^"]+)"/.exec(cd);
    return { b64: buf.toString("base64"), name: (m && m[1]) || "resume.pdf", type: rr.headers.get("content-type") || "application/pdf" };
  } catch { return null; }
}
