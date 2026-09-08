// Roster Apply — content script (runs in every frame of Greenhouse / Lever / Ashby pages).
// Executes a reviewed PLAN: sets each field by the selector the planner derived from the ATS's own
// form definition, attaches the résumé, highlights what it set, reports field by field. It contains no
// submit call and never clicks a submit button — the user submits.
(() => {
  if (window.__rosterApplyLoaded) return;
  window.__rosterApplyLoaded = true;

  // Set a text-like value the way TYPING would: focus, select what is there, insert through the browser's
  // editing pipeline (execCommand insertText → real beforeinput/input events every form library accepts), and
  // only then fall back to the prototype setter + a synthetic InputEvent. Ends with change → blur → focusout so
  // "touched" + validation state update too (owner, 2026-09-05: fields looked filled but submit complained
  // until they were touched — the form's own state had never registered the value).
  const setNative = (el, v) => {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : (el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype);
    const d = Object.getOwnPropertyDescriptor(proto, "value");
    const set = () => { if (d && d.set) d.set.call(el, v); else el.value = v; };
    try { el.focus(); el.dispatchEvent(new FocusEvent("focus", { bubbles: false })); el.dispatchEvent(new FocusEvent("focusin", { bubbles: true })); } catch (e) {}
    let typed = false;
    if (el.tagName !== "SELECT") {
      try {
        if (typeof el.select === "function") el.select();
        else if (typeof el.setSelectionRange === "function") el.setSelectionRange(0, (el.value || "").length);
        typed = document.execCommand("insertText", false, v) && el.value === v;
      } catch (e) { typed = false; }
    }
    if (!typed) {
      set();
      try { el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: v })); }
      catch (e) { el.dispatchEvent(new Event("input", { bubbles: true })); }
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
    try { el.blur(); } catch (e) {}
    el.dispatchEvent(new FocusEvent("blur", { bubbles: false }));
    el.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
  };
  const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
  // Three outcomes, three colours — because "we tried" and "the form took it" are different facts and
  // a reader deciding whether to retype a field needs to see which one they are looking at.
  const COLOUR = { verified: "#6c5ce7", entered: "#e1a100", missing: "#d63031" };
  const mark = (el, state) => {
    const c = COLOUR[state === true ? "verified" : (state === false ? "missing" : state)] || COLOUR.missing;
    try { el.style.outline = "2px solid " + c; el.style.outlineOffset = "1px";
          if (c === COLOUR.entered) el.title = "Roster entered this but the form did not confirm it — check it"; } catch (e) {}
  };
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

  // ── VERIFICATION ────────────────────────────────────────────────────────────────────────────────
  // Setting a value and seeing it in the box is NOT proof the form accepted it. Measured against the
  // three widget shapes an ATS actually ships:
  //   • a masked input (phone, date)                  — value sticks, state agrees
  //   • a picker that ignores typing                  — value does not even stick; a read-back catches it
  //   • a combobox whose committed value lives in a
  //     HIDDEN input (the react-select shape)         — the visible box reads back CORRECTLY while the
  //                                                     form still holds nothing. This is the case the
  //                                                     owner hits: "filled right, submit says empty."
  // So a read-back of the visible element is necessary and NOT sufficient. Where a question has a
  // hidden committed twin, that twin is the truth; where we cannot find one, we say so rather than
  // claim a green tick.
  const OK = "verified", SOFT = "entered", NONE = "missing";

  // the hidden input that actually carries a widget's value: same name/id, or the one hidden input in
  // the question's own box. Returns undefined when there is no such twin (then we cannot be sure).
  function committedTwin(el, box) {
    const nm = el.getAttribute("name") || el.id || "";
    const base = nm.replace(/[-_ ]?(search|input|query|text)$/i, "");
    const pool = [];
    try {
      if (base) pool.push(...document.querySelectorAll(`input[type=hidden][name="${CSS.escape(base)}"], input[type=hidden]#${CSS.escape(base)}`));
    } catch (e) {}
    if (!pool.length && box) pool.push(...box.querySelectorAll("input[type=hidden]"));
    return pool.length === 1 ? pool[0] : undefined;
  }

  // Did this question end up actually set? Returns OK / SOFT / NONE.
  //   OK   — something we can read confirms the value (the element itself, or its committed twin)
  //   SOFT — we entered it and cannot confirm the form took it (the user must check this one)
  //   NONE — nothing is set
  function verifyValue(el, want, box) {
    const twin = committedTwin(el, box);
    const seen = (el.value || "").trim();
    const w = norm(want);
    if (twin !== undefined) {
      const tv = norm(twin.value || "");
      if (tv && (tv === w || tv.includes(w) || w.includes(tv))) return OK;
      return seen ? SOFT : NONE;          // the box shows it; the form holds nothing → SOFT, never OK
    }
    if (!seen) return NONE;
    const s = norm(seen);
    return (s === w || s.includes(w) || w.includes(s)) ? OK : SOFT;
  }

  function verifyChecked(ctl) { return ctl && ctl.checked ? OK : SOFT; }

  function findAll(q) {
    const sels = (q.selector || "").split(",").map(s => s.trim()).filter(Boolean);
    let els = [];
    for (const s of sels) { try { els = els.concat([...document.querySelectorAll(s)]); } catch (e) {} }
    if (!els.length && q.id) {
      try { els = [...document.querySelectorAll(`[name="${CSS.escape(q.id)}"], #${CSS.escape(q.id)}, [name*="${CSS.escape(q.id)}"]`)]; } catch (e) {}
    }
    return els;
  }

  const WIDGET = "input, textarea, select, button, [role='radio'], [role='checkbox'], [role='combobox']";
  const nInputs = box => box.querySelectorAll("input:not([type=hidden]), textarea, select").length;

  // the container that holds ONE question's widget on React forms (Ashby / Greenhouse job-boards): the
  // nearest ancestor of the question's label that holds a widget — and never a whole section (a box
  // holding many fields would make us write this question's value into somebody else's input)
  function containerByLabel(label) {
    const want = norm(label).slice(0, 60);
    if (!want) return null;
    const cands = [...document.querySelectorAll("label, legend, [class*='label'], [class*='Label'], h3, h4, p, div, span")]
      .filter(el => el.children.length < 6 && norm(el.textContent).startsWith(want));
    for (const el of cands) {
      let n = el;
      for (let i = 0; i < 6 && n; i++) {
        if (n.querySelector && n.querySelector(WIDGET)) return nInputs(n) <= 12 ? n : null;
        n = n.parentElement;
      }
    }
    return null;
  }

  // Ashby stamps data-field-path=<field path> on every field's container (application form, diversity
  // survey, EEOC block alike) — exact, so it comes first; other ATSs fall back to the label walk
  function fieldBox(q) {
    if (q.id) { try { const b = document.querySelector(`[data-field-path="${CSS.escape(q.id)}"]`); if (b) return b; } catch (e) {} }
    return containerByLabel(q.label);
  }

  function clickOption(scope, text) {
    const want = norm(text);
    if (!want) return false;
    const pool = [...scope.querySelectorAll("label, button, [role='radio'], [role='checkbox'], [role='option'], li, span, div")]
      .filter(el => visible(el) && el.children.length < 4);
    let best = pool.find(el => norm(el.textContent) === want) || pool.find(el => norm(el.textContent).startsWith(want)) || pool.find(el => norm(el.textContent).includes(want) && norm(el.textContent).length < want.length + 40);
    if (!best) return false;
    // the real control: an input inside the match, the label's own control, or the input next to it
    // (a BUTTON is the control itself — Ashby's Yes / No pair sits next to one hidden checkbox whose
    // toggle means "Yes", so never reach past a button for a sibling input)
    let ctl = best.querySelector && best.querySelector("input[type=radio], input[type=checkbox]");
    if (!ctl && best.tagName === "LABEL" && best.control) ctl = best.control;
    if (!ctl && best.tagName === "LABEL" && best.parentElement) ctl = best.parentElement.querySelector("input[type=radio], input[type=checkbox]");
    if (ctl && ctl.checked) { mark(best, OK); return true; }   // already set — never toggle it off
    const target = ctl || best;
    target.click();
    // A real input's .click() makes the browser dispatch input + change itself. A DIV or SPAN
    // pretending to be a radio (every React ATS widget) dispatches ONLY click, so a site listening
    // for input/change never hears us — the option looks chosen and the form holds nothing.
    if (!ctl) {
      try {
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
      } catch (e) {}
    }
    mark(best, ctl ? OK : SOFT);
    return true;
  }

  async function setFile(el, resume) {
    if (!resume || !resume.b64) return false;
    try {
      const bin = atob(resume.b64); const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      const file = new File([bytes], resume.name || "resume.pdf", { type: resume.type || "application/pdf" });
      const dt = new DataTransfer(); dt.items.add(file);
      el.files = dt.files;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      el.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    } catch (e) { return false; }
  }

  async function fillOne(q, resume) {
    const kind = q.kind, val = q.answer;
    if (q.policy === "never" || q.policy === "skip") return null;
    // No answer planned. A REQUIRED question with no answer is the single most expensive thing to
    // discover at the submit button, so it is reported rather than silently skipped.
    if (!val) return q.required ? NONE : null;
    const els = findAll(q).filter(el => el.type !== "hidden");
    // FILE
    if (kind === "file") {
      const fi = els.find(el => el.type === "file") || [...document.querySelectorAll("input[type=file]")].find(el => /resume|cv/i.test((el.name || "") + (el.id || "") + (el.getAttribute("data-qa") || "")));
      if (!fi) return NONE;
      const ok = await setFile(fi, resume);
      // a file input reports its own truth: files.length is what the form will send
      const st = ok && fi.files && fi.files.length ? OK : (ok ? SOFT : NONE);
      mark(fi.closest("label, div") || fi, st); return st;
    }
    // TEXT-LIKE
    if (["text", "textarea", "email", "tel", "url", "date"].includes(kind)) {
      let el = els.find(el => ["INPUT", "TEXTAREA"].includes(el.tagName) && visible(el)) || els[0];
      if (!el) {   // no stable name on the input (Ashby's Location widget): the one text input in this question's own box
        const box = fieldBox(q);
        const ins = box ? [...box.querySelectorAll("input, textarea")].filter(x => visible(x) && !["hidden", "file", "checkbox", "radio", "submit", "button"].includes(x.type)) : [];
        el = ins.length === 1 ? ins[0] : null;
      }
      if (!el) return NONE;
      const box0 = fieldBox(q);
      if (el.getAttribute("role") === "combobox" || (el.getAttribute("aria-autocomplete") || "") !== "") {
        // A COMBOBOX IS NOT SET BY TYPING INTO IT. The visible box will read back the text we typed
        // while the form still holds nothing, which is precisely the "filled right, submit says
        // empty" report. Typing only opens the list; the OPTION CLICK is what commits.
        setNative(el, val); await new Promise(r => setTimeout(r, 500));
        const scope = box0 || document;
        if (!clickOption(scope, val)) {
          const opt = scope.querySelector("[role='option']") || document.querySelector("[role='option']");
          if (opt) opt.click(); else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        }
        await new Promise(r => setTimeout(r, 250));
      } else setNative(el, val);
      const st = verifyValue(el, val, box0);
      mark(el, st); return st;
    }
    // NATIVE SELECT
    const sel = els.find(el => el.tagName === "SELECT");
    if (sel) {
      const w = norm(val);
      const idx = [...sel.options].findIndex(o => norm(o.text) === w) ?? -1;
      const i2 = idx >= 0 ? idx : [...sel.options].findIndex(o => norm(o.text).includes(w) || w.includes(norm(o.text)) && norm(o.text).length > 2);
      if (i2 >= 0) { try { sel.focus(); } catch (e) {} sel.selectedIndex = i2; sel.dispatchEvent(new Event("input", { bubbles: true })); sel.dispatchEvent(new Event("change", { bubbles: true })); try { sel.blur(); } catch (e) {} sel.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
        // a native select reports its own truth: the option it now holds
        const st = norm(sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : "") ? OK : SOFT;
        mark(sel, st); return st; }
    }
    // the question's own box first (exact on Ashby): a custom dropdown → open it, pick; a Boolean →
    // Yes / No buttons; radio / checkbox groups → the option by its label; a multi-select → every value
    const box = fieldBox(q);
    if (box) {
      const combo = box.querySelector("[role='combobox'], input[aria-autocomplete]");
      if (combo && kind !== "boolean") {
        combo.focus(); setNative(combo, val); await new Promise(r => setTimeout(r, 500));
        const clicked = clickOption(box, val) || clickOption(document, val);
        if (!clicked) combo.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
        await new Promise(r => setTimeout(r, 250));
        const st = verifyValue(combo, val, box);
        mark(combo, st); return st;
      }
      const vals = kind === "multiselect" ? String(val).split(/\s*[;|\n]\s*/).filter(Boolean) : [val];
      let ok = false;
      for (const v of vals) ok = clickOption(box, v) || ok;
      // an option click we could not confirm through a checked control is ENTERED, not verified
      if (ok) { const ctl = box.querySelector("input[type=radio]:checked, input[type=checkbox]:checked");
                return ctl ? OK : SOFT; }
    }
    // then RADIO / CHECKBOX groups that share the question's name
    const grp = els.filter(el => el.type === "radio" || el.type === "checkbox");
    if (grp.length) {
      const w = norm(val);
      const hit = grp.find(el => { const l = el.labels && el.labels[0] ? el.labels[0].textContent : (el.closest("label") || {}).textContent || el.value; return norm(l) === w || norm(l).startsWith(w) || w.startsWith(norm(l)); });
      if (hit) { if (!hit.checked) hit.click(); const st = verifyChecked(hit);
                 mark(hit.closest("label") || hit, st); return st; }
    }
    return NONE;
  }

  async function run(application, resume) {
    const plan = application.plan || [];
    const filled = [], unconfirmed = [], missing = [];
    for (const q of plan) {
      try {
        const r = await fillOne(q, resume);
        if (r === OK) filled.push(q.label);
        else if (r === SOFT) unconfirmed.push(q.label);
        else if (r === NONE) missing.push(q.label);
      } catch (e) { missing.push(q.label); }
    }
    // `missing` stays the field the caller already understands: everything the reader must handle.
    return { filled, unconfirmed, missing, needs_you: unconfirmed.concat(missing),
             frame: location.href.slice(0, 120) };
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type !== "fill") return;
    (async () => {
      const res = await run(msg.application || {}, msg.resume);
      // report from the frame that actually held the form (the one that filled something)
      // Report from the frame that actually held the form. A run that filled NOTHING used to report
      // nothing at all, so a total miss looked identical to never having pressed the button.
      if (res.filled.length || res.unconfirmed.length || res.missing.length) {
        chrome.runtime.sendMessage({ type: "executed", id: msg.application.id, filled: res.filled,
                                     unconfirmed: res.unconfirmed, missing: res.missing, note: res.frame });
        // NAME the fields the reader has to handle. "3 not found" sends someone hunting a 40-field
        // form; the amber outlines plus these names send them straight to the three that need them.
        const need = res.needs_you;
        const banner = document.createElement("div");
        banner.innerHTML = `<b>Roster filled ${res.filled.length} field${res.filled.length === 1 ? "" : "s"}.</b>`
          + (need.length ? `<br><span style="color:#ffd479">${need.length} need${need.length === 1 ? "s" : ""} you (outlined amber/red): ${need.slice(0, 6).map(x => String(x).slice(0, 40)).join(" · ")}${need.length > 6 ? " …" : ""}</span>` : "")
          + `<br><span style="opacity:.75">Review the page, then submit it yourself.</span>`;
        banner.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:2147483647;background:#111;color:#fff;padding:10px 16px;border-radius:10px;font:14px/1.5 system-ui;max-width:min(90vw,560px);box-shadow:0 6px 24px rgba(0,0,0,.3);";
        document.body.appendChild(banner); setTimeout(() => banner.remove(), 15000);
        // observe the submission: a thank-you / confirmation appearing later → record it (never claimed otherwise)
        const obs = new MutationObserver(() => {
          const t = document.body.innerText || "";
          if (/thank you for applying|application (has been )?(submitted|received)|we('ve| have) received your application/i.test(t)) {
            obs.disconnect(); chrome.runtime.sendMessage({ type: "submitted", id: msg.application.id });
          }
        });
        obs.observe(document.body, { childList: true, subtree: true, characterData: true });
      }
      sendResponse({ ok: true, ...res });
    })();
    return true;
  });
})();
