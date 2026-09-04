"""AUTO-APPLY — the CODE-OWNED answer mapping: a form question (label, kind, options) → the Apply
profile's value (standard questions), the user's saved answers, or nothing (a draft is the caller's,
policy-gated, step). Used by apply_plan.bind_plan. No browser here.
"""
from __future__ import annotations

import re

# ---- pure helpers (unit-tested; no browser) ---------------------------------------------------------
_FIELD_RX: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)^(preferred\s+)?(first|given)\s*name"), "first_name"),
    (re.compile(r"(?i)^(preferred\s+)?(last|family|sur)\s*name"), "last_name"),
    (re.compile(r"(?i)^(most recent|current|present|last)\s+(company|employer|organization)|(current|previous|most recent).{0,25}(employer|company)\??$"), "current_company"),
    (re.compile(r"(?i)^(most recent|current|present|last)\s+(title|role|job title|position)|(current|previous|most recent).{0,25}(job title|title)\??$"), "current_title"),
    (re.compile(r"(?i)^school|university|college name|institution"), "school"),
    (re.compile(r"(?i)^degree|highest (level of )?education|education level"), "highest_degree"),
    (re.compile(r"(?i)discipline|field of study|major"), "field_of_study"),
    (re.compile(r"(?i)(graduation|grad)\s*(year|date)|year of graduation|end date"), "grad_year"),
    (re.compile(r"(?i)(state|province).{0,20}(reside|live|located)|which (u\.?s\.? )?state"), "region"),
    (re.compile(r"(?i)^where are you (located|based)|^current location|^location\b"), "city"),
    (re.compile(r"(?i)^(full\s*)?(legal\s*)?name$|^your (full |legal )?name|^name\s*\*?$|^legal name"), "full_name"),
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
_VOLUNTARY_RX = re.compile(r"(?i)racial|race|ethnic|gender|pronoun|veteran|disabilit|self-identif|sexual orientation|select all that apply|first-generation|transgender|lgbt")
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
    t = re.sub(r"\s+", " ", (label or "")).strip().rstrip("*✱✲⁎＊").strip()
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
    (re.compile(r"(?i)(authori[sz]ed|eligible|legally|right).{0,25}work|work authori[sz]ation"), "us_authorized_to_work", "yn"),
    (re.compile(r"(?i)relocat"), "willing_to_relocate", "yn"),
    (re.compile(r"(?i)previously (worked|employed|consulted)|former employee|worked (at|for) .* before|have you (ever )?(worked|been employed|consulted|contracted)|(ever|previously|currently).{0,20}(work(ed|ing)?|employed|contractor).{0,40}(at|for|with)"), "previously_worked_here", "yn"),
    (re.compile(r"(?i)marketing communications|receive (emails|updates|communications)|subscribe"), "marketing_opt_in", "yn"),
    (re.compile(r"(?i)zip|postal"), "postal_code", "text"),
    (re.compile(r"(?i)^country"), "country", "text"),
    (re.compile(r"(?i)hispanic|latin"), "hispanic_latino", "yn"),
    (re.compile(r"(?i)preferred (first )?name|name you.d prefer|what should we call you"), "preferred_name", "text"),
    (re.compile(r"(?i)pronoun"), "pronouns", "text"),
    (re.compile(r"(?i)(where|location).*(expect|intend|plan|would).* work|work location|intended work location|where do you (live|reside)|country of residence|current location"), "work_location", "text"),
    (re.compile(r"(?i)remote|hybrid|on-?site preference|work arrangement"), "remote_preference", "text"),
    (re.compile(r"(?i)salary|compensation expectation|pay expectation|desired (pay|comp)"), "desired_salary", "text"),
    (re.compile(r"(?i)start date|when (can|could) you start|availability|notice period"), "earliest_start_date", "text"),
    (re.compile(r"(?i)how did you (hear|find|learn)|where did you (hear|find)|referral source|source"), "source", "text"),
    (re.compile(r"(?i)visa (status|type)|immigration status"), "visa_status", "text"),
    (re.compile(r"(?i)(links? to|share links|relevant (technical )?work|work samples|code samples|portfolio|github|open[- ]source)"), "work_links", "text"),
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
            if re.search(r"(?i)prefer not|decline|rather not|don.?t wish|do not wish|not wish to|choose not", o):
                return o
    return ""


def standard_answer(label: str, kind: str, options: list[str], profile: dict) -> str:
    """The Apply profile's answer to a recurring ATS question ('' when the profile doesn't hold one —
    never a guess). A profile value never overrides a choice the user already made for this question."""
    for rx, key, mode in _STANDARD_QS:
        if not rx.search(label or ""):
            continue
        if key == "work_links":     # the profile's public work, most technical first
            v = "\n".join(str(profile.get(k) or "").strip() for k in ("github", "portfolio_website", "linkedin") if str(profile.get(k) or "").strip())
        else:
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
        ans = (answers or {}).get(label) or (answers or {}).get(f.get("name") or "") or _bank.get(norm_question(label))
        src = "saved answer" if ans else ""
        if not ans:
            ans = standard_answer(label, kind, f.get("options") or [], profile)
            src = "profile" if ans else ""
        f = {**f, "source": src}
        if ans and kind in ("radio", "checkbox", "select") and (f.get("options") or []) and ans not in (f.get("options") or []):
            ans = _pick_option(f.get("options") or [], str(ans))          # a remembered answer must name a real option
        if ans:
            filled.append({**f, "key": "answer", "value": str(ans)})
        elif kind in ("text", "textarea", "select", "radio", "checkbox", "email", "tel", "url"):
            if label.lower().startswith(("start typing", "type here", "apply for this job", "select...")):
                continue
            if any(q["label"] == label for q in open_q):
                continue                       # a combobox renders two inputs for one question
            if re.fullmatch(r"(?i)other( \(please specify\))?|please specify|if other, please specify", label) and not f.get("required"):
                continue                       # the free-text box under a checkbox group is not a question
            pk = next((key for rx, key, _m in _STANDARD_QS if rx.search(label)), "")
            open_q.append({"label": label, "kind": kind, "required": bool(f.get("required")), "options": f.get("options") or [],
                           "voluntary": bool(_VOLUNTARY_RX.search(label)), "profile_key": pk, "combo": bool(f.get("combo")),
                           "selector": f.get("selector") or ""})
    return {"filled": filled, "open": open_q,
            "blocking": [q for q in open_q if q["required"]]}


# The browser executor that used to live here (headless Chromium on the server) is GONE: job sites
# refuse automated submits from datacenter browsers and a bookmarklet cannot attach files or enter
# cross-origin frames. Planning is definition-driven (apply_adapters + apply_plan); execution is the
# Roster Apply Chrome extension in the user's own browser (apps/extension). The helpers above are the
# code-owned mapping the planner uses.
