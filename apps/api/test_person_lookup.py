"""Person lookup in People mode: everything the index holds on a named person, on demand — or a
clarifying question when several people share the name. Code-owned resolution, never a merge."""
from __future__ import annotations

import asyncio

from api.people_population import _clarify_text, _distinguisher, lookup_person, resolve_candidates


def _row(eid, name, company="", metro="", role="", blurb=""):
    attrs = [{"key": k, "display": v} for k, v in (("company", company), ("metro", metro), ("role", role)) if v]
    return {"entity_id": eid, "name": name, "attributes": attrs, "links": [], "blurb": blurb}


A = _row("openalex:A1", "Tom Brown", company="Anthropic", role="Researcher", metro="London")
B = _row("yc:35419", "Tom Brown", company="SolidStage", role="Founder")
C = _row("github:tb", "Tom Brown", metro="Austin", blurb="Rails developer in Austin, formerly IBM")


def test_resolution_states():
    assert resolve_candidates([], "") == ("none", [])
    assert resolve_candidates([A], "") == ("resolved", [A])
    assert resolve_candidates([A, B, C], "")[0] == "ambiguous"
    assert resolve_candidates([A, B, C], "the one at Anthropic") == ("resolved", [A])
    assert resolve_candidates([A, B, C], "IBM guy") == ("resolved", [C])
    assert resolve_candidates([A, B, C], "the founder") == ("resolved", [B])
    # no candidate matches the hint → still ambiguous, all kept (never a guess)
    assert resolve_candidates([A, B, C], "works at Google")[0] == "ambiguous"
    # stop-words alone carry no signal
    assert resolve_candidates([A, B, C], "the one who works at the company")[0] == "ambiguous"


def test_distinguisher_and_clarify_are_grounded_labels():
    assert _distinguisher(A) == "Anthropic · Researcher · London · via OpenAlex scholarly registry (derived from published works)"
    assert _distinguisher(C).startswith("Austin · via the person's own GitHub profile")
    t = _clarify_text("Tom Brown", [A, B, C], "ambiguous")
    assert t.startswith("Which Tom Brown? 3 people") and "Tom Brown at Anthropic" in t
    assert "not in Roster's people index yet" in _clarify_text("Mukul Gupta", [], "none")
    assert _clarify_text("Tom Brown", [A], "resolved") == ""


class _Store:
    """Index stub: people_by_name / people_by_ids return facet-row shapes; no pool (no artifacts)."""
    def __init__(self, people):
        self._people = people

    async def people_by_name(self, name, *, tenant_id="demo", limit=12):
        return [p for p in self._people if p["name"].lower() == name.lower()][:limit]

    async def people_by_ids(self, ids, *, tenant_id="demo"):
        return [p for p in self._people if p["entity_id"] in set(ids)]


def _facet_person(eid, name, **facets):
    rows = [{"facet_key": k, "display_value": v, "value_norm": v.lower().replace(" ", "_"),
             "document_id": "https://github.com/" + eid.split(":", 1)[1], "block_id": ""}
            for k, v in facets.items()]
    return {"entity_id": eid, "name": name, "facets": rows}


def _run(coro):
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_lookup_person_resolves_clarifies_and_handles_absence(monkeypatch):
    monkeypatch.setenv("ROSTER_LINKEDIN_RESOLVE", "0")     # no network in unit tests
    monkeypatch.setenv("ROSTER_WEB_DISCOVERY", "0")        # open-web discovery is tested with fakes elsewhere
    store = _Store([_facet_person("github:a", "Mukul Gupta", company="Google", metro="Bay Area"),
                    _facet_person("github:b", "Mukul Gupta", company="IIT Delhi", role="Researcher")])
    amb = _run(lookup_person(store, "Mukul Gupta"))
    assert amb["kind"] == "person" and amb["person_lookup"]["resolution"] == "ambiguous"
    assert len(amb["people_rows"]) == 2 and amb["person_lookup"]["clarify"].startswith("Which Mukul Gupta? 2 people")
    assert [c["entity_id"] for c in amb["person_lookup"]["candidates"]] == ["github:a", "github:b"]
    assert all(r.get("distinguisher") for r in amb["people_rows"])
    res = _run(lookup_person(store, "Mukul Gupta", "the one at Google"))
    assert res["person_lookup"]["resolution"] == "resolved" and res["people_rows"][0]["entity_id"] == "github:a"
    assert res["people_rows"][0]["evidence"]["strength"] == "self_stated"      # typed, never 'verified'
    picked = _run(lookup_person(store, "github:b"))                            # a picked candidate
    assert picked["person_lookup"]["resolution"] == "resolved" and picked["person_lookup"]["name"] == "Mukul Gupta"
    none = _run(lookup_person(store, "Nobody Here"))
    assert none["person_lookup"]["resolution"] == "none" and none["people_rows"] == []
    assert none["person_card"]["links"][0]["kind"] == "github_search"          # explicit search links remain


