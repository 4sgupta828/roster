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

// NOTIFICATIONS: every 15 minutes ask Roster for unread notifications (a map refresh found new roles or
// people, an application moved) and raise them as native Chrome notifications. Nothing is sent anywhere;
// the poll is a plain read with the user's own token, and it stops when there is no token.
const SEEN_KEY = "roster_notified_ids";
async function pollNotifications() {
  try {
    const { token, base } = await cfg();
    if (!token) return;
    const r = await fetch(base + "/me/notifications?unread=1", { headers: { "X-Roster-Token": token } });
    if (!r.ok) return;
    const d = await r.json();
    const s = await chrome.storage.local.get([SEEN_KEY]);
    const seen = new Set(s[SEEN_KEY] || []);
    const fresh = (d.notifications || []).filter(n => !seen.has(n.id)).slice(0, 5);
    for (const n of fresh) {
      chrome.notifications.create("roster-" + n.id, { type: "basic", iconUrl: "icons/128.png", title: n.title || "Roster",
        message: (n.body || "").slice(0, 200), priority: 1 });
      seen.add(n.id);
    }
    await chrome.storage.local.set({ [SEEN_KEY]: [...seen].slice(-500) });
  } catch (e) { /* offline or token revoked: try again next tick */ }
}
chrome.alarms.create("roster-notify", { periodInMinutes: 15 });
chrome.alarms.onAlarm.addListener(a => { if (a.name === "roster-notify") pollNotifications(); });
chrome.notifications.onClicked.addListener(async id => {
  const { base } = await cfg();
  chrome.tabs.create({ url: base + "/" });
  chrome.notifications.clear(id);
});
chrome.runtime.onInstalled.addListener(() => pollNotifications());

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
        // keep the last run's field-by-field record so the popup can hand it back verbatim
        try { await chrome.storage.local.set({ roster_last_diag: {
                at: new Date().toISOString(), url: msg.note || "", id: msg.id,
                filled: msg.filled || [], unconfirmed: msg.unconfirmed || [], missing: msg.missing || [],
                fields: msg.diag || [] } }); } catch (e) {}
        await api(`/me/applications/${msg.id}/executed`, { method: "POST", headers: { "content-type": "application/json" },
                  body: JSON.stringify({ filled: msg.filled || [], unconfirmed: msg.unconfirmed || [],
                                         missing: msg.missing || [], note: msg.note || "" }) });
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
        // MERGE across frames. This used to ASSIGN `missing` and only when that frame had filled
        // something, so a frame that missed everything reported nothing and a later frame erased an
        // earlier frame's misses — the fields a user most needed to hear about were the ones dropped.
        const agg = { filled: [], unconfirmed: [], missing: [], frames: 0 };
        const add = (into, from) => { for (const x of (from || [])) if (!into.includes(x)) into.push(x); };
        for (const f of frames) {
          try {
            const r = await chrome.tabs.sendMessage(tab.id, { type: "fill", application: plan, resume }, { frameId: f.frameId });
            if (r && r.filled) {
              agg.frames++;
              add(agg.filled, r.filled); add(agg.unconfirmed, r.unconfirmed); add(agg.missing, r.missing);
            }
          } catch (e) { /* frame without our content script (other host) */ }
        }
        // a field filled in one frame is not missing because another frame lacked it
        agg.missing = agg.missing.filter(x => !agg.filled.includes(x) && !agg.unconfirmed.includes(x));
        agg.unconfirmed = agg.unconfirmed.filter(x => !agg.filled.includes(x));
        agg.needs_you = agg.unconfirmed.concat(agg.missing);
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
