// Roster Apply — background: talks to Roster with the user's token (stored by the popup), hands the
// plan and the résumé bytes to the content script. It has NO code path that submits a form.
const DEFAULT_BASE = "https://roster-api-production-3405.up.railway.app";

async function cfg() {
  const s = await chrome.storage.local.get(["roster_token", "roster_base"]);
  return { token: s.roster_token || "", base: (s.roster_base || DEFAULT_BASE).replace(/\/+$/, "") };
}

async function api(path, opts = {}) {
  const { token, base } = await cfg();
  if (!token) throw new Error("Not connected to Roster — open the extension popup and paste your Roster token.");
  const r = await fetch(base + path, { ...opts, headers: { ...(opts.headers || {}), "X-Roster-Token": token } });
  if (!r.ok) throw new Error(`Roster ${path} → ${r.status}`);
  return r;
}

async function getResume() {
  const r = await api("/me/resume");
  const buf = await r.arrayBuffer();
  const cd = r.headers.get("content-disposition") || "";
  const m = /filename="([^"]+)"/.exec(cd);
  let bin = ""; const bytes = new Uint8Array(buf); for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return { name: (m && m[1]) || "resume.pdf", type: r.headers.get("content-type") || "application/pdf", b64: btoa(bin) };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === "list") {
        const d = await (await api("/me/applications")).json();
        sendResponse({ ok: true, applications: d.applications || [] });
      } else if (msg.type === "plan") {
        const d = await (await api(`/me/applications/${msg.id}`)).json();
        sendResponse({ ok: true, application: d });
      } else if (msg.type === "resume") {
        sendResponse({ ok: true, ...(await getResume()) });
      } else if (msg.type === "executed") {
        await api(`/me/applications/${msg.id}/executed`, { method: "POST", headers: { "content-type": "application/json" },
                  body: JSON.stringify({ filled: msg.filled || [], missing: msg.missing || [], note: msg.note || "" }) });
        sendResponse({ ok: true });
      } else if (msg.type === "submitted") {
        await api(`/me/applications/${msg.id}/mark-submitted`, { method: "POST" });
        sendResponse({ ok: true });
      } else if (msg.type === "fill_active_tab") {
        // the popup asked to fill the active tab with plan `id`: relay to every frame of that tab
        const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
        if (!tab) throw new Error("no active tab");
        const plan = await (await api(`/me/applications/${msg.id}`)).json();
        let resume = null;
        try { resume = await getResume(); } catch (e) { resume = null; }
        // every frame of the tab gets the plan (a company page embeds the Greenhouse form in a frame);
        // the frame that holds the form is the one that fills something
        const frames = (await chrome.webNavigation.getAllFrames({ tabId: tab.id })) || [];
        const agg = { filled: [], missing: [], frames: 0 };
        for (const f of frames) {
          try {
            const r = await chrome.tabs.sendMessage(tab.id, { type: "fill", application: plan, resume }, { frameId: f.frameId });
            if (r && r.filled) { agg.frames++; if (r.filled.length) { agg.filled = agg.filled.concat(r.filled); agg.missing = r.missing || []; } }
          } catch (e) { /* frame without our content script (other host) */ }
        }
        sendResponse({ ok: true, results: agg });
      } else {
        sendResponse({ ok: false, error: "unknown message" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String(e && e.message || e) });
    }
  })();
  return true;   // async sendResponse
});
