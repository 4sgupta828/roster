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
