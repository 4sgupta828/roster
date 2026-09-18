"""APPLY SAFETY GATES — deterministic, code-owned, no browser / no network / no LLM.

The gates career-ops enforces before it ever drafts an answer, ported as pure functions Roster can
unit-test and reuse from the plan endpoint:

  is_blocked()        a personal blacklist ("never apply to X") + a global blocklist (ATS/hosts where
                      automation is not permitted — the ToS / vendor-opt-out lever Codex asked for).
  rate_limit_reason() per-user and per-ATS caps on applications in a rolling window, so a runaway loop
                      can never fire hundreds of applications.
  knockout_reasons()  hard, BINARY eligibility mismatches read straight off the JD text vs the profile
                      (sponsorship / citizenship / work-authorization / on-site) — a warning, not a
                      forced block: the candidate decides. No requirement is inferred by a model here.

All spending / model work stays out of this file by design.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit


def _norm_co(s: str) -> str:
    """A company name reduced for matching: lowercased, legal suffixes + punctuation dropped."""
    t = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    t = re.sub(r"\b(inc|llc|ltd|corp|co|gmbh|plc|sa|ag|the)\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _host(url: str) -> str:
    h = (urlsplit(url or "").netloc or "").lower()
    return h[4:] if h.startswith("www.") else h


def global_blocklist() -> list[str]:
    """Hosts / companies where Roster must not automate an application (ToS, vendor opt-out). Comma-separated
    in ROSTER_APPLY_BLOCKLIST; empty by default. Entries match a URL host (substring) or a company name."""
    return [e.strip().lower() for e in (os.environ.get("ROSTER_APPLY_BLOCKLIST", "") or "").split(",") if e.strip()]


def is_blocked(company: str, url: str, *, personal: list[str] | None = None, glob: list[str] | None = None) -> str:
    """Why this application is blocked ('' when it is allowed). Personal blacklist first (the user's own
    'never apply here'), then the global blocklist. Matches a company name (normalized substring, both ways)
    or a URL host."""
    host, co = _host(url), _norm_co(company)
    for scope, entries in (("your blacklist", personal or []), ("Roster's blocklist", glob if glob is not None else global_blocklist())):
        for raw in entries:
            e = (raw or "").strip().lower()
            if not e:
                continue
            if "." in e and host and (e == host or host.endswith("." + e) or e in host):
                return f"This posting's site is on {scope}."
            en = _norm_co(e)
            if en and co and (en in co or co in en):
                return f"“{company}” is on {scope}."
    return ""


def _cap(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def rate_limit_reason(apps: list[dict], *, ats: str = "", now: datetime | None = None,
                      window_hours: int = 24, max_total: int | None = None, max_per_ats: int | None = None) -> str:
    """Why a NEW application must wait ('' when under the caps). Counts applications created in the trailing
    `window_hours` from `apps` (as list_applications returns them: dicts with ISO `created_at`, and `ats`).
    Caps default to ROSTER_APPLY_MAX_PER_DAY (50) and ROSTER_APPLY_MAX_PER_ATS_PER_DAY (25) — generous
    guardrails against a runaway loop, not a product limit."""
    now = now or datetime.now(timezone.utc)
    max_total = _cap("ROSTER_APPLY_MAX_PER_DAY", 50) if max_total is None else max_total
    max_per_ats = _cap("ROSTER_APPLY_MAX_PER_ATS_PER_DAY", 25) if max_per_ats is None else max_per_ats
    cutoff = now - timedelta(hours=window_hours)
    recent = [a for a in (apps or []) if _within(a.get("created_at"), cutoff)]
    if max_total and len(recent) >= max_total:
        return f"You've started {len(recent)} applications in the last {window_hours}h (limit {max_total}). Try again later."
    if ats and max_per_ats:
        n = sum(1 for a in recent if (a.get("ats") or "") == ats)
        if n >= max_per_ats:
            return f"You've started {n} {ats} applications in the last {window_hours}h (limit {max_per_ats}). Try again later."
    return ""


def _within(created_at, cutoff: datetime) -> bool:
    if not created_at:
        return False
    try:
        dt = created_at if isinstance(created_at, datetime) else datetime.fromisoformat(str(created_at))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


def _yn(v) -> bool | None:
    """A profile Yes/No → True/False; None when unset/ambiguous (never assumed)."""
    s = str(v or "").strip().lower()
    if s in ("yes", "y", "true", "1"):
        return True
    if s in ("no", "n", "false", "0"):
        return False
    return None


# JD signals — deliberately conservative: each requires an explicit disqualifying phrase, so a warning
# only fires on a hard, stated mismatch (never inferred).
_NO_SPONSOR_RX = re.compile(r"(?i)(no|not|without|unable to|cannot|can not|do(es)? not).{0,30}sponsor|sponsorship is not|not (able|available) to sponsor|no visa sponsor")
_CITIZEN_ONLY_RX = re.compile(r"(?i)(u\.?s\.?|american) citizens? (only|required)|must be a (u\.?s\.?|united states) citizen|citizenship is required|require(s|d)? (u\.?s\.?|american) citizenship")
_MUST_AUTH_RX = re.compile(r"(?i)must be (legally )?authori[sz]ed to work|require(s|d)? (work )?authori[sz]ation|legally (authori[sz]ed|able) to work.{0,20}(without|no) sponsor")
_ONSITE_RX = re.compile(r"(?i)(fully |100% )?on-?site|in-?office (required|role|position)|must (be able to )?(work|report).{0,20}(on-?site|in the office|in office)|not (a )?remote|no remote|onsite required")


def knockout_reasons(jd_text: str, profile: dict) -> list[dict]:
    """Hard binary eligibility mismatches between the JOB (its own stated words) and the PROFILE. Each is a
    {code, message} WARNING for the candidate — not a forced block. Empty when nothing hard conflicts.
    Nothing here is guessed: a warning fires only when the JD explicitly states the bar AND the profile
    explicitly says the candidate does not clear it."""
    jd = jd_text or ""
    p = profile or {}
    out = []
    needs_sponsor = _yn(p.get("requires_sponsorship"))
    authorized = _yn(p.get("us_authorized_to_work"))
    citizen = _yn(p.get("us_citizen_or_permanent_resident"))
    onsite_ok = _yn(p.get("can_work_onsite"))
    remote_only = "remote" in str(p.get("remote_preference") or "").lower() and "only" in str(p.get("remote_preference") or "").lower()
    if needs_sponsor is True and _NO_SPONSOR_RX.search(jd):
        out.append({"code": "sponsorship", "message": "This role states it does not offer visa sponsorship, and your profile says you require it."})
    if citizen is False and _CITIZEN_ONLY_RX.search(jd):
        out.append({"code": "citizenship", "message": "This role requires U.S. citizenship, which your profile does not indicate."})
    if authorized is False and _MUST_AUTH_RX.search(jd):
        out.append({"code": "work_authorization", "message": "This role requires existing work authorization, which your profile does not indicate."})
    if (onsite_ok is False or remote_only) and _ONSITE_RX.search(jd):
        out.append({"code": "onsite", "message": "This role is on-site, and your profile indicates you cannot / prefer not to work on-site."})
    return out
