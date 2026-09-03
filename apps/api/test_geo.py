"""Local scope: nearest metro, person/job locality checks (unknown is kept, elsewhere dropped)."""
from __future__ import annotations

from api.geo import (job_geo_status, nearest_us_metro, partition_local, person_geo_status, resolve_scope,
                     scope_statement)


def test_nearest_metro_and_scope_resolution():
    assert nearest_us_metro(37.77, -122.42)[0] == "bay_area"          # San Francisco
    assert nearest_us_metro(37.39, -122.08)[0] == "bay_area"          # Mountain View
    assert nearest_us_metro(47.62, -122.35)[0] == "seattle"
    assert nearest_us_metro(44.06, -121.31)[0] == ""                  # Bend, OR: no metro within 120 km
    s = resolve_scope(lat=37.44, lon=-122.14)
    assert s["metro"] == "bay_area" and s["state"] == "ca" and s["metro_label"] == "Bay Area" and s["source"] == "browser"
    assert resolve_scope(ip="10.0.0.1")["source"] == ""              # private IP: nothing


def _f(**kv):
    return [{"facet_key": k, "value_norm": v} for k, v in kv.items()]


def test_person_locality_keeps_unknown_and_drops_elsewhere():
    m = dict(metro="bay_area")
    assert person_geo_status(_f(metro="san_francisco"), **m) == "in"          # alias → canonical
    assert person_geo_status(_f(metro="bay_area", country="us"), **m) == "in"
    assert person_geo_status(_f(metro="seattle"), **m) == "out"
    assert person_geo_status(_f(state="wa"), **m) == "out"
    assert person_geo_status(_f(state="ca"), **m) == "unknown"                # right state, no metro
    assert person_geo_status(_f(country="us"), **m) == "unknown"
    assert person_geo_status(_f(country="de"), **m) == "out"
    assert person_geo_status(_f(metro="berlin"), **m) == "out"                # metro implies country
    assert person_geo_status([], **m) == "unknown"
    s = dict(state="ca")
    assert person_geo_status(_f(metro="los_angeles"), **s) == "in"
    assert person_geo_status(_f(state="ca"), **s) == "in"
    assert person_geo_status(_f(metro="nyc"), **s) == "out"
    assert person_geo_status(_f(role="x"), **s) == "unknown"
    assert person_geo_status(_f(metro="nyc")) == "in"                         # no scope: everything is in


def test_job_locality():
    m = dict(metro="bay_area")
    assert job_geo_status("San Francisco, CA", **m) == "in"
    assert job_geo_status("Mountain View, California", **m) == "in"
    assert job_geo_status("Seattle, WA", **m) == "out"
    assert job_geo_status("Austin, TX", **m) == "out"
    assert job_geo_status("Remote - US", **m) == "remote"
    assert job_geo_status("", **m) == "unknown"
    assert job_geo_status("London, United Kingdom", **m) == "out"
    assert job_geo_status("Berlin", **m) == "out"
    s = dict(state="ca")
    assert job_geo_status("Los Angeles, CA", **s) == "in" and job_geo_status("New York, NY", **s) == "out"
    assert job_geo_status("California", **s) == "in"


def test_partition_and_statement():
    jobs = [{"location": "Seattle, WA"}, {"location": "Remote"}, {"location": ""}, {"location": "Palo Alto, CA"}]
    rows, c = partition_local(jobs, lambda j: job_geo_status(j["location"], metro="bay_area"))
    assert [r["location"] for r in rows] == ["Palo Alto, CA", "Remote", ""] and c == {"in": 1, "remote": 1, "unknown": 1, "out": 1}
    st = scope_statement("jobs", "bay_area", "", c)
    assert st.startswith("1 roles located there lead") and "Expand to California or all US" in st
    assert scope_statement("people", "", "wa", {"in": 3, "unknown": 2, "out": 1}).startswith("3 people placed there lead")
    assert scope_statement("people", "", "", {}) == ""


def test_apply_job_scope_leads_local_keeps_remote_and_respects_query_location():
    from api.people_population import apply_job_scope
    jobs = [{"location": "Berlin, Germany"}, {"location": "Seattle, WA"}, {"location": "Remote"},
            {"location": "San Jose, CA"}, {"location": ""}]
    rows, gs = apply_job_scope(jobs, country="us", metro="bay_area")
    assert [r["location"] for r in rows] == ["San Jose, CA", "Remote", ""]     # Berlin (country) + Seattle (metro) out
    assert rows[0].get("local") is True and gs["label"] == "Bay Area" and gs["counts"]["in"] == 1
    assert gs["state"] == "ca" and gs["state_label"] == "California" and "1 roles located there lead" in gs["statement"]
    rows2, gs2 = apply_job_scope(jobs, country="us", metro="bay_area", query_location="Seattle")
    assert gs2 is None and len(rows2) == 5                                     # the query's own place wins
    rows3, gs3 = apply_job_scope(jobs, country="de", metro="bay_area")
    assert gs3 is None and [r["location"] for r in rows3] == ["Berlin, Germany", "Remote", ""]   # non-US country: no metro scope


