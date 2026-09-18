"""APPLICATION-FORM DEFINITIONS, per ATS, from the ATS itself — never from DOM guessing.

`fetch_form(url)` → a `FormDef` (or None when no adapter owns the host — Workday and unknown career
sites are NOT planned, which is what keeps "never create an account for the user" structural).

FormDef = {
  ats, board, job_id, title, company, form_url,
  questions: [ {id, label, kind, options, required, group, selector, policy} ]
}
  kind     : text | textarea | email | tel | url | select | multiselect | radio | checkbox | boolean | file | date
  selector : how the EXTENSION finds the field on the live page (stable ids/names from the definition)
  policy   : open | identity_sensitive (EEO / demographics — profile defaults only, never drafted)
             | eligibility (knock-out / hard facts — work authorization, sponsorship, visa, clearance,
               salary expectation, years of experience, relocation, degree — filled from the profile
               ONLY, never drafted; unanswered ones are flagged for the candidate, never guessed)
             | legal (certifications / consents — the user's standing pre-approval or their own click)
             | file (the résumé) | never (CAPTCHA, passwords)
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_EEO_RX = re.compile(r"(?i)gender|race|ethnic|hispanic|latin|veteran|disabilit|sexual orientation|orientation\?|transgender|self-identif|pronoun|\bage\b|communities do you|identify (as|with|your)|diversity")
_LEGAL_RX = re.compile(r"(?i)certify|acknowledg|privacy (notice|policy|statement)|consent|i agree|terms|authorize.*(background|check)|attest|true and (accurate|complete)")
_NEVER_RX = re.compile(r"(?i)captcha|password")
# ELIGIBILITY — a hard/knock-out fact about the candidate (can they legally/factually take the role) or a
# quantitative claim an LLM would happily fabricate. These are filled from the profile deterministically or
# left for the candidate; they are NEVER drafted (bind_plan / draftable). The patterns are deliberately
# narrow — they demand a knock-out token (sponsor/visa/authori.e/citizen/clearance/relocate/onsite) or a
# quantified phrase ("years ... experience", "salary/compensation expectation") — so open free-text
# questions ("Why us?", "Describe your experience with X") stay draftable.
_ELIGIBILITY_RX = re.compile(
    r"(?i)"
    r"(authori[sz]ed|eligible|legally).{0,25}\bwork\b|work authori[sz]ation|right to work"
    r"|require.{0,20}sponsor|need.{0,20}sponsor|visa sponsor|\bsponsorship\b"
    r"|visa (status|type)|immigration status|work permit"
    r"|\bcitizen\b|green.?card|permanent resident"
    r"|security clearance|clearance level"
    r"|years?\s+(of\s+)?experience|how many years|years'?\s+experience"
    r"|salary (expectation|requirement|range)|compensation expectation|desired (salary|pay|compensation)"
    r"|expected (salary|compensation|pay)|pay expectation"
    r"|willing to relocate|able to relocate|open to relocat"
    r"|(able|willing|open) to (work|working|commute|be).{0,20}(on-?site|in.person|in the office|the office)"
    r"|notice period|earliest (start|available)|when can you start|available to start"
    r"|do you (have|hold).{0,20}(degree|bachelor|master|phd|diploma)|highest (level of )?(education|degree)|minimum (education|degree)")


def policy_for(label: str, kind: str) -> str:
    t = label or ""
    if kind == "file":
        return "file"
    if _NEVER_RX.search(t):
        return "never"
    if _EEO_RX.search(t):
        return "identity_sensitive"
    if _LEGAL_RX.search(t):
        return "legal"
    if _ELIGIBILITY_RX.search(t):
        return "eligibility"
    return "open"


def question(*, id: str, label: str, kind: str, options: list[str] | None = None, required: bool = False,
             group: str = "", selector: str = "", policy: str | None = None) -> dict:
    label = re.sub(r"\s+", " ", (label or "")).strip()
    return {"id": str(id), "label": label, "kind": kind, "options": [str(o) for o in (options or []) if str(o).strip()],
            "required": bool(required), "group": group, "selector": selector, "policy": policy or policy_for(label, kind)}


_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
                    "gh_src", "ref", "source", "src", "trackingid", "trk"}
_UUID_RX = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def application_dedup_key(url: str) -> str:
    """A STABLE identity for a job posting so the same role linked two ways — tracking params, the embed
    form vs the public page, an `/apply` suffix — dedupes to ONE application (no duplicate submissions).
    ATS + the ATS's own job id when we can parse it (globally unique per ATS); otherwise a normalized URL
    (lowercased host without `www.`, tracking params dropped, the rest sorted, trailing slash trimmed).
    Pure — unit-tested, no network."""
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower()
    ats = detect(u)
    if ats == "greenhouse":
        m = re.search(r"(?:gh_jid=|/jobs/|[?&]token=)(\d{5,})", low)
        if m:
            return f"greenhouse:{m.group(1)}"
    if ats in ("lever", "ashby"):
        m = _UUID_RX.search(low)
        if m:
            return f"{ats}:{m.group(0).lower()}"
    p = urlsplit(low)
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    q = sorted((k, v) for k, v in parse_qsl(p.query) if k not in _TRACKING_PARAMS)
    path = (p.path or "").rstrip("/")
    return f"{host}{path}" + (("?" + urlencode(q)) if q else "")


def pick_existing_application(apps: list[dict], url: str) -> dict | None:
    """The application already on file for this posting, if any (`apps` newest-first, as list_applications
    returns them): the most recent still-open one to REUSE, else the most recent already-submitted one so
    the caller can warn 'already applied'. None when the posting is new. Pure — no DB."""
    key = application_dedup_key(url)
    if not key:
        return None
    matches = [a for a in (apps or []) if application_dedup_key(a.get("url") or "") == key]
    if not matches:
        return None
    return next((a for a in matches if a.get("status") != "submitted"), matches[0])


def detect(url: str) -> str:
    u = (url or "").lower()
    if "greenhouse.io" in u or "gh_jid=" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    if "myworkdayjobs.com" in u or "workday" in u:
        return "workday"
    return ""


async def fetch_form(url: str) -> dict | None:
    """The form definition for a posting URL, from the ATS that hosts it; None when unsupported."""
    ats = detect(url)
    if ats == "greenhouse":
        from . import greenhouse
        return await greenhouse.fetch_form(url)
    if ats == "ashby":
        from . import ashby
        return await ashby.fetch_form(url)
    if ats == "lever":
        from . import lever
        return await lever.fetch_form(url)
    return None
