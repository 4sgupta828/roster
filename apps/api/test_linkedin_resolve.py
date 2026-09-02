"""LinkedIn resolution from search snippets (never reads linkedin.com) + self-stated calibration."""
from __future__ import annotations

import asyncio

from api.evidence import calibrate, evidence_packet
from api.linkedin_resolve import (build_query, choose, hint_hits, hints_from_row, name_matches,
                                  parse_title, resolve_linkedin)

R_ANTHROPIC = {"url": "https://www.linkedin.com/in/nottombrown", "title": "Tom Brown - Co-Founder at Anthropic | LinkedIn",
               "snippet": "Tom Brown. Co-Founder at Anthropic. San Francisco Bay Area."}
R_SF = {"url": "https://www.linkedin.com/in/tom-brown-226b191a0", "title": "Tom Brown - San Francisco, California, United States - LinkedIn",
        "snippet": "Tom Brown. Student at UC Berkeley."}
R_TMBRWN = {"url": "https://uk.linkedin.com/in/t0mbrown", "title": "Tom Brown - Creative Director at TMBRWN | LinkedIn", "snippet": ""}
R_TOMER = {"url": "https://www.linkedin.com/in/tomerbrown", "title": "Tomer Brown - Software Engineer at Google | LinkedIn", "snippet": ""}
R_OTHER = {"url": "https://www.linkedin.com/company/anthropic", "title": "Anthropic | LinkedIn", "snippet": ""}


def test_title_parsing_and_name_matching():
    assert parse_title(R_ANTHROPIC["title"]) == ("Tom Brown", "Co-Founder at Anthropic")
    assert parse_title(R_SF["title"]) == ("Tom Brown", "San Francisco, California, United States")
    assert parse_title(".Chris Albon - Wikimedia Foundation | LinkedIn") == ("Chris Albon", "Wikimedia Foundation")
    assert name_matches("Tom Brown", "Tom B. Brown") and name_matches("tom brown", "Tom Brown")
    assert not name_matches("Tom Brown", "Tomer Brown") and not name_matches("Tom Brown", "Tom")


def test_choose_gates_on_name_and_company():
    hints = {"company": ["Anthropic"], "role": ["Researcher"]}
    d = choose("Tom Brown", hints, [R_SF, R_ANTHROPIC, R_TMBRWN, R_TOMER, R_OTHER])
    assert d["status"] == "resolved" and d["match"]["url"] == "https://www.linkedin.com/in/nottombrown"
    assert d["match"]["headline"] == "Co-Founder at Anthropic" and d["match"]["hits"] == {"company": ["Anthropic"]}
    assert all(c["name"] != "Tomer Brown" for c in d["candidates"])          # name gate
    # a company we hold that NO name-matching profile mentions → never a guess
    d2 = choose("Tom Brown", {"company": ["SolidStage"]}, [R_SF, R_ANTHROPIC, R_TMBRWN])
    assert d2["status"] == "ambiguous" and d2["match"] is None and len(d2["candidates"]) == 3
    # no hints at all and several same-named profiles → ambiguous; none matching → none
    assert choose("Tom Brown", {}, [R_SF, R_ANTHROPIC])["status"] == "ambiguous"
    assert choose("Ada Byte", {"company": ["Acme"]}, [R_SF, R_ANTHROPIC])["status"] == "none"
    # metro-only hint can resolve when there is no company on file and one profile carries it
    d3 = choose("Tom Brown", {"metro": ["San Francisco"]}, [R_SF, R_TMBRWN])
    assert d3["status"] == "resolved" and d3["match"]["url"].endswith("226b191a0")


