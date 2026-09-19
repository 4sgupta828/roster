// The browser drive, shared by the CLI and the GUI: connect to a launched Chrome over CDP, open a tab,
// inject the proven fill logic, optionally submit. Returns a structured result and streams progress to a
// `log` callback.
import { launchChrome } from "./chrome.mjs";
import { CDP } from "./cdp.mjs";
import { FILL_SRC } from "./fill-inject.mjs";
import { reviewFields, submitBlockedReason } from "./plan.mjs";
import { api } from "./api.mjs";

// The applications a fresh run should offer: prepared and not yet filled/submitted. Excludes old "filled"
// ones (already done) and "needs_you" (unsupported ATS / CAPTCHA — can't be auto-filled) so the app shows
// a clean to-do queue, not the whole history.
export const readyToFill = (apps) => (apps || []).filter(a => a.status === "planned");

export const SUBMIT_SRC = `(function(){var b=[].slice.call(document.querySelectorAll('button,input[type=submit],[role=button]')).find(function(el){var t=((el.textContent||'')+' '+(el.value||'')).toLowerCase().trim();return !el.disabled&&/(submit|apply|send application|finish|complete application)/.test(t)&&!/(save|cancel|back|previous|draft|sign in|log in|search)/.test(t);});if(b){try{b.scrollIntoView({block:'center'});}catch(e){}b.click();return true;}return false;})()`;

// Launch the user's Chrome and connect a CDP client. The Chrome window is left open (detached) for review.
export async function connectBrowser() {
  const { wsUrl } = await launchChrome();
  return await CDP.connect(wsUrl);
}

export async function fillApp(cdp, app, resume, opts = {}, log = () => {}, c = null) {
  const url = app.form_url || app.url;
  log(`[#${app.id}] ${app.title || "role"} @ ${app.company || ""}`);
  const sid = await cdp.openTab(url);
  const expr = `(${FILL_SRC})(${JSON.stringify(app.plan || [])}, ${JSON.stringify(resume)})`;
  const res = await cdp.evaluate(sid, expr).catch(e => ({ filled: [], needs: [], missing: ["(page error: " + e.message + ")"] }));
  const rv = reviewFields(app.plan || []);
  const blocked = submitBlockedReason(app.plan || []);
  log(`filled ${res.filled.length}` + (res.needs.length ? `, ${res.needs.length} need you` : "") + (res.missing.length ? `, ${res.missing.length} not set` : ""));
  let submitted = false;
  if (opts.submit && blocked) log(`not submitting — ${blocked}`);
  else if (opts.submit) { submitted = await cdp.evaluate(sid, SUBMIT_SRC).catch(() => false); log(submitted ? "submitted (confirm on the page)" : "couldn't find submit — submit on the page"); }
  // Tell Roster this one was filled so it drops off the "ready to fill" queue (same as the extension does).
  if (c) { try { await api(`/me/applications/${app.id}/executed`, c, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ filled: res.filled, unconfirmed: res.needs || [], missing: res.missing, note: "desktop runner" }) }); } catch (e) { /* best-effort */ } }
  return { id: app.id, filled: res.filled.length, needs: res.needs.length, missing: res.missing.length, review: rv.all, blocked, submitted };
}
