// Find and launch the user's INSTALLED Chrome with remote debugging — no bundled browser. Cross-platform.
import { spawn } from "node:child_process";
import { existsSync, readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execSync } from "node:child_process";

export function findChrome() {
  const p = process.platform;
  const cands = p === "darwin"
    ? ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
       "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
       "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    : p === "win32"
    ? [join(process.env["PROGRAMFILES"] || "C:/Program Files", "Google/Chrome/Application/chrome.exe"),
       join(process.env["PROGRAMFILES(X86)"] || "C:/Program Files (x86)", "Google/Chrome/Application/chrome.exe"),
       join(process.env["LOCALAPPDATA"] || "", "Google/Chrome/Application/chrome.exe")]
    : ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser", "/snap/bin/chromium"];
  for (const c of cands) if (c && existsSync(c)) return c;
  if (p !== "win32") { for (const n of ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]) { try { const r = execSync(`command -v ${n}`, { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(); if (r) return r; } catch (e) {} } }
  return "";
}

// Launch Chrome (visible), return { proc, wsUrl }. Uses a throwaway profile so it never disturbs the
// user's main Chrome; --remote-debugging-port=0 picks a free port, written to DevToolsActivePort.
export async function launchChrome() {
  const bin = findChrome();
  if (!bin) throw new Error("Google Chrome not found. Install Chrome (or set it as default) and try again.");
  const dir = mkdtempSync(join(tmpdir(), "roster-apply-"));
  const proc = spawn(bin, ["--remote-debugging-port=0", `--user-data-dir=${dir}`, "--no-first-run",
                           "--no-default-browser-check", "--disable-fre", "about:blank"],
                     { stdio: "ignore", detached: false });
  const portFile = join(dir, "DevToolsActivePort");
  let port = "";
  for (let i = 0; i < 100 && !port; i++) { try { const t = readFileSync(portFile, "utf8").split("\n"); if (t[0]) port = t[0].trim(); } catch (e) {} if (!port) await new Promise(r => setTimeout(r, 100)); }
  if (!port) { try { proc.kill(); } catch (e) {} throw new Error("Chrome did not start a debugging port."); }
  const info = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  return { proc, wsUrl: info.webSocketDebuggerUrl };
}