def test_hints_and_query_come_from_grounded_attributes():
    row = {"name": "Tom Brown", "attributes": [{"key": "company", "display": "anthropic"},
                                                {"key": "worked_at", "display": "OpenAI"},
                                                {"key": "metro", "display": "bay_area"},
                                                {"key": "skill", "display": "python"}]}
    h = hints_from_row(row)
    assert h == {"company": ["anthropic"], "worked_at": ["OpenAI"], "metro": ["bay area"]}
    assert build_query("Tom Brown", h) == '"Tom Brown" anthropic site:linkedin.com/in'
    assert build_query("Tom B Brown", h) == '"Tom Brown" anthropic site:linkedin.com/in'      # initials dropped
    assert build_query("Tom B. Brown", {}) == '"Tom Brown" site:linkedin.com/in'
    assert build_query("Mary Jane Watson", {}) == '"Mary Watson" site:linkedin.com/in'        # first + last (recall)
    assert hint_hits(h, "Co-Founder at Anthropic, previously OpenAI") == {"company": ["anthropic"], "worked_at": ["OpenAI"]}


def test_resolve_linkedin_uses_search_and_frames_evidence():
    row = {"name": "Tom Brown", "attributes": [{"key": "company", "display": "Anthropic"}]}
    seen = {}

    async def fake_search(q):
        seen["q"] = q
        return [R_SF, R_ANTHROPIC]

    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    d = loop.run_until_complete(resolve_linkedin(row, search=fake_search))
    assert seen["q"] == '"Tom Brown" Anthropic site:linkedin.com/in'
    assert d["status"] == "resolved" and d["evidence"]["type"] == "self_stated" and d["evidence"]["family"] == "linkedin"
    one = loop.run_until_complete(resolve_linkedin({"name": "Prince"}, search=fake_search))
    assert one["status"] == "none"                                              # single-token names never resolve

    async def blocked(q):
        return []                                                               # anti-bot page / rate limit
    ua = loop.run_until_complete(resolve_linkedin(row, search=blocked))
    assert ua["status"] == "unavailable" and ua["match"] is None                # never reads as 'no profile'


def test_calibration_bands_and_consistency_vs_corroboration():
    # GitHub bio + LinkedIn headline agree on company → CONSISTENT (both self-authored), not corroborated
    pk = evidence_packet([
        {"facet_key": "company", "value_norm": "anthropic", "document_id": "https://github.com/x"},
        {"facet_key": "company", "value_norm": "anthropic", "document_id": "https://www.linkedin.com/in/x"},
        {"facet_key": "linkedin_headline", "value_norm": "co founder at anthropic", "document_id": "https://www.linkedin.com/in/x"},
    ], "github:x")
    assert pk["consistent_keys"] == ["company"] and pk["corroborated_keys"] == []
    assert pk["per_key"]["company"]["type"] == "self_stated" and pk["strength"] == "self_stated"
    row = {"attributes": [{"key": "company", "display": "Anthropic"}], "artifacts": {"affiliations": [], "items": []}}
    calibrate(pk, row)
    assert pk["calibration"]["band"] == "consistent"
    assert any("consistently across independent self-authored profiles" in r for r in pk["calibration"]["reasons"])
    assert any("matched on name + company" in r for r in pk["calibration"]["reasons"])
    # a registry agreeing with a bio stays CORROBORATED
    pk2 = evidence_packet([
        {"facet_key": "company", "value_norm": "anthropic", "document_id": "https://github.com/y"},
        {"facet_key": "company", "value_norm": "anthropic", "document_id": "https://theorg.com/o"},
    ], "github:y")
    assert pk2["corroborated_keys"] == ["company"] and pk2["consistent_keys"] == []
    # self-stated only, artifacts agree on affiliation → consistent with an artifact reason
    pk3 = evidence_packet([{"facet_key": "company", "value_norm": "anthropic", "document_id": "https://github.com/z"}], "github:z")
    calibrate(pk3, {"attributes": [{"key": "company", "display": "Anthropic"}],
                    "artifacts": {"affiliations": [{"name": "Anthropic PBC", "n": 3, "years": [2024]}],
                                  "items": [{"kind": "org", "title": "anthropics"}]}})
    assert pk3["calibration"]["band"] == "consistent"
    assert pk3["calibration"]["reasons"][0].startswith("3 published works carry an affiliation")
    assert any("GitHub org membership" in r for r in pk3["calibration"]["reasons"])
    # self-stated with nothing else → uncorroborated; structured-only → n/a
    pk4 = evidence_packet([{"facet_key": "company", "value_norm": "acme", "document_id": "https://github.com/w"}], "github:w")
    calibrate(pk4, {"attributes": [{"key": "company", "display": "Acme"}]})
    assert pk4["calibration"] == {"band": "uncorroborated", "reasons": []}
    pk5 = evidence_packet([{"facet_key": "company", "value_norm": "acme", "document_id": "https://openalex.org/A1"}], "openalex:A1")
    calibrate(pk5, {"attributes": []})
    assert pk5["calibration"]["band"] == "n/a"