def test_location_regex_matches_scope_only():
    import re
    from api.geo import location_regex
    rx = re.compile(location_regex("bay_area"), re.I)
    assert rx.search("San Francisco, CA") and rx.search("Mountain View, California") and rx.search("SF Bay Area")
    assert not rx.search("Seattle, WA") and not rx.search("Remote - US")
    rs = re.compile(location_regex(state="ca"), re.I)
    assert rs.search("Los Angeles, CA") and rs.search("Sacramento, California")
    assert not rs.search("Toronto, Canada") and not rs.search("New York, NY")
    assert location_regex() == ""


def test_apply_job_must_requires_every_selected_kind():
    import asyncio
    from api.people_population import apply_job_must
    class _S:
        async def companies_with_facet(self, keys, values=None):
            return {"tubi"} if "accelerator" in keys else set()
    rows = [{"company": "Google", "title": "Senior Software Engineer", "location": "Remote - US"},
            {"company": "tubi", "title": "Staff Engineer", "location": "San Francisco, CA (Hybrid)"},
            {"company": "Google", "title": "Software Engineer II", "location": "Mountain View, CA"},
            {"company": "Acme", "title": "Director of Engineering", "location": "Remote"}]
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    run = lambda m: loop.run_until_complete(apply_job_must(_S(), rows, m))
    assert run([]) == (rows, None)
    kept, must = run(["remote"])
    assert [r["company"] for r in kept] == ["Google", "Acme"] and must["kept"] == 2 and must["dropped"] == 2
    assert [r["title"] for r in run(["hybrid"])[0]] == ["Staff Engineer"]
    assert [r["title"] for r in run(["f500"])[0]] == ["Senior Software Engineer", "Software Engineer II"]
    assert [r["company"] for r in run(["startup"])[0]] == ["tubi"]
    assert run(["remote", "senior"])[1]["kinds"] == ["remote"]          # 'senior' is not a must-have kind
    assert [r["title"] for r in run(["leadership"])[0]] == ["Director of Engineering"]
    assert [r["title"] for r in run(["f500", "remote"])[0]] == ["Senior Software Engineer"]
    assert run(["bogus"])[1] is None                    # unknown kinds are ignored, not enforced
    assert run(["public"])[1]["labels"] == ["Public company"]


def test_fit_score_is_code_computed_and_deterministic():
    from api.people_population import fit_score_from_requirements
    reqs = [{"requirement": "Python", "importance": "must", "verdict": "strong"},
            {"requirement": "Go", "importance": "must", "verdict": "partial"},
            {"requirement": "Fraud domain", "importance": "must", "verdict": "gap"},
            {"requirement": "Kubernetes", "importance": "nice", "verdict": "strong"}]
    score, basis = fit_score_from_requirements(reqs)
    assert score == 57                                   # (2*1 + 2*.5 + 0 + 1*1) / (2+2+2+1) = 4/7
    assert basis == {"must": {"strong": 1, "partial": 1, "gap": 1}, "nice": {"strong": 1, "partial": 0, "gap": 0}}
    assert fit_score_from_requirements(list(reversed(reqs)))[0] == 57      # order-independent
    assert fit_score_from_requirements([]) == (0, {"must": {"strong": 0, "partial": 0, "gap": 0}, "nice": {"strong": 0, "partial": 0, "gap": 0}})
    assert fit_score_from_requirements([{"requirement": "x", "importance": "??", "verdict": "??"}])[0] == 0   # unknowns → must/gap


def test_grade_requirements_gates_on_verbatim_resume_quotes():
    from api.people_population import grade_requirements, quote_in_text
    resume = "Senior software engineer, 10 years building distributed systems in Python and Go at Google and Stripe.\nLed the payments infrastructure team."
    reqs = [{"requirement": "Python", "importance": "must"}, {"requirement": "Fraud domain", "importance": "must"},
            {"requirement": "Go", "importance": "nice"}]
    grades = [{"index": 0, "verdict": "strong", "evidence_quote": "distributed systems in Python and Go", "evidence": "Python at scale"},
              {"index": 1, "verdict": "strong", "evidence_quote": "built fraud models for banks", "evidence": "fraud"},   # not in résumé → gap
              {"index": 2, "verdict": "partial", "evidence_quote": "Go", "evidence": "Go"}]                                   # too short → gap
    out = grade_requirements(reqs, grades, resume)
    assert [r["verdict"] for r in out] == ["strong", "gap", "gap"]
    assert out[0]["verified"] and out[0]["evidence"] == "Python at scale"
    assert out[1]["evidence"] == "" and out[1]["evidence_quote"] == ""
    assert quote_in_text("LED THE PAYMENTS   infrastructure team", resume)      # case / whitespace normalized
    assert not quote_in_text("payments team", resume)                           # < 4 words never proves anything
    # a missing grade is a gap; order follows the requirements list
    assert [r["verdict"] for r in grade_requirements(reqs, [], resume)] == ["gap", "gap", "gap"]


