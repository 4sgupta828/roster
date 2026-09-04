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
             | legal (certifications / consents — the user's standing pre-approval or their own click)
             | file (the résumé) | never (CAPTCHA, passwords)
"""
from __future__ import annotations

import re

_EEO_RX = re.compile(r"(?i)gender|race|ethnic|hispanic|latin|veteran|disabilit|sexual orientation|transgender|self-identif|pronoun")
_LEGAL_RX = re.compile(r"(?i)certify|acknowledg|privacy (notice|policy|statement)|consent|i agree|terms|authorize.*(background|check)|attest|true and (accurate|complete)")
_NEVER_RX = re.compile(r"(?i)captcha|password")


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
    return "open"


def question(*, id: str, label: str, kind: str, options: list[str] | None = None, required: bool = False,
             group: str = "", selector: str = "", policy: str | None = None) -> dict:
    label = re.sub(r"\s+", " ", (label or "")).strip()
    return {"id": str(id), "label": label, "kind": kind, "options": [str(o) for o in (options or []) if str(o).strip()],
            "required": bool(required), "group": group, "selector": selector, "policy": policy or policy_for(label, kind)}


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
