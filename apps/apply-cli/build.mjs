// Build a single-file executable of the CLI with Node's built-in SEA (Single Executable Application).
// No browser is bundled — the binary drives the Chrome the user already has, over CDP. Run: npm run build
// Produces dist/roster-apply (this OS/arch). Build once per target OS.
import { build } from "esbuild";
import { execFileSync, execSync } from "node:child_process";
import { copyFileSync, mkdirSync, writeFileSync, chmodSync, rmSync } from "node:fs";
import { join } from "node:path";

const OUT = "dist";
mkdirSync(OUT, { recursive: true });
const bundle = join(OUT, "bundle.cjs");
const seaConfig = join(OUT, "sea-config.json");
const blob = join(OUT, "sea.blob");
const isWin = process.platform === "win32";
const isMac = process.platform === "darwin";
const binName = isWin ? "roster-apply.exe" : "roster-apply";
const bin = join(OUT, binName);

console.log("1/5 bundling …");
await build({ entryPoints: ["apply.mjs"], bundle: true, platform: "node", format: "cjs", target: "node21", outfile: bundle, logLevel: "error" });

console.log("2/5 sea config …");
writeFileSync(seaConfig, JSON.stringify({ main: bundle, output: blob, disableExperimentalSEAWarning: true }));
execFileSync(process.execPath, ["--experimental-sea-config", seaConfig], { stdio: "inherit" });

console.log("3/5 copying node runtime …");
try { rmSync(bin, { force: true }); } catch (e) {}
copyFileSync(process.execPath, bin);
chmodSync(bin, 0o755);
if (isMac) { try { execSync(`codesign --remove-signature "${bin}"`); } catch (e) {} }

console.log("4/5 injecting app blob …");
const fuse = "NODE_SEA_FUSE_fce680ab2cc467b6e072b8b5df1996b2";
const postject = ["postject", bin, "NODE_SEA_BLOB", blob, "--sentinel-fuse", fuse];
if (isMac) postject.push("--macho-segment-name", "NODE_SEA");
execFileSync("npx", postject, { stdio: "inherit" });

console.log("5/5 finalizing …");
if (isMac) { try { execSync(`codesign --sign - "${bin}"`); } catch (e) { console.log("   (codesign skipped:", e.message, ")"); } }
if (!isWin) chmodSync(bin, 0o755);
console.log(`\n✓ Built ${bin}\nTry:  ${isWin ? bin : "./" + bin} login <token>   then   ${isWin ? bin : "./" + bin} batch`);
