// Pure helpers for the Roster Apply CLI — no browser, no network, unit-tested (test_plan.mjs).
// The server already RESOLVED every answer (bind_plan): each plan question carries {answer, source,
// policy, needs_confirmation, kind, options, selector}. The CLI's job is to put those answers into the
// live form; these helpers decide HOW to treat each field and keep it honest about what it may touch.

export const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();

// The option (verbatim, as the page renders it) that means `want` — for clicking a radio/checkbox/listbox
// option by its text. Exact match first, then a decline phrase, then a contains either way.
export function pickOption(options, want) {
  const w = norm(want);
  if (!w || !options || !options.length) return "";
  for (const o of options) if (norm(o) === w) return o;
  if (/prefer|decline|not to|rather not/.test(w))
    for (const o of options) if (/prefer not|decline|rather not|do not wish|not to answer|not to say/i.test(o)) return o;
  for (const o of options) if (norm(o).includes(w)) return o;
  for (const o of options) { const on = norm(o); if (on.length >= 4 && w.includes(on)) return o; }
  return "";
}

// What the fill loop does with a question. `skip` — never touch (CAPTCHA/password/résumé-text twin,
// or an eligibility fact the profile didn't hold: the human owns it). `file` — the résumé upload.
// `combo` — an autocomplete that must be typed then PICKED (react-select, Greenhouse #candidate-location).
// `choice` — a native/option control set by value. `text` — a plain field. `none` — nothing to do.
export function classifyField(q) {
  if (!q) return "none";
  const pol = q.policy || "open";
  if (pol === "never" || pol === "skip") return "skip";
  if (q.needs_confirmation) return "skip";               // an unsourced eligibility fact — never guessed by the runner
  if (q.kind === "file") return "file";
  if (!q.answer) return "none";                          // nothing resolved to fill
  const combo = q.combo || /(^|\W)location(\W|$)/i.test(q.label || "") ||
                (q.selector || "").includes("candidate-location");
  if (combo && (q.kind === "text" || q.kind === "select")) return "combo";
  if (["select", "multiselect", "radio", "checkbox", "boolean"].includes(q.kind)) return "choice";
  return "text";
}

// The comma-separated selector list the planner derived, as an array to try in order across frames.
export function selectorsOf(q) {
  return String((q && q.selector) || "").split(",").map(s => s.trim()).filter(Boolean);
}

// Fields the person must handle before submitting (mirrors the extension's review surface).
export function reviewFields(plan) {
  const needs = [], drafts = [];
  for (const q of plan || []) {
    if (q.needs_confirmation) needs.push(q.label);
    else if (q.source === "agent draft") drafts.push(q.label);
  }
  return { needs, drafts, all: needs.concat(drafts) };
}

// Never one-tap submit while a required field is unanswered or an eligibility fact is unconfirmed.
export function submitBlockedReason(plan) {
  const req = (plan || []).filter(q => q.required && !q.answer && (q.policy || "open") !== "skip" && q.policy !== "never");
  if (req.length) return `${req.length} required field(s) still empty: ${req.slice(0, 4).map(q => q.label).join(", ")}`;
  const needs = (plan || []).filter(q => q.needs_confirmation);
  if (needs.length) return `${needs.length} eligibility field(s) need you: ${needs.slice(0, 4).map(q => q.label).join(", ")}`;
  return "";
}
