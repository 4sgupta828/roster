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


def test_web_footprint_needs_name_plus_hint_and_url_identity():
    from api.person_discovery import footprint_from_results, hint_tokens, identity_from_url, required_hits, url_hint
    assert hint_tokens("the one at Cisco in Bangalore") == ["cisco", "bangalore"]
    assert required_hits("cisco") == 1 and required_hits("cisco bangalore") == 2 and required_hits("cisco bangalore iit delhi") == 2
    res = [{"title": "Mukul Gupta - Cisco Systems | Speaker at DevNet Bangalore", "url": "https://devnet.example.org/speakers/mg",
            "snippet": "Mukul Gupta leads the platform team at Cisco Bangalore."},
           {"title": "Mukul Gupta | Infosys", "url": "https://other.org/x", "snippet": "an Infosys engineer"},        # no hint
           {"title": "Cisco Bangalore office", "url": "https://cisco.com/bangalore", "snippet": "no name here"},
           {"title": "Mukul Gupta - Student - Bangalore University | LinkedIn", "url": "https://www.linkedin.com/in/mukul-gupta-1/",
            "snippet": "Delhi · 257 connections"},                                                                    # 1 of 2 hints → namesake
           {"title": "Mukul Gupta - Engineering Lead at Cisco | LinkedIn", "url": "https://in.linkedin.com/in/mukul-gupta-2/",
            "snippet": "Bangalore Urban, Karnataka · Cisco"}]
    fp = footprint_from_results("Mukul Gupta", "the one at Cisco in Bangalore", res)
    idn = fp["identity"]
    assert idn and idn["entity_id"].startswith("web:mukul-gupta") and idn["web"]
    assert idn["web_hits"] == ["cisco", "bangalore"] and len(idn["web_pages"]) == 1
    assert idn["links"][0]["url"] == "https://devnet.example.org/speakers/mg"
    assert [p["entity_id"] for p in fp["profiles"]] == ["linkedin:mukul-gupta-2"]        # the 1-of-2 profile is out
    assert fp["profiles"][0]["hint_hits"] == ["cisco", "bangalore"] and "Bangalore" in fp["profiles"][0]["blurb"]
    assert footprint_from_results("Mukul Gupta", "cisco", [res[1]])["identity"] is None   # hint never confirmed
    assert footprint_from_results("Mukul Gupta", "", res) == {"profiles": [], "identity": None, "required": 0}
    assert identity_from_url("https://github.com/torvalds") == "github:torvalds"
    assert identity_from_url("https://www.linkedin.com/in/Mukul-G-123/") == "linkedin:mukul-g-123"
    assert identity_from_url("https://example.com/about") == ""
    assert url_hint("he is at https://github.com/torvalds I think") == "https://github.com/torvalds"


def test_lookup_uses_all_hints_web_search_when_keyed_sources_miss(monkeypatch):
    import asyncio
    from api import people_population as pp
    from api import person_discovery as pd
    monkeypatch.setenv("ROSTER_LINKEDIN_RESOLVE", "0")
    monkeypatch.setenv("ROSTER_WEB_DISCOVERY", "1")
    monkeypatch.setenv("ROSTER_TALKS_ENRICH", "0")
    calls = {}
    async def _no_keyed(name, ctx="", **kw):
        return []
    PAGES = [{"title": "Mukul Gupta - Cisco Bangalore", "url": "https://x.org/mg", "snippet": "Mukul Gupta, Cisco Bangalore platform lead"}]
    async def _fp(name, ctx, search=None):
        calls["q"] = (name, ctx)
        return pd.footprint_from_results(name, ctx, PAGES)
    minted = {}
    async def _mint(pool, eid, row):
        minted[eid] = row
        return True
    monkeypatch.setattr(pd, "discover_candidates", _no_keyed)
    monkeypatch.setattr(pd, "web_footprint", _fp)
    monkeypatch.setattr(pd, "_mint", _mint)
    from api.test_person_lookup import _Store
    store = _Store([])
    async def _by_ids(ids, tenant_id="demo"):
        r = minted.get(ids[0])
        return [{"entity_id": ids[0], "name": r["name"], "facets": r["_facets"]}] if r else []
    store.people_by_ids = _by_ids
    async def _pool():
        return None
    store._get_pool = _pool
    out = asyncio.run(pp.lookup_person(store, "Mukul Gupta", "the one at Cisco in Bangalore"))
    lk = out["person_lookup"]
    assert calls["q"] == ("Mukul Gupta", "the one at Cisco in Bangalore")
    assert lk["resolution"] == "resolved" and lk["searched_web"] == '"Mukul Gupta" cisco bangalore'
    assert lk["web_hits"] == ["cisco", "bangalore"]
    assert out["people_rows"][0]["entity_id"].startswith("web:mukul-gupta")
    # a namesake page confirming ONE of two hints resolves nothing — honest 'none' names the query
    PAGES[:] = [{"title": "Mukul Gupta - Bangalore University", "url": "https://x.org/other", "snippet": "student, Delhi"}]
    out2 = asyncio.run(pp.lookup_person(store, "Mukul Gupta", "the one at Cisco in Bangalore"))
    assert out2["person_lookup"]["resolution"] == "none"
    assert "cisco, bangalore" in out2["person_lookup"]["clarify"]
