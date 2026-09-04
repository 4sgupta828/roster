"""AUTO-APPLY, CONTROLLED (owner ask 2026-09-04): open a posting's apply page in a headless browser,
fill what the user's Apply profile + résumé answer, screenshot the filled form, and STOP. Submission
happens only after the user approves THAT application in Roster. The agent never bypasses a CAPTCHA,
never creates an ATS account or types a password (Workday-style portals → `needs_you` with the
prefilled answers ready to paste), and never invents an answer to a question the profile can't answer
(those are listed for the user; a model may DRAFT free-text answers from the profile, labeled as drafts).

Runs as a SEPARATE PROCESS (Chromium never lives in the web process):
    python -m api.auto_apply fill|submit <in.json> <out.json>
in.json  = {url, profile, answers, resume: {name, b64}}   out.json = the result dict below.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile

# ---- pure helpers (unit-tested; no browser) ---------------------------------------------------------
_FIELD_RX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)^(preferred\s+)?(first|given)\s*name"), "first_name"),
    (re.compile(r"(?i)^(preferred\s+)?(last|family|sur)\s*name"), "last_name"),
    (re.compile(r"(?i)^(most recent|current|present|last)\s+(company|employer|organization)"), "current_company"),
    (re.compile(r"(?i)^(most recent|current|present|last)\s+(title|role|job title|position)"), "current_title"),
    (re.compile(r"(?i)^(full\s*)?name$|^your name|^name\s*\*?$"), "full_name"),
    (re.compile(r"(?i)e-?mail"), "email"),
    (re.compile(r"(?i)phone|mobile|telephone"), "phone"),
    (re.compile(r"(?i)linkedin"), "linkedin"),
    (re.compile(r"(?i)github"), "github"),
    (re.compile(r"(?i)portfolio|website|personal site|url$"), "portfolio_website"),
    (re.compile(r"(?i)^(current |present )?(company|employer|organization|org)\b"), "current_company"),
    (re.compile(r"(?i)^(current |present )?(title|role|job title|position)\b"), "current_title"),
    (re.compile(r"(?i)^(location|city|current location|where are you)"), "city"),
    (re.compile(r"(?i)years? of experience"), "years_experience"),
    (re.compile(r"(?i)cover letter"), "cover_letter"),
    (re.compile(r"(?i)resume|résumé|cv\b"), "resume"),
]
# voluntary self-identification (EEO): never drafted, never required by Roster, shown folded as optional
_VOLUNTARY_RX = re.compile(r"(?i)racial|race|ethnic|gender|pronoun|veteran|disabilit|self-identif|sexual orientation|select all that apply")
_LOGIN_RX = re.compile(r"(?i)(sign in|log in|create account|password)")
_WORKDAY_RX = re.compile(r"(?i)myworkdayjobs\.com|workday")
_CAPTCHA_RX = re.compile(r"(?i)recaptcha|hcaptcha|captcha")


def detect_ats(url: str) -> str:
    u = (url or "").lower()
    if "greenhouse.io" in u or "gh_jid=" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if _WORKDAY_RX.search(u):
        return "workday"
    return "generic"


def map_label(label: str) -> str:
    """A form field's label / name / placeholder → the Apply-profile key it answers ('' = a custom question)."""
    t = re.sub(r"\s+", " ", (label or "")).strip().rstrip("*").strip()
    if not t:
        return ""
    for rx, key in _FIELD_RX:
        if rx.search(t):
            return key
    return ""


def value_for(key: str, profile: dict, answers: dict | None = None) -> str:
    """The value the profile (or the user's saved answers) gives a standard field; '' when unknown."""
    answers = answers or {}
    if key in answers and str(answers[key]).strip():
        return str(answers[key]).strip()
    if key == "full_name":
        return (" ".join(str(profile.get(k) or "") for k in ("first_name", "last_name"))).strip()
    if key == "city":
        return ", ".join(str(profile.get(k) or "") for k in ("city", "region") if profile.get(k))
    v = profile.get(key)
    return str(v).strip() if v not in (None, "") else ""


