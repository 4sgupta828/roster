"""Open-web person discovery: candidate rows from GitHub/OpenAlex/LinkedIn, hint-gated resolution."""
import asyncio

from api.person_discovery import clarify_web, discover_candidates, rows_from_github, rows_from_linkedin, rows_from_openalex
from api.people_population import resolve_candidates

GH = [{"login": "mukul-g", "type": "User", "name": "Mukul Gupta", "company": "@google", "location": "Mountain View, CA",
       "bio": "Engineer, search infra", "html_url": "https://github.com/mukul-g", "blog": "https://mukul.dev"},
      {"login": "mg1", "type": "User", "name": "Mukul", "html_url": "https://github.com/mg1"}]           # single token → skipped
OA = [{"id": "https://openalex.org/A1", "display_name": "Mukul Gupta", "works_count": 535, "cited_by_count": 9000,
       "last_known_institutions": [{"display_name": "UGC DAE Consortium", "country_code": "IN"}],
       "topics": [{"display_name": "Thin films"}], "orcid": "https://orcid.org/0000-0002-9622-656X"}]
LI = [{"url": "https://www.linkedin.com/in/mukulgupta", "title": "Mukul Gupta - VP Engineering at Acme | LinkedIn", "snippet": ""},
      {"url": "https://www.linkedin.com/in/other", "title": "Someone Else - CEO | LinkedIn", "snippet": ""}]


def test_rows_are_card_shaped_typed_and_keyed():
    g = rows_from_github(GH)
    assert len(g) == 1 and g[0]["entity_id"] == "github:mukul-g" and g[0]["web"] and g[0]["source_label"] == "GitHub"
    assert {a["key"]: a["display"] for a in g[0]["attributes"]}["company"] == "google"
    assert g[0]["evidence"]["strength"] == "self_stated"
    o = rows_from_openalex(OA)
    assert o[0]["entity_id"] == "openalex:A1" and o[0]["evidence"]["strength"] == "structured"
    assert {a["key"]: a["display"] for a in o[0]["attributes"]}["company"] == "UGC DAE Consortium"
    l = rows_from_linkedin("Mukul Gupta", LI)
    assert len(l) == 1 and l[0]["entity_id"] == "linkedin:mukulgupta"
    assert {a["key"]: a["display"] for a in l[0]["attributes"]}["company"] == "Acme"


def test_discovery_then_context_resolves_or_asks():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    async def li(q): return LI
    rows = loop.run_until_complete(discover_candidates("Mukul Gupta", "", gh=lambda n: GH, oa=lambda n: OA, li=li))
    assert [r["entity_id"] for r in rows] == ["github:mukul-g", "openalex:A1", "linkedin:mukulgupta"]
    assert resolve_candidates(rows, "")[0] == "ambiguous"
    assert resolve_candidates(rows, "the one at Google")[1][0]["entity_id"] == "github:mukul-g"
    assert resolve_candidates(rows, "thin films researcher")[1][0]["entity_id"] == "openalex:A1"
    assert resolve_candidates(rows, "Acme")[1][0]["entity_id"] == "linkedin:mukulgupta"
    assert clarify_web("Mukul Gupta", rows).startswith("Which Mukul Gupta? Not in Roster's index yet — 3 possible matches found on GitHub, LinkedIn, OpenAlex")
    # a failing source is silent
    def boom(n): raise RuntimeError("down")
    rows2 = loop.run_until_complete(discover_candidates("Mukul Gupta", "", gh=boom, oa=lambda n: OA, li=li))
    assert [r["entity_id"] for r in rows2] == ["openalex:A1", "linkedin:mukulgupta"]