def test_persist_writes_link_headline_and_company_facets_from_the_linkedin_family():
    from api.linkedin_resolve import persist_resolution

    class _Rec:
        def __init__(self):
            self.calls = []

        async def add_person_facet(self, **kw):
            self.calls.append(kw)

    st = _Rec()
    match = {"url": "https://www.linkedin.com/in/nottombrown", "headline": "Co-Founder at Anthropic",
             "hits": {"company": ["anthropic"]}}
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    loop.run_until_complete(persist_resolution(st, "github:nottombrown", match))
    keys = [c["facet_key"] for c in st.calls]
    assert keys == ["link_linkedin", "linkedin_headline", "linkedin_company"]   # own key: never overwrites the GitHub row
    assert all(c["source_document_id"] == match["url"] for c in st.calls)         # family 'linkedin'
    assert st.calls[2]["facet_value_norm"] == "anthropic"                          # ingester's norm
    # the packet then sees github + linkedin agreeing on company → consistent, not corroborated
    pk = evidence_packet([
        {"facet_key": "company", "value_norm": "anthropic", "document_id": "https://github.com/nottombrown"},
        {"facet_key": "linkedin_company", "value_norm": "anthropic", "document_id": match["url"]},
    ], "github:nottombrown")
    assert pk["consistent_keys"] == ["company"] and pk["corroborated_keys"] == []   # claim-axis mapping


def test_enrich_cohort_reads_snippets_once_and_scores_rank(monkeypatch):
    """Cohort enrichment: each unscanned person gets ONE search; outcomes are remembered (a second
    call makes no search); resolved people gain self-stated facets; rows carry a rank read."""
    from api.linkedin_resolve import enrich_cohort
    from api.evidence import rank_read
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)          # no embeddings → no headline fit

    def _fp(eid, name, **facets):
        rows = [{"facet_key": k, "display_value": v, "value_norm": v.lower().replace(" ", "_"),
                 "document_id": "https://github.com/" + eid.split(":", 1)[1], "block_id": ""}
                for k, v in facets.items()]
        return {"entity_id": eid, "name": name, "facets": rows}

    class _St:
        def __init__(self):
            self.people = {"github:tb": _fp("github:tb", "Tom Brown", company="Anthropic"),
                           "github:ab": _fp("github:ab", "Ada Byte", company="Acme")}
            self.scans, self.facets, self.searches = {}, [], []

        async def people_by_ids(self, ids, *, tenant_id="demo"):
            return [self.people[i] for i in ids if i in self.people]

        async def linkedin_scans(self, ids):
            return {i: self.scans[i] for i in ids if i in self.scans}

        async def linkedin_scans_today(self):
            return len(self.scans)

        async def record_linkedin_scan(self, eid, status, *, url="", headline=""):
            self.scans[eid] = {"entity_id": eid, "status": status, "url": url, "headline": headline}

        async def add_person_facet(self, **kw):
            self.facets.append(kw)
            self.people[kw["entity_id"]]["facets"].append(
                {"facet_key": kw["facet_key"], "display_value": kw["display_value"],
                 "value_norm": kw["facet_value_norm"], "document_id": kw["source_document_id"], "block_id": ""})

    st = _St()

    async def search(q):
        st.searches.append(q)
        return [R_ANTHROPIC, R_SF] if "Tom Brown" in q else []

    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    res = loop.run_until_complete(enrich_cohort(st, ["github:tb", "github:ab", "missing:x"], "LLM training",
                                                qps=1000, search=search))
    assert res["skipped"] == ["missing:x"] and len(st.searches) == 2
    tb, ab = res["rows"]
    assert tb["linkedin"]["status"] == "resolved" and tb["linkedin"]["headline"] == "Co-Founder at Anthropic"
    assert any(l["kind"] == "linkedin" for l in tb["links"])                    # facet persisted + row rebuilt
    assert tb["evidence"]["consistent_keys"] == ["company"]                     # GitHub + LinkedIn agree
    assert ab["linkedin"]["status"] == "unavailable"                            # empty leg ≠ absence; not recorded
    assert st.scans["github:tb"]["status"] == "resolved" and "github:ab" not in st.scans
    assert tb["rank_read"]["score"] >= ab["rank_read"]["score"]
    assert any("consistent" in r for r in tb["rank_read"]["reasons"])
    # second call: Tom is remembered (no new search); Ada is retried (unavailable was never recorded)
    res2 = loop.run_until_complete(enrich_cohort(st, ["github:tb", "github:ab"], "LLM training", qps=1000, search=search))
    assert len(st.searches) == 3 and res2["rows"][0]["linkedin"]["status"] == "resolved"
    # quota: cap reached → 'quota', no search
    res3 = loop.run_until_complete(enrich_cohort(st, ["github:ab"], "x", qps=1000, daily_cap=1, search=search))
    assert res3["rows"][0]["linkedin"]["status"] == "quota" and len(st.searches) == 3


