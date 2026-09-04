// Roster Apply — content script (runs in every frame of Greenhouse / Lever / Ashby pages).
// Executes a reviewed PLAN: sets each field by the selector the planner derived from the ATS's own
// form definition, attaches the résumé, highlights what it set, reports field by field. It contains no
// submit call and never clicks a submit button — the user submits.
(() => {
  if (window.__rosterApplyLoaded) return;
  window.__rosterApplyLoaded = true;

  const setNative = (el, v) => {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : (el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype);
    const d = Object.getOwnPropertyDescriptor(proto, "value");
    if (d && d.set) d.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  };
  const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
  const mark = (el, ok) => { try { el.style.outline = ok ? "2px solid #6c5ce7" : "2px solid #d63031"; el.style.outlineOffset = "1px"; } catch (e) {} };
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

  function findAll(q) {
    const sels = (q.selector || "").split(",").map(s => s.trim()).filter(Boolean);
    let els = [];
    for (const s of sels) { try { els = els.concat([...document.querySelectorAll(s)]); } catch (e) {} }
    if (!els.length && q.id) {
      try { els = [...document.querySelectorAll(`[name="${CSS.escape(q.id)}"], #${CSS.escape(q.id)}, [name*="${CSS.escape(q.id)}"]`)]; } catch (e) {}
    }
    return els;
  }

  // the container that holds a question's widget on React forms (Ashby / Greenhouse job-boards): the
  // nearest ancestor whose text starts with the question label
  function containerByLabel(label) {
    const want = norm(label).slice(0, 60);
    if (!want) return null;
    const cands = [...document.querySelectorAll("label, legend, [class*='label'], [class*='Label'], h3, h4, p, div, span")]
      .filter(el => el.children.length < 6 && norm(el.textContent).startsWith(want));
    for (const el of cands) {
      let n = el;
      for (let i = 0; i < 6 && n; i++) {
        if (n.querySelector && n.querySelector("input, textarea, select, button, [role='radio'], [role='checkbox'], [role='combobox']") && n !== el) return n;
        n = n.parentElement;
      }
    }
    return null;
  }

  function clickOption(scope, text) {
    const want = norm(text);
    const pool = [...scope.querySelectorAll("label, button, [role='radio'], [role='checkbox'], [role='option'], li, span, div")]
      .filter(el => visible(el) && el.children.length < 4);
    let best = pool.find(el => norm(el.textContent) === want) || pool.find(el => norm(el.textContent).startsWith(want)) || pool.find(el => norm(el.textContent).includes(want) && norm(el.textContent).length < want.length + 40);
    if (!best) return false;
    const inp = best.querySelector && best.querySelector("input[type=radio], input[type=checkbox]");
    (inp || best).click();
    mark(best, true);
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
    if (!val || q.policy === "never" || q.policy === "skip") return null;
    const els = findAll(q).filter(el => el.type !== "hidden");
    // FILE
    if (kind === "file") {
      const fi = els.find(el => el.type === "file") || [...document.querySelectorAll("input[type=file]")].find(el => /resume|cv/i.test((el.name || "") + (el.id || "") + (el.getAttribute("data-qa") || "")));
      if (!fi) return false;
      const ok = await setFile(fi, resume); if (ok) mark(fi.closest("label, div") || fi, true); return ok;
    }
    // TEXT-LIKE
    if (["text", "textarea", "email", "tel", "url", "date"].includes(kind)) {
      const el = els.find(el => ["INPUT", "TEXTAREA"].includes(el.tagName) && visible(el)) || els[0];
      if (!el) return false;
      if (el.getAttribute("role") === "combobox" || (el.getAttribute("aria-autocomplete") || "") !== "") {
        setNative(el, val); await new Promise(r => setTimeout(r, 500));
        const opt = document.querySelector("[role='option']"); if (opt) opt.click(); else { el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); }
      } else setNative(el, val);
      mark(el, true); return true;
    }
    // NATIVE SELECT
    const sel = els.find(el => el.tagName === "SELECT");
    if (sel) {
      const w = norm(val);
      const idx = [...sel.options].findIndex(o => norm(o.text) === w) ?? -1;
      const i2 = idx >= 0 ? idx : [...sel.options].findIndex(o => norm(o.text).includes(w) || w.includes(norm(o.text)) && norm(o.text).length > 2);
      if (i2 >= 0) { sel.selectedIndex = i2; sel.dispatchEvent(new Event("change", { bubbles: true })); mark(sel, true); return true; }
    }
    // RADIO / CHECKBOX groups by name, then by the question's container
    const grp = els.filter(el => el.type === "radio" || el.type === "checkbox");
    if (grp.length) {
      const w = norm(val);
      const hit = grp.find(el => { const l = el.labels && el.labels[0] ? el.labels[0].textContent : (el.closest("label") || {}).textContent || el.value; return norm(l) === w || norm(l).startsWith(w) || w.startsWith(norm(l)); });
      if (hit) { hit.click(); mark(hit.closest("label") || hit, true); return true; }
    }
    const box = containerByLabel(q.label);
    if (box) {
      // Ashby-style: a custom dropdown → open it, pick; a Boolean → Yes / No buttons
      const combo = box.querySelector("[role='combobox'], input[aria-autocomplete]");
      if (combo && kind !== "boolean") {
        combo.focus(); setNative(combo, val); await new Promise(r => setTimeout(r, 500));
        if (clickOption(document, val)) return true;
        combo.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); return true;
      }
      if (clickOption(box, val)) return true;
    }
    return false;
  }

  async function run(application, resume) {
    const plan = application.plan || [];
    const filled = [], missing = [];
    for (const q of plan) {
      try {
        const r = await fillOne(q, resume);
        if (r === true) filled.push(q.label); else if (r === false) missing.push(q.label);
      } catch (e) { missing.push(q.label); }
    }
    return { filled, missing, frame: location.href.slice(0, 120) };
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type !== "fill") return;
    (async () => {
      const res = await run(msg.application || {}, msg.resume);
      // report from the frame that actually held the form (the one that filled something)
      if (res.filled.length) {
        chrome.runtime.sendMessage({ type: "executed", id: msg.application.id, filled: res.filled, missing: res.missing, note: res.frame });
        const banner = document.createElement("div");
        banner.textContent = `Roster filled ${res.filled.length} field${res.filled.length === 1 ? "" : "s"}${res.missing.length ? `; ${res.missing.length} not found — check them` : ""}. Review everything, then submit yourself.`;
        banner.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:2147483647;background:#111;color:#fff;padding:10px 16px;border-radius:10px;font:14px system-ui;box-shadow:0 6px 24px rgba(0,0,0,.3);";
        document.body.appendChild(banner); setTimeout(() => banner.remove(), 9000);
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