def group_fields(fields: list[dict]) -> list[dict]:
    """Radio / checkbox inputs that share a name are ONE question (its group label) with options; a
    placeholder-only label ('Start typing...') is not a label — the group label is used instead."""
    out, groups = [], {}
    for f in fields:
        kind = f.get("kind") or "text"
        if kind in ("radio", "checkbox") and (f.get("group_key") or f.get("name") or f.get("group_label")):
            key = f.get("group_key") or f.get("name") or f.get("group_label")
            g = groups.get(key)
            if g is None:
                g = {"label": (f.get("group_label") or f.get("label") or f.get("name") or "").strip(), "kind": kind, "name": f.get("name") or "",
                     "required": bool(f.get("required")), "options": [], "option_selectors": {}, "selector": ""}
                groups[key] = g
                out.append(g)
            opt = (f.get("label") or "").strip()
            if opt and opt not in g["options"]:
                g["options"].append(opt)
                g["option_selectors"][opt] = f.get("selector") or ""
            g["required"] = g["required"] or bool(f.get("required"))
            continue
        lab = (f.get("label") or "").strip()
        if (not lab or lab.endswith("...") or lab.lower().startswith("type here") or lab.lower().startswith("start typing")) and f.get("group_label"):
            f = {**f, "label": f["group_label"]}
        out.append(f)
    for g in groups.values():
        g["label"] = re.sub(r"\s*\*\s*$", "", g["label"] or "")
        # the walk reached page chrome (tab titles) instead of a question: name the group by its options
        if not g["label"] or len(g["label"]) < 4 or re.fullmatch(r"(?i)(overview|application|apply|overview application|job description)\s*", g["label"]):
            g["label"] = " / ".join(g["options"][:2]) + (" …" if len(g["options"]) > 2 else "")
    return out


# STANDARD QUESTIONS every ATS asks, answered UPFRONT from the Apply profile (owner, 2026-09-04: source
# every field in the account; the user mostly reviews). Each entry: (question regex, profile key, mode).
#   yn      → the profile holds Yes/No; pick the option that starts with / contains yes|no accordingly
#   text    → the profile value fills a text field (or is matched against options when there are some)
#   ack     → a single-option acknowledgement box: tick it when the profile pre-approved it (Yes)
_STANDARD_QS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"(?i)sponsor"), "requires_sponsorship", "yn"),
    (re.compile(r"(?i)(authori[sz]ed|eligible|legally|right) to work|work authori[sz]ation"), "us_authorized_to_work", "yn"),
    (re.compile(r"(?i)relocat"), "willing_to_relocate", "yn"),
    (re.compile(r"(?i)previously (worked|employed|consulted)|former employee|worked (at|for) .* before"), "previously_worked_here", "yn"),
    (re.compile(r"(?i)hispanic|latin"), "hispanic_latino", "yn"),
    (re.compile(r"(?i)preferred (first )?name|name you.d prefer|what should we call you"), "preferred_name", "text"),
    (re.compile(r"(?i)pronoun"), "pronouns", "text"),
    (re.compile(r"(?i)(where|location).*(expect|intend|plan|would).* work|work location|intended work location|where do you (live|reside)|country of residence|current location"), "work_location", "text"),
    (re.compile(r"(?i)remote|hybrid|on-?site preference|work arrangement"), "remote_preference", "text"),
    (re.compile(r"(?i)salary|compensation expectation|pay expectation|desired (pay|comp)"), "desired_salary", "text"),
    (re.compile(r"(?i)start date|when (can|could) you start|availability|notice period"), "earliest_start_date", "text"),
    (re.compile(r"(?i)how did you (hear|find|learn)|where did you (hear|find)|referral source|source"), "source", "text"),
    (re.compile(r"(?i)visa (status|type)|immigration status"), "visa_status", "text"),
    (re.compile(r"(?i)gender identity|^gender|what gender"), "gender", "text"),
    (re.compile(r"(?i)racial|race|ethnic"), "race_ethnicity", "text"),
    (re.compile(r"(?i)veteran"), "veteran_status", "text"),
    (re.compile(r"(?i)disabilit"), "disability_status", "text"),
    (re.compile(r"(?i)privacy (notice|policy|statement)|data (transfer|processing)|consent to"), "ack_privacy", "ack"),
    (re.compile(r"(?i)certify|true, accurate|accurate and complete|acknowledg"), "ack_certify", "ack"),
]


