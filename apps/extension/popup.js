const $ = s => document.querySelector(s);
const send = msg => new Promise(res => chrome.runtime.sendMessage(msg, res));

async function refresh() {
  const s = await chrome.storage.local.get(["roster_token"]);
  $("#connect").style.display = s.roster_token ? "none" : "block";
  if (!s.roster_token) return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const here = (tab && tab.url) || "";
  const r = await send({ type: "list" });
  if (!r || !r.ok) { $("#note").textContent = (r && r.error) || "Couldn't reach Roster."; return; }
  const apps = (r.applications || []).filter(a => a.status !== "submitted").slice(0, 12);
  const host = u => { try { return new URL(u).host; } catch (e) { return ""; } };
  const match = a => here && (here.startsWith((a.form_url || "").split("?")[0].slice(0, 60)) || (host(a.form_url || a.url) && here.includes(host(a.form_url || a.url))));
  apps.sort((a, b) => (match(b) ? 1 : 0) - (match(a) ? 1 : 0));
  $("#apps").innerHTML = apps.length ? apps.map(a => `<div class="app"><div><b>${esc(a.title || "role")}</b> <span class="muted">@ ${esc(a.company || "")}</span> <span class="st">${esc(a.status)}</span></div>
      <div class="row"><button class="primary fill" data-id="${a.id}" ${match(a) ? "" : 'title="Open its application form first"'}>${match(a) ? "Fill this page" : "Fill (open its form first)"}</button>
      <a class="muted" href="${esc(a.form_url || a.url)}" target="_blank">form ↗</a></div></div>`).join("")
    : `<div class="muted">No prepared applications. Use “📨 Prepare application” on a job card in Roster.</div>`;
  document.querySelectorAll(".fill").forEach(b => b.addEventListener("click", async () => {
    $("#note").textContent = "Filling…";
    const rr = await send({ type: "fill_active_tab", id: Number(b.dataset.id) });
    if (!rr || !rr.ok) { $("#note").textContent = (rr && rr.error) || "Fill failed."; return; }
    const res = rr.results || {};
    if (!res.filled) { $("#note").textContent = res.error || "The page didn't answer — is the application form open in this tab?"; return; }
    // NAME what needs the user. A count sends someone hunting a forty-field form.
    const need = res.needs_you || [];
    $("#note").textContent = `Filled ${res.filled.length}.`
      + (need.length ? ` ${need.length} need${need.length === 1 ? "s" : ""} you: ${need.slice(0, 4).join(", ")}${need.length > 4 ? "…" : ""} (outlined on the page).` : "")
      + " Review, then submit on the page yourself.";
  }));
}
function esc(s) { return String(s || "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
// COPY DIAGNOSTICS — the last fill, field by field. A bug report saying "some fields didn't take"
// cannot be acted on; this says which selector matched, what the field held immediately and a second
// later, whether the node survived a re-render, and what the page printed beside it.
const dbg = document.getElementById("diag");
if (dbg) dbg.addEventListener("click", async () => {
  const s = await chrome.storage.local.get(["roster_last_diag"]);
  const d = s.roster_last_diag;
  if (!d) { $("#note").textContent = "No fill recorded yet — press Fill this page first."; return; }
  try { await navigator.clipboard.writeText(JSON.stringify(d, null, 1));
        $("#note").textContent = `Copied the last fill (${(d.fields || []).length} fields) — paste it into the bug report.`; }
  catch (e) { $("#note").textContent = "Couldn't copy: " + (e && e.message || e); }
});
$("#save").addEventListener("click", async () => { const t = $("#token").value.trim(); if (!t) return; await chrome.storage.local.set({ roster_token: t }); refresh(); });
refresh();
