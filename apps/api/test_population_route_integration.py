"""Integration test for the Task-4 population ANSWER ROUTE — DB round-trip, FAKE LLM.

Skipped unless ROSTER_CORPUS_DSN points at Postgres (mirrors the other claim-graph
integration tests). Seeds a small GROUNDED population under a UNIQUE tenant (so it is
isolated from any populated graph), injects a FAKE `llm` whose `complete` returns a
canned market-map answer citing `[c1] [c2]` (plus a hallucinated `[c99]` to exercise
the code gate), and monkeypatches `population_route.derive` to `[]` so NO reasoning
LLM call fires. Asserts the grounded end-to-end wiring:

  * a non-empty answer is returned; every SURVIVING `[cN]` resolves to a real citation;
  * the hallucinated `[c99]` is dropped and counted (n_uncited_dropped);
  * citations are non-empty, stats present, coverage_basis names the seeded category;
  * the empty-population path (a fresh tenant) returns the HONEST no-data answer.

NO network / NO live LLM — the FAKE llm proves WIRING + the citation gate; real compose
QUALITY (no uncited proper nouns) is the live eval (Task 5).

    ROSTER_CORPUS_DSN=postgresql://strata:strata@localhost:5433/roster_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest \
        apps/api/test_population_route_integration.py -q
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

import api.population_route as population_route
from api.claimgraph import ClaimGraphStore
from api.population_route import answer_from_population

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN for claim-graph integration"),
]


class _FakeParsed:
    def __init__(self, text: str):
        self.text = text


class _FakeResult:
    def __init__(self, text: str):
        self.parsed = _FakeParsed(text)
        self.model = "fake"


class _FakeLLM:
    """Async `complete` returning a canned market-map answer. Records the last call so a
    test could inspect the rendered prompt; here it just proves the compose is wired."""
    def __init__(self, text: str):
        self._text = text
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048,
                       temperature=None):
        self.calls += 1
        return _FakeResult(self._text)


async def _seed_population(store: ClaimGraphStore, tenant: str, cat_norm: str,
                           cat_entity: str, cat_name: str) -> None:
    """Two grounded companies in one named category, each with one cited cell — so
    build_market_map assigns exactly c1 (Acme) and c2 (Beta)."""
    await store.upsert_entity(cat_entity, "category", cat_name, tenant_id=tenant)
    # Acme — offers_product cell
    a = "domain:" + uuid.uuid4().hex[:12]
    await store.upsert_entity(a, "company", "Acme", tenant_id=tenant)
    ac = await store.upsert_claim(subject_id=a, predicate="operates_in_category",
                                  object_kind="entity", object_entity_id=cat_entity,
                                  object_norm=cat_norm, confidence=0.9, tenant_id=tenant)
    await store.add_evidence(ac, "doc-" + uuid.uuid4().hex[:8], "b1",
                             "Acme operates in the category", authority_tier=3, tenant_id=tenant)
    ap = await store.upsert_claim(subject_id=a, predicate="offers_product",
                                  object_kind="value", object_value="AcmeVec",
                                  confidence=0.8, tenant_id=tenant)
    await store.add_evidence(ap, "doc-" + uuid.uuid4().hex[:8], "b2",
                             "Acme offers AcmeVec", authority_tier=2, tenant_id=tenant)
    # Beta — targets_customer cell
    b = "domain:" + uuid.uuid4().hex[:12]
    await store.upsert_entity(b, "company", "Beta", tenant_id=tenant)
    bc = await store.upsert_claim(subject_id=b, predicate="operates_in_category",
                                  object_kind="entity", object_entity_id=cat_entity,
                                  object_norm=cat_norm, confidence=0.9, tenant_id=tenant)
    await store.add_evidence(bc, "doc-" + uuid.uuid4().hex[:8], "b3",
                             "Beta operates in the category", authority_tier=3, tenant_id=tenant)
    bt = await store.upsert_claim(subject_id=b, predicate="targets_customer",
                                  object_kind="value", object_value="enterprise",
                                  confidence=0.8, tenant_id=tenant)
    await store.add_evidence(bt, "doc-" + uuid.uuid4().hex[:8], "b4",
                             "Beta targets enterprise", authority_tier=2, tenant_id=tenant)


def test_population_answer_grounded_and_citation_gated(monkeypatch) -> None:
    # No reasoning LLM call — derive stubbed to [] so the fake compose is the only call.
    async def _no_derive(*a, **k):
        return []
    monkeypatch.setattr(population_route, "derive", _no_derive)

    async def body():
        tenant = "t-" + uuid.uuid4().hex[:12]
        cat_norm = "cat-" + uuid.uuid4().hex[:10]
        seed_store = ClaimGraphStore(DSN)
        try:
            await _seed_population(seed_store, tenant, cat_norm,
                                   "category:" + cat_norm, "Vector Database")
        finally:
            await seed_store.close()

        # Canned answer cites both valid ids + a hallucinated one the gate must drop.
        fake = _FakeLLM("Acme [c1] leads the vector database space; Beta [c2] targets "
                        "enterprise. Ghost [c99] does not exist.\n\n## Coverage basis\n"
                        "Reflects the grounded population, not all startups.")
        res = await answer_from_population(question="Map the vector database landscape",
                                           tenant_id=tenant, dsn=DSN, llm=fake)

        assert fake.calls == 1                         # compose wired, one LLM call
        assert res.get("error") is None
        assert res["empty_population"] is False
        assert res["answer"]                           # non-empty grounded answer
        # citation gate: [c99] dropped + counted; the two valid ids survive
        assert res["n_uncited_dropped"] == 1
        assert "[c99]" not in res["answer"]
        assert "[c1]" in res["answer"] and "[c2]" in res["answer"]
        # citations non-empty and EVERY surviving [cN] resolves to a real citation id
        valid_ids = {c["citation_id"] for c in res["citations"]}
        assert {"c1", "c2"} <= valid_ids
        import re
        for m in re.findall(r"\[(c\d+)\]", res["answer"]):
            assert m in valid_ids                      # no hallucinated ref survives
        # stats + coverage_basis name the seeded category
        assert res["stats"]["n_companies"] == 2
        assert "Vector Database" in res["coverage_basis"]["categories_covered"]
        assert "NOT all startups" in res["coverage_basis"]["population_statement"]
        assert res["derivations_rendered"] == ""       # derive stubbed → no section
    asyncio.run(body())


def test_empty_population_honest_no_data(monkeypatch) -> None:
    async def _no_derive(*a, **k):
        return []
    monkeypatch.setattr(population_route, "derive", _no_derive)

    async def body():
        tenant = "t-empty-" + uuid.uuid4().hex[:12]   # fresh tenant, no data
        fake = _FakeLLM("SHOULD NOT BE CALLED")
        res = await answer_from_population(question="Map the landscape",
                                           tenant_id=tenant, dsn=DSN, llm=fake)
        assert res.get("error") is None
        assert res["empty_population"] is True
        assert fake.calls == 0                         # no fabrication → no compose call
        assert res["citations"] == []
        assert "No grounded startup population" in res["answer"]
        assert tenant in res["answer"]                 # honest, names the tenant
    asyncio.run(body())