def norm_question(label: str) -> str:
    """The ANSWER-BANK key for a question: lowercase, punctuation-free, whitespace-collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (label or "").lower())).strip()[:200]


def _pick_option(options: list[str], want: str) -> str:
    """The option that means `want` (a Yes/No, or a value like 'Man' / 'Prefer not to say')."""
    w = (want or "").strip().lower()
    if not w or not options:
        return ""
    if w in ("yes", "no"):
        for o in options:
            ol = o.strip().lower()
            if ol == w or ol.startswith(w + ",") or ol.startswith(w + " ") or ol.startswith(w + "."):
                return o
        return ""
    for o in options:                       # exact, then contains, then 'prefer not to say' families
        if o.strip().lower() == w:
            return o
    for o in options:
        if w in o.strip().lower() or o.strip().lower() in w:
            return o
    if "prefer" in w or "decline" in w or "not to" in w:
        for o in options:
            if re.search(r"(?i)prefer not|decline|rather not", o):
                return o
    return ""


def standard_answer(label: str, kind: str, options: list[str], profile: dict) -> str:
    """The Apply profile's answer to a recurring ATS question ('' when the profile doesn't hold one —
    never a guess). A profile value never overrides a choice the user already made for this question."""
    for rx, key, mode in _STANDARD_QS:
        if not rx.search(label or ""):
            continue
        v = str(profile.get(key) or "").strip()
        if not v:
            return ""
        if mode == "yn":
            return _pick_option(options, v) if options else v
        if mode == "ack":
            return (options[0] if options else "Yes") if v.lower() == "yes" else ""
        if options:
            return _pick_option(options, v)
        return v
    return ""


def plan_fill(fields: list[dict], profile: dict, answers: dict | None = None) -> dict:
    """CODE-OWNED plan: which discovered fields get which value, which stay open for the user.
    fields = [{label, kind, name, required, options}] as the browser found them."""
    filled, open_q = [], []
    for f in group_fields(fields):
        key = map_label(f.get("label") or f.get("name") or f.get("placeholder") or "")
        kind = f.get("kind") or "text"
        if key == "resume" and kind == "file":
            filled.append({**f, "key": "resume", "value": "<résumé file>"})
            continue
        if key and kind in ("text", "email", "tel", "url", "textarea"):
            v = value_for(key, profile, answers)
            if v:
                filled.append({**f, "key": key, "value": v})
                continue
        # a custom question: the user's saved answer wins; else it stays open (required ones block submit)
        label = f.get("label") or f.get("name") or ""
        if not label.strip():
            continue                       # a nameless input (a combobox's search box) is not a question
        _bank = {norm_question(k): v for k, v in (answers or {}).items()}
        ans = ((answers or {}).get(label) or (answers or {}).get(f.get("name") or "") or _bank.get(norm_question(label))
               or standard_answer(label, kind, f.get("options") or [], profile))
        if ans and kind in ("radio", "checkbox", "select") and (f.get("options") or []) and ans not in (f.get("options") or []):
            ans = _pick_option(f.get("options") or [], str(ans))          # a remembered answer must name a real option
        if ans:
            filled.append({**f, "key": "answer", "value": str(ans)})
        elif kind in ("text", "textarea", "select", "radio", "checkbox", "email", "tel", "url"):
            if label.lower().startswith("start typing") or label.lower().startswith("type here"):
                continue
            open_q.append({"label": label, "kind": kind, "required": bool(f.get("required")), "options": f.get("options") or [],
                           "voluntary": bool(_VOLUNTARY_RX.search(label))})
    return {"filled": filled, "open": open_q,
            "blocking": [q for q in open_q if q["required"]]}


# ---- the browser part (separate process) -------------------------------------------------------------
def _discover_fields(scope) -> list[dict]:
    """Every visible input / textarea / select in the form with its best label."""
    js = r"""
    () => {
      const out = [];
      const lab = el => {
        let t = "";
        if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) t = l.innerText; }
        if (!t) { const p = el.closest("label"); if (p) t = p.innerText; }
        if (!t && el.getAttribute("aria-label")) t = el.getAttribute("aria-label");
        if (!t && el.getAttribute("aria-labelledby")) { const l = document.getElementById(el.getAttribute("aria-labelledby")); if (l) t = l.innerText; }
        if (!t) { const wrap = el.closest(".field, .application-question, [class*='field'], [class*='question'], li, div"); const l = wrap && wrap.querySelector("label, legend, .label, [class*='label']"); if (l) t = l.innerText; }
        if (!t) t = el.placeholder || el.name || "";
        return (t || "").replace(/\s+/g, " ").trim().slice(0, 160);
      };
      // the QUESTION a radio / checkbox / combobox belongs to: a fieldset legend, a labelled group, or the
      // nearest preceding label-like text above the group
      let gkCounter = 0;
      const groupOf = el => {
        const type = (el.type || "").toLowerCase();
        if (type !== "radio" && type !== "checkbox") return null;
        let anc = el.parentElement;
        while (anc && anc !== document.body && anc.tagName !== "FORM" && anc.querySelectorAll(`input[type='${type}']`).length < 2) anc = anc.parentElement;
        if (!anc || anc === document.body || anc.tagName === "FORM" || anc.querySelectorAll("input, select, textarea").length > 40) {
          anc = el.closest("fieldset, [role='group'], [role='radiogroup']") || el.closest("label") || el.parentElement;   // a lone option is its own group
        }
        if (!anc.dataset.rosterGk) anc.dataset.rosterGk = "g" + (++gkCounter) + "_" + Math.random().toString(36).slice(2, 6);
        return anc;
      };
      const groupLab = el => {
        const fs = el.closest("fieldset"); const lg = fs && fs.querySelector("legend"); if (lg && lg.innerText.trim()) return lg.innerText.replace(/\s+/g, " ").trim().slice(0, 160);
        const g = el.closest("[role='group'],[role='radiogroup']");
        if (g) { const by = g.getAttribute("aria-labelledby"); const l = by && document.getElementById(by); if (l && l.innerText.trim()) return l.innerText.replace(/\s+/g, " ").trim().slice(0, 160); if (g.getAttribute("aria-label")) return g.getAttribute("aria-label").slice(0, 160); }
        const grp = groupOf(el);
        if (grp) {   // the question text usually sits INSIDE the group container, before its first option
          for (const ch of grp.children) {
            if (ch.querySelector("input, select, textarea") || ch.tagName === "INPUT") break;
            const txt = (ch.innerText || "").replace(/\s+/g, " ").trim();
            if (txt && txt.length <= 200) return txt.slice(0, 160);
          }
        }
        let node = grp || el.closest("label") || el; 
        for (let depth = 0; node && depth < 6; depth++) {
          let sib = node.previousElementSibling;
          while (sib) {
            const txt = (sib.innerText || "").replace(/\s+/g, " ").trim();
            if (txt && txt.length <= 200 && !sib.querySelector("input, select, textarea")) return txt.slice(0, 160);
            if (sib.querySelector("input, select, textarea")) break;
            sib = sib.previousElementSibling;
          }
          node = node.parentElement;
        }
        return "";
      };
      const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return (r.width > 0 || el.type === "file") && s.visibility !== "hidden" && s.display !== "none"; };
      document.querySelectorAll("input, textarea, select").forEach(el => {
        const type = (el.type || "text").toLowerCase();
        if (["hidden", "submit", "button", "image", "reset"].includes(type)) return;
        if (!vis(el)) return;
        let kind = el.tagName === "SELECT" ? "select" : el.tagName === "TEXTAREA" ? "textarea" : type;
        const options = el.tagName === "SELECT" ? [...el.options].map(o => o.text.trim()).filter(Boolean).slice(0, 40) : [];
        const isCombo = el.getAttribute("role") === "combobox" || (el.getAttribute("aria-autocomplete") || "") !== "";
        const gl = (kind === "radio" || kind === "checkbox" || isCombo) ? groupLab(el) : "";
        const grp = groupOf(el); const gk = grp ? grp.dataset.rosterGk : "";
        const sel = el.id ? `#${CSS.escape(el.id)}` : (el.name ? `[name="${el.name}"]` : "");
        out.push({ label: lab(el), group_label: gl, group_key: gk, name: el.name || "", id: el.id || "", kind, required: !!el.required || el.getAttribute("aria-required") === "true" || /\*\s*$/.test(gl) || /\*\s*$/.test(lab(el)),
                   placeholder: el.placeholder || "", options, selector: (kind === "radio" || kind === "checkbox") && el.id ? `#${CSS.escape(el.id)}` : sel });
      });
      return out;
    }"""
    return scope.evaluate(js)


def _run_browser(mode: str, job: dict) -> dict:
    from playwright.sync_api import sync_playwright  # noqa: WPS433 — only in the subprocess
    url = job["url"]
    profile = job.get("profile") or {}
    answers = job.get("answers") or {}
    resume = job.get("resume") or {}
    ats = detect_ats(url)
    out: dict = {"ats": ats, "url": url, "status": "filled", "filled": [], "open": [], "blocking": [], "notes": []}
    if ats == "workday":
        out.update(status="needs_you", reason="This portal requires creating an account and signing in — Roster never does that for you. Your answers are prepared below; open the link and paste.")
        return out
    resume_path = ""
    if resume.get("b64"):
        _dir = tempfile.mkdtemp()
        _name = re.sub(r"[^A-Za-z0-9._ -]+", "_", resume.get("name") or "resume.pdf") or "resume.pdf"
        resume_path = os.path.join(_dir, _name)            # the ATS shows the file name — keep the user's
        with open(resume_path, "wb") as fh:
            fh.write(base64.b64decode(resume["b64"]))
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1200, "height": 1600},
                                  user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 roster-apply")
        page = ctx.new_page()
        page.set_default_timeout(20000)
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            # reach the FORM: Lever's posting page → "Apply for this job"; Ashby → "Apply" tab; Greenhouse embeds an iframe
            for sel in ("a.postings-btn", "a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
                        "a:has-text('Apply Now')", "button:has-text('Apply')", "a[href$='/apply']"):
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        loc.click()
                        page.wait_for_timeout(1500)
                        break
                except Exception:  # noqa: BLE001
                    continue
            scope = page
            for fr in page.frames:
                if "greenhouse" in (fr.url or "") and fr != page.main_frame:
                    scope = fr
                    break
            html = scope.content()
            if _LOGIN_RX.search(scope.title() or "") and scope.locator("input[type=password]").count():
                out.update(status="needs_you", reason="The apply page asks you to sign in — Roster never enters passwords. Open it yourself; your answers are ready below.")
                return out
            fields = _discover_fields(scope)
            plan = plan_fill(fields, profile, answers)
            out["open"], out["blocking"] = plan["open"], plan["blocking"]
            done = []
            for f in plan["filled"]:
                sel = f.get("selector")
                if not sel:
                    continue
                try:
                    loc = scope.locator(sel).first
                    if f["key"] == "resume":
                        if resume_path:
                            loc.set_input_files(resume_path)
                            done.append({"label": f["label"], "value": resume.get("name") or "résumé"})
                    elif f["kind"] == "select":
                        loc.select_option(label=f["value"])
                        done.append({"label": f["label"], "value": f["value"]})
                    elif f["kind"] in ("radio", "checkbox"):
                        osel = (f.get("option_selectors") or {}).get(f["value"]) or ""
                        if osel:
                            scope.locator(osel).first.check()
                        else:
                            scope.locator(f"label:has-text('{f['value']}')").first.click()
                        done.append({"label": f["label"], "value": f["value"]})
                    else:
                        loc.fill(f["value"])
                        done.append({"label": f["label"], "value": f["value"]})
                except Exception as e:  # noqa: BLE001 — one field failing is a note, not a failure
                    out["notes"].append(f"couldn't fill '{f.get('label')}': {str(e)[:80]}")
            out["filled"] = done
            # a VISIBLE challenge widget blocks; an invisible score-only script is the page's own business
            try:
                out["captcha"] = any(scope.locator(sel).first.is_visible() for sel in
                                     ("iframe[src*='recaptcha/api2/anchor']", "iframe[src*='hcaptcha.com']", ".g-recaptcha", ".h-captcha", "[data-sitekey]")
                                     if scope.locator(sel).count())
            except Exception:  # noqa: BLE001
                out["captcha"] = False
            page.wait_for_timeout(800)
            if mode == "submit":
                if out["blocking"]:
                    out.update(status="needs_you", reason="Required questions still need your answer.")
                elif out["captcha"]:
                    out.update(status="needs_you", reason="This form shows a CAPTCHA challenge — Roster never solves one. Open the link; the fields are prepared.")
                else:
                    clicked = False
                    for sel in ("button[type=submit]:has-text('Submit')", "input[type=submit]", "button:has-text('Submit application')",
                                "button:has-text('Submit Application')", "button[type=submit]"):
                        try:
                            loc = scope.locator(sel).first
                            if loc.count() and loc.is_visible():
                                loc.click()
                                clicked = True
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    page.wait_for_timeout(4000)
                    body = (page.content() or "")[:200000]
                    ok = bool(re.search(r"(?i)thank you|application (has been )?(submitted|received)|we('ve| have) received|successfully", body))
                    challenge = bool(re.search(r"(?i)recaptcha/api2/bframe|hcaptcha\.com/captcha", body))
                    out.update(status=("submitted" if (clicked and ok) else "needs_you"),
                               reason=("" if (clicked and ok) else ("A CAPTCHA challenge appeared on submit — Roster never solves one; open the link and finish there (your answers are prepared)."
                                                                    if challenge else "No confirmation was seen after submitting — check the apply page yourself before retrying.")))
            out["screenshot_b64"] = base64.b64encode(page.screenshot(full_page=True, type="png")).decode()
            out["page_title"] = page.title()
        except Exception as e:  # noqa: BLE001
            out.update(status="failed", reason=f"{type(e).__name__}: {str(e)[:160]}")
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
            if resume_path:
                try:
                    os.remove(resume_path); os.rmdir(os.path.dirname(resume_path))
                except Exception:  # noqa: BLE001
                    pass
    return out


if __name__ == "__main__":
    mode, inp, outp = sys.argv[1], sys.argv[2], sys.argv[3]
    job = json.load(open(inp))
    res = _run_browser(mode, job)
    json.dump(res, open(outp, "w"))