def test_people_surface_person_lookup_end_to_end(monkeypatch):
    """People tab + a person name: the index is searched (no research, no router call); several
    same-named people → clarify + candidates; the next utterance with prior_person resolves it."""
    from fastapi.testclient import TestClient
    from roster_kernel.providers.llm import LLMResult
    import api.app as appmod
    from api.app import create_app
    from api.people_population import _FacetParse
    from api.test_qa_native import _AskSpy

    class _NameLLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            assert response_format is _FacetParse
            txt = messages[-1]["content"] if messages else ""
            return LLMResult(parsed=_FacetParse(person="Tom Brown",
                                                person_context=("Anthropic" if "Anthropic" in txt else "")),
                             output_tokens=5)

    people = [_facet_person("openalex:A1", "Tom Brown", company="Anthropic", role="Researcher"),
              _facet_person("yc:1", "Tom Brown", company="SolidStage", role="Founder")]

    class _AppStore(_Store):
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 2, "source_documents": 2, "facet_coverage": {}}

    svc = _AskSpy(answer="MUST NOT RUN")
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1"); monkeypatch.setenv("ROSTER_PEOPLE_POPULATION", "1")
    monkeypatch.setenv("ROSTER_LINKEDIN_RESOLVE", "0")     # no network: the snippet leg is not under test here
    monkeypatch.setenv("ROSTER_WEB_DISCOVERY", "0")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _NameLLM())
    app = create_app(svc); app.state.claim_store = _AppStore(people)
    client = TestClient(app)
    r = client.post("/research", json={"question": "Tom Brown", "tenant_id": "demo", "surface": "people"}).json()
    assert svc.calls == [] and r["answer"] == "" and r.get("redirect_to_qa") is not True
    assert r["person_lookup"]["resolution"] == "ambiguous" and len(r["people_rows"]) == 2
    assert r["person_lookup"]["clarify"].startswith("Which Tom Brown? 2 people")
    r2 = client.post("/research", json={"question": "the one at Anthropic", "tenant_id": "demo",
                                        "surface": "people", "prior_person": "Tom Brown"}).json()
    assert r2["person_lookup"]["resolution"] == "resolved" and r2["people_rows"][0]["entity_id"] == "openalex:A1"
    r3 = client.post("/research", json={"question": "Tom Brown — SolidStage · Founder [yc:1]",
                                        "tenant_id": "demo", "surface": "people",
                                        "prior_person": "Tom Brown"}).json()          # a picked card
    assert r3["person_lookup"]["resolution"] == "resolved" and r3["people_rows"][0]["entity_id"] == "yc:1"
    assert svc.calls == []


def test_topic_partition_leads_with_people_who_mention_the_subject():
    from api.people_population import topic_hit, topic_partition
    a = {"name": "A", "blurb": "Engineering manager, backend", "attributes": [{"key": "function", "display": "Backend"}]}
    b = {"name": "B", "blurb": "Led the ad serving platform at X", "attributes": []}
    c = {"name": "C", "blurb": "", "attributes": [{"key": "function", "display": "Advertising"}],
         "artifacts": {"items": [{"title": "rtb-bidder"}]}}
    terms = ["ad serving", "adtech", "advertising", "real-time bidding"]
    assert topic_hit(b, terms) == "ad serving" and topic_hit(c, terms) == "advertising" and topic_hit(a, terms) == ""
    rows, n = topic_partition([a, b, c], terms)
    assert [r["name"] for r in rows] == ["B", "C", "A"] and n == 2                # stable, anchored first
    assert rows[0]["topic_hit"] == "ad serving" and "topic_hit" not in a
    assert topic_partition([a], [])[1] == 0                                     # no terms: untouched


def test_topic_terms_fall_back_to_compiled_facets():
    from api.people_population import topic_terms_for
    assert topic_terms_for({"skill": ["ad_serving"], "function": ["engineering"]}, ["ad serving", "adtech"]) == ["ad serving", "adtech"]
    assert topic_terms_for({"skill": ["ad_serving", "kafka"], "function": ["infrastructure", "advertising"]}, []) == ["ad serving", "kafka", "advertising"]
    assert topic_terms_for({"role": ["engineering_manager"], "function": ["backend"]}, None) == []   # no subject → no anchor
