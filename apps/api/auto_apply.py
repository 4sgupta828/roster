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
    (re.compile(r"(?i)^(first|given)\s*name"), "first_name"),
    (re.compile(r"(?i)^(last|family|sur)\s*name"), "last_name"),
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


def plan_fill(fields: list[dict], profile: dict, answers: dict | None = None) -> dict:
    """CODE-OWNED plan: which discovered fields get which value, which stay open for the user.
    fields = [{label, kind, name, required, options}] as the browser found them."""
    filled, open_q = [], []
    for f in fields:
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
        ans = (answers or {}).get(label) or (answers or {}).get(f.get("name") or "")
        if ans:
            filled.append({**f, "key": "answer", "value": str(ans)})
        elif kind in ("text", "textarea", "select", "radio", "checkbox", "email", "tel", "url"):
            open_q.append({"label": label, "kind": kind, "required": bool(f.get("required")), "options": f.get("options") or []})
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
      const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return (r.width > 0 || el.type === "file") && s.visibility !== "hidden" && s.display !== "none"; };
      document.querySelectorAll("input, textarea, select").forEach(el => {
        const type = (el.type || "text").toLowerCase();
        if (["hidden", "submit", "button", "image", "reset"].includes(type)) return;
        if (!vis(el)) return;
        let kind = el.tagName === "SELECT" ? "select" : el.tagName === "TEXTAREA" ? "textarea" : type;
        const options = el.tagName === "SELECT" ? [...el.options].map(o => o.text.trim()).filter(Boolean).slice(0, 40) : [];
        out.push({ label: lab(el), name: el.name || "", id: el.id || "", kind, required: !!el.required || el.getAttribute("aria-required") === "true",
                   placeholder: el.placeholder || "", options, selector: el.id ? `#${CSS.escape(el.id)}` : (el.name ? `[name="${el.name}"]` : "") });
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
        suffix = os.path.splitext(resume.get("name") or "resume.pdf")[1] or ".pdf"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(base64.b64decode(resume["b64"]))
            resume_path = tf.name
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
                    else:
                        loc.fill(f["value"])
                        done.append({"label": f["label"], "value": f["value"]})
                except Exception as e:  # noqa: BLE001 — one field failing is a note, not a failure
                    out["notes"].append(f"couldn't fill '{f.get('label')}': {str(e)[:80]}")
            out["filled"] = done
            out["captcha"] = bool(_CAPTCHA_RX.search(html))
            page.wait_for_timeout(800)
            if mode == "submit":
                if out["blocking"]:
                    out.update(status="needs_you", reason="Required questions still need your answer.")
                elif out["captcha"]:
                    out.update(status="needs_you", reason="This form has a CAPTCHA — Roster never bypasses one. Open the link; the fields are prepared.")
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
                    out.update(status=("submitted" if (clicked and ok) else "needs_you"),
                               reason=("" if (clicked and ok) else "No confirmation was seen after submitting — check the apply page yourself before retrying."))
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
                    os.remove(resume_path)
                except Exception:  # noqa: BLE001
                    pass
    return out


if __name__ == "__main__":
    mode, inp, outp = sys.argv[1], sys.argv[2], sys.argv[3]
    job = json.load(open(inp))
    res = _run_browser(mode, job)
    json.dump(res, open(outp, "w"))
