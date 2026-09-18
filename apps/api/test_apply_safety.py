"""Deterministic apply-safety gates: blocklist, rate limits, knock-out pre-scan (no browser/network/LLM)."""
from datetime import datetime, timedelta, timezone

from api.apply_safety import is_blocked, knockout_reasons, rate_limit_reason


def test_personal_blacklist_and_global_blocklist_match_company_or_host():
    # personal blacklist, by company name (normalized, suffix-insensitive)
    assert is_blocked("Acme, Inc.", "https://boards.greenhouse.io/acme/jobs/1", personal=["acme"], glob=[])
    assert "your blacklist" in is_blocked("Acme Corp", "https://x/y", personal=["Acme Corporation"], glob=[])
    # global blocklist, by host
    r = is_blocked("Whoever", "https://jobs.badats.com/x", personal=[], glob=["badats.com"])
    assert r and "Roster's blocklist" in r
    # a clean posting is not blocked
    assert is_blocked("Gusto", "https://boards.greenhouse.io/gusto/jobs/1", personal=["acme"], glob=["badats.com"]) == ""
    # host match is not fooled by a lookalike substring in the path
    assert is_blocked("Gusto", "https://boards.greenhouse.io/gusto/jobs/acme", personal=[], glob=["acme.com"]) == ""


def test_rate_limits_count_only_the_trailing_window_and_respect_per_ats():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    def app(hrs_ago, ats="greenhouse"):
        return {"created_at": (now - timedelta(hours=hrs_ago)).isoformat(), "ats": ats}
    recent = [app(1) for _ in range(3)] + [app(48) for _ in range(10)]   # 10 are outside the 24h window
    assert rate_limit_reason(recent, now=now, max_total=5) == ""          # only 3 count → under 5
    assert rate_limit_reason([app(1) for _ in range(5)], now=now, max_total=5)     # at the cap → blocked
    # per-ATS cap fires even when the global cap is fine
    mixed = [app(1, "greenhouse") for _ in range(3)] + [app(1, "lever") for _ in range(1)]
    assert rate_limit_reason(mixed, ats="greenhouse", now=now, max_total=100, max_per_ats=3)
    assert rate_limit_reason(mixed, ats="lever", now=now, max_total=100, max_per_ats=3) == ""


def test_knockouts_fire_only_on_an_explicit_jd_bar_and_a_conflicting_profile():
    # sponsorship: JD says no sponsorship, profile requires it → warn
    jd = "We are unable to sponsor work visas for this position."
    assert [k["code"] for k in knockout_reasons(jd, {"requires_sponsorship": "Yes"})] == ["sponsorship"]
    # same JD, but the profile does NOT need sponsorship → no warning
    assert knockout_reasons(jd, {"requires_sponsorship": "No"}) == []
    # citizenship-only role vs a non-citizen profile
    assert [k["code"] for k in knockout_reasons("This role requires U.S. citizenship.", {"us_citizen_or_permanent_resident": "No"})] == ["citizenship"]
    # on-site role vs a remote-only candidate
    assert [k["code"] for k in knockout_reasons("This is a fully on-site position.", {"remote_preference": "Remote only"})] == ["onsite"]
    # nothing stated in the JD → never a warning, even with a restrictive profile (no inference)
    assert knockout_reasons("Great team, great mission. Apply now.", {"requires_sponsorship": "Yes", "us_citizen_or_permanent_resident": "No"}) == []
    # an unset profile fact is never assumed against the candidate
    assert knockout_reasons(jd, {}) == []
