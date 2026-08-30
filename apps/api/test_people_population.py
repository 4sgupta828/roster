"""Offline unit tests for the people-population ENGINE (facet parse + answer assembly + coverage).

Fake store + fake LLM, so the orchestration + grounding/coverage LOGIC is proven with no DB/network
(Rule 3: proves the plumbing + honesty gates, not real ranking quality). The SQL enumeration itself is
covered by test_people_population_integration.py.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from dataclasses import dataclass

from api.people_population import _FacetParse, answer_people_population, parse_people_facets


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@dataclass
class _Res:
    parsed: object


class _FakeLLM:
    """Returns a scripted _FacetParse from complete() (or raises to test the fail-safe)."""
    def __init__(self, parsed):
        self._parsed = parsed

    async def complete(self, *, system, messages, response_format=None, max_tokens=0):
        if isinstance(self._parsed, Exception):
            raise self._parsed
        return _Res(parsed=self._parsed)


class _FakeStore:
    def __init__(self, rows, stats):
        self._rows = rows
        self._stats = stats
        self.last_facets = None

    async def people_index_stats(self, *, tenant_id="demo"):
        return self._stats

    async def enumerate_by_facets(self, facets, *, tenant_id="demo", cap=200):
        self.last_facets = facets
        return self._rows


_STATS = {"persons_indexed": 42, "source_documents": 3,
          "facet_coverage": {"seniority": 12, "function": 40, "metro": 30}}

_ROWS = [
    {"entity_id": "github:dana", "name": "Dana Director", "facets": [
        {"facet_key": "seniority", "display_value": "Director of ML", "value_norm": "director",
         "document_id": "https://acme.example/leadership", "block_id": "b1", "source_claim_id": "c1"},
        {"facet_key": "metro", "display_value": "Bay Area", "value_norm": "bay_area",
         "document_id": "https://acme.example/leadership", "block_id": "b1", "source_claim_id": "c2"}]},
]


def test_parse_people_facets_compiles_query():
    llm = _FakeLLM(_FacetParse(seniority=["Director", "Engineering Manager"],
                               function=["machine_learning"], metro=["bay_area"]))
    facets = _run(parse_people_facets("Directors/EMs in ML in Bay Area", llm))
    assert facets == {"seniority": ["director", "engineering_manager"],
                      "function": ["machine_learning"], "metro": ["bay_area"]}


def test_parse_returns_empty_for_non_people_query_and_on_error():
    assert _run(parse_people_facets("who is Andrej Karpathy", _FakeLLM(_FacetParse()))) == {}
    assert _run(parse_people_facets("anything", _FakeLLM(RuntimeError("provider down")))) == {}


def test_answer_grounded_rows_with_citations_and_coverage():
    llm = _FakeLLM(_FacetParse(seniority=["director"], function=["machine_learning"], metro=["bay_area"]))
    store = _FakeStore(_ROWS, _STATS)
    out = _run(answer_people_population(question="Directors in ML in Bay Area",
                                       tenant_id="demo", store=store, llm=llm))
    assert out["grounded"] is True and out["not_people_query"] is False
    assert len(out["people_rows"]) == 1
    row = out["people_rows"][0]
    assert row["name"] == "Dana Director"
    assert row["citation"]["document_id"]                      # grounded row
    assert any(a["key"] == "seniority" for a in row["attributes"])
    cov = out["coverage_basis"]
    assert cov["matches_returned"] == 1 and cov["persons_indexed"] == 42
    assert "not_ingested" in cov and "NOT an exhaustive" in cov["population_statement"]
    assert "Dana Director" in out["answer"] and "grounded people index" in out["answer"]


def test_linkedin_search_proxy_when_no_direct_link():
    # A person with no direct LinkedIn link gets a Google-search proxy (name + role) to reach it.
    out = _run(answer_people_population(question="ML directors", tenant_id="demo",
                                       store=_FakeStore(_ROWS, _STATS),
                                       llm=_FakeLLM(_FacetParse(seniority=["director"]))))
    links = out["people_rows"][0]["links"]
    proxy = [l for l in links if l["kind"] == "linkedin_search"]
    assert proxy and "google.com/search" in proxy[0]["url"]
    assert "Dana" in urllib.parse.unquote(proxy[0]["url"])


def test_empty_match_is_honest_not_a_crash():
    llm = _FakeLLM(_FacetParse(seniority=["director"], function=["quantum"], metro=["bay_area"]))
    out = _run(answer_people_population(question="Directors in quantum in Bay Area",
                                       tenant_id="demo", store=_FakeStore([], _STATS), llm=llm))
    assert out["grounded"] is False and out["not_people_query"] is False
    assert out["people_rows"] == []
    assert "coverage gap" in out["answer"] and out["coverage_basis"]["matches_returned"] == 0


def test_non_people_query_signals_fallthrough():
    out = _run(answer_people_population(question="who is X", tenant_id="demo",
                                       store=_FakeStore([], _STATS), llm=_FakeLLM(_FacetParse())))
    assert out["not_people_query"] is True and out["grounded"] is False


def test_research_out_accepts_people_population_result():
    """Regression: the /research people-route must build a VALID ResearchOut. `rejected` is a required
    field with no default — omitting it 500s the whole (working) answer at serialization."""
    from api.app import ResearchOut
    engine = _run(answer_people_population(question="Directors in ML in Bay Area", tenant_id="demo",
                                           store=_FakeStore(_ROWS, _STATS),
                                           llm=_FakeLLM(_FacetParse(seniority=["director"]))))
    ro = ResearchOut(grounded=engine["grounded"], answer=engine["answer"], claims=[],
                     coverage_gaps=[], rejected=0, people_rows=engine["people_rows"],
                     coverage_basis=engine["coverage_basis"], session_id=None)
    assert ro.grounded is True and ro.people_rows and ro.coverage_basis