def test_rank_read_bands_then_evidence():
    from api.evidence import rank_read, rank_sort_key
    base = {"match_pct": 70, "evidence": {"types": ["self_stated"], "corroborated_keys": [], "consistent_keys": []},
            "artifacts": {"scanned": ["github"], "total": 0, "counts": {}, "newest": None}}
    rich = {"match_pct": 68, "evidence": {"types": ["artifact_backed", "self_stated"], "corroborated_keys": ["company"]},
            "artifacts": {"scanned": ["github"], "total": 40, "counts": {"repo": 40}, "newest": "2026-01-01",
                          "items": [{"kind": "repo", "title": "vecdb-rs", "venue": "Rust", "stat": "120★"}]},
            "linkedin": {"headline_fit": 0.8}}
    b, r = rank_read(base), rank_read(rich)
    base["rank_read"], rich["rank_read"] = b, r
    assert b["band"] == 14 and r["band"] == 13                        # 0.70 vs 0.68 → different bands
    assert b["score"] > r["score"] and rank_sort_key(base) < rank_sort_key(rich)   # band wins over evidence
    assert "scanned: no public artifacts found" in b["reasons"] and b["within"] == 0.0
    same = rank_read({**rich, "match_pct": 70})                       # same band → evidence decides
    assert same["score"] > b["score"] and same["score"] < 15 * 0.05   # and never crosses into the next band
    # brief-aware artifact boost + seniority from evidence when the brief asks for senior people
    br = rank_read({**rich, "match_pct": 70,
                    "artifacts": {**rich["artifacts"], "items": [
                        {"kind": "repo", "title": "vector-db-rs", "venue": "Rust", "stat": "120★"},
                        {"kind": "paper", "title": "Vector database infrastructure at scale", "venue": "VLDB",
                         "role": "first_author", "stat": "340 citations"}]}},
                   {"skill": ["vector_db"], "function": ["infrastructure"], "seniority": ["senior"]})
    assert any(x.startswith("2 artifacts match the brief") for x in br["reasons"])
    assert any(x.startswith("seniority evidence: 1 first-author paper, 340 citations, 1 starred repo") for x in br["reasons"])
    assert br["within"] > same["within"]
    # unscanned is neutral: no penalty, no footprint claim
    un = rank_read({"match_pct": 70, "evidence": {"types": ["self_stated"]}, "artifacts": {"scanned": [], "total": 0}})
    assert un["within"] == 0.0 and not any("scanned" in x for x in un["reasons"])