def test_jd_match_uses_scope_keys_for_locations_and_skill_boost(monkeypatch):
    """Explicit locations are metro keys / state codes: people placed in any chosen scope lead and are
    boosted; clearly-elsewhere people are dropped; unknown-location people stay."""
    import asyncio
    from api import people_population as pp
    facets = lambda **kw: [{"facet_key": k, "value_norm": v, "display_value": v, "document_id": "d", "block_id": ""} for k, v in kw.items()]
    people = {"a": facets(name="A", metro="bay_area", country="us", skill="kubernetes"),
              "b": facets(name="B", metro="austin", country="us"),
              "c": facets(name="C", country="us"),
              "d": facets(name="D", metro="seattle", country="us")}
    class _S:
        async def match_people_scored(self, qvec, cap=400):
            return [{"entity_id": k, "sim": 0.6} for k in people]
        async def people_by_ids(self, ids, tenant_id="demo"):
            return [{"entity_id": i, "name": i.upper(), "facets": people[i]} for i in ids]
        async def _get_pool(self):
            return None
    monkeypatch.setattr(pp, "embed_query", lambda t: "[0.1]")
    async def _attach(store, rows):
        return None
    monkeypatch.setattr(pp, "attach_artifacts", _attach, raising=False)
    import api.artifacts as art
    monkeypatch.setattr(art, "attach_artifacts", _attach)
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    res = loop.run_until_complete(pp.match_jd_people(_S(), "Senior platform engineer with Kubernetes experience, 8+ years.",
                                                     {"locations": ["bay_area", "tx"], "skills": ["kubernetes"], "country": "us"}))
    ids = [r["entity_id"] for r in res["people_rows"]]
    assert ids[:2] == ["a", "b"]                      # in Bay Area / Texas lead (a boosted by skill too)
    assert "c" in ids and "d" not in ids              # unknown kept; Seattle dropped
    a = res["people_rows"][0]
    assert "location" in a["reasons"] and any(x.startswith("skills:") for x in a["reasons"])
    assert res["geo_scope"]["source"] == "chosen" and "Bay Area" in res["geo_scope"]["label"]


def test_job_summary_verification_blanks_what_the_posting_does_not_state():
    from api.people_population import verify_job_summary
    jd = "Senior Backend Engineer at Acme. Hybrid, 3 days in our Austin office. Base salary $180,000 – $220,000 plus equity. Full-time."
    s = verify_job_summary({"work_mode": "hybrid", "compensation": "$180,000–$220,000 base + equity", "employment_type": "full-time",
                            "key_requirements": ["Go", "Postgres", "", "x"] * 3, "title": "Senior Backend Engineer"}, jd)
    assert s["work_mode"] == "hybrid" and s["compensation"].startswith("$180,000") and s["employment_type"] == "full-time"
    assert len(s["key_requirements"]) == 6
    # the posting says nothing about pay or remote → the model's guesses are blanked
    s2 = verify_job_summary({"work_mode": "remote", "compensation": "$150k-$200k", "employment_type": "contract"},
                            "Backend Engineer at Acme in Austin. Build services in Go.")
    assert s2["work_mode"] == "" and s2["compensation"] == "" and s2["employment_type"] == ""
    # a pay figure that is not the posting's figure is blanked too
    s3 = verify_job_summary({"compensation": "$300,000"}, jd)
    assert s3["compensation"] == ""


def test_build_job_summary_returns_the_verified_read():
    import asyncio
    from api.people_population import _JobSummary, build_job_summary
    class _Comp:
        parsed = _JobSummary(title="Backend Engineer", company="Acme", location="Austin, TX", work_mode="hybrid",
                             compensation="$180,000 – $220,000", one_liner="Own the payments API.", key_requirements=["Go", "Postgres"])
    class _LLM:
        async def complete(self, **kw):
            return _Comp()
    jd = "Backend Engineer at Acme, Austin, TX. Hybrid. Base $180,000 – $220,000. Own the payments API. Requirements: Go, Postgres. " * 3
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    out = loop.run_until_complete(build_job_summary(jd, _LLM(), company="Acme"))
    assert out and out["one_liner"] == "Own the payments API." and out["work_mode"] == "hybrid" and out["compensation"].startswith("$180,000")
    class _Empty:
        parsed = _JobSummary()
    class _LLM2:
        async def complete(self, **kw):
            return _Empty()
    assert loop.run_until_complete(build_job_summary(jd, _LLM2())) is None      # nothing read → None, never cached
