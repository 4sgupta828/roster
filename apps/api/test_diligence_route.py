"""Integration test for the D3 single-company DILIGENCE route — DB round-trip, FAKE LLM.

Skipped unless ROSTER_CORPUS_DSN points at Postgres (mirrors the other claim-graph
integration tests). Seeds ONE grounded company under a UNIQUE tenant with claims across
several dimensions (incl. a has_founder→person), injects a FAKE `llm` whose `complete`
returns a canned diligence brief citing `[c1] [c2]` (plus a hallucinated `[c99]` to
exercise the code gate), and monkeypatches `diligence_route.derive` to `[]` so NO
reasoning LLM call fires. Asserts the grounded end-to-end wiring:

  * a non-empty answer is returned; every SURVIVING `[cN]` resolves to a real citation;
  * the hallucinated `[c99]` is dropped and counted (n_uncited_dropped);
  * citations non-empty; stats (n_founders/n_investors/n_dimensions_covered) present;
  * coverage_basis lists dimensions covered vs not_collected;
  * the unresolved-company path and the resolved-but-empty path return HONEST no-data
    answers with NO compose call (no fabrication).

NO network / NO live LLM — the FAKE llm proves WIRING + the citation gate; real brief
QUALITY (no uncited proper nouns) is the live eval.

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest \
        apps/api/test_diligence_route.py -q
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest

import api.diligence_route as diligence_route
from api.claimgraph import ClaimGraphStore
from api.diligence_route import answer_diligence

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
    """Async `complete` returning a canned diligence brief. Records call count so the
    honest no-data paths can assert the compose is NOT called."""
    def __init__(self, text: str):
        self._text = text
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048,
                       temperature=None):
        self.calls += 1
        return _FakeResult(self._text)


async def _seed_company(store: ClaimGraphStore, tenant: str, name: str) -> str:
    """One grounded company with claims across Team (founder→person + headcount),
    Funding (raised_funding), Product (offers_product), and Market (operates_in_category)
    — leaving Traction / Moat / Company-profile empty (not_collected)."""
    cid = "domain:" + uuid.uuid4().hex[:12]
    await store.upsert_entity(cid, "company", name, tenant_id=tenant)
    # Team — founder is a PERSON entity object
    pid = "person:" + uuid.uuid4().hex[:10]
    await store.upsert_entity(pid, "person", "Jane Roe", tenant_id=tenant)
    f = await store.upsert_claim(subject_id=cid, predicate="has_founder",
                                 object_kind="entity", object_entity_id=pid,
                                 confidence=0.9, tenant_id=tenant)
    await store.add_evidence(f, "doc-" + uuid.uuid4().hex[:8], "b1",
                             "Jane Roe co-founded the company", authority_tier=3,
                             tenant_id=tenant)
    h = await store.upsert_claim(subject_id=cid, predicate="headcount",
                                 object_kind="value", object_value="25",
                                 confidence=0.8, tenant_id=tenant)
    await store.add_evidence(h, "doc-" + uuid.uuid4().hex[:8], "b2",
                             "The team is 25 people", authority_tier=2, tenant_id=tenant)
    # Funding
    r = await store.upsert_claim(subject_id=cid, predicate="raised_funding",
                                 object_kind="value", object_value="Series A: $10M",
                                 confidence=0.8, tenant_id=tenant)
    await store.add_evidence(r, "doc-" + uuid.uuid4().hex[:8], "b3",
                             "Raised a $10M Series A", authority_tier=2, tenant_id=tenant)
    # Product
    p = await store.upsert_claim(subject_id=cid, predicate="offers_product",
                                 object_kind="value", object_value="AcmeVec",
                                 confidence=0.8, tenant_id=tenant)
    await store.add_evidence(p, "doc-" + uuid.uuid4().hex[:8], "b4",
                             "Offers AcmeVec", authority_tier=2, tenant_id=tenant)
    # Market
    await store.upsert_entity("category:vectordb", "category", "Vector Database",
                              tenant_id=tenant)
    m = await store.upsert_claim(subject_id=cid, predicate="operates_in_category",
                                 object_kind="entity", object_entity_id="category:vectordb",
                                 object_norm="vector database", confidence=0.9,
                                 tenant_id=tenant)
    await store.add_evidence(m, "doc-" + uuid.uuid4().hex[:8], "b5",
                             "Operates in vector databases", authority_tier=3,
                             tenant_id=tenant)
    return cid


def test_diligence_grounded_and_citation_gated(monkeypatch) -> None:
    async def _no_derive(*a, **k):
        return []
    monkeypatch.setattr(diligence_route, "derive", _no_derive)

    async def body():
        tenant = "t-" + uuid.uuid4().hex[:12]
        seed_store = ClaimGraphStore(DSN)
        try:
            await _seed_company(seed_store, tenant, "Acme")
        finally:
            await seed_store.close()

        fake = _FakeLLM(
            "## Team\nJane Roe [c1] founded the company; 25 people [c2].\n"
            "Ghost [c99] does not exist.\n\n## Coverage basis\n"
            "Grounded facts as of ingest, NOT a complete picture.")
        res = await answer_diligence(company="Acme", tenant_id=tenant, dsn=DSN, llm=fake)

        assert fake.calls == 1                          # compose wired, one LLM call
        assert res.get("error") is None
        assert res["empty"] is False and res["unresolved"] is False
        assert res["answer"]
        # citation gate: [c99] dropped + counted; valid ids survive
        assert res["n_uncited_dropped"] == 1
        assert "[c99]" not in res["answer"]
        assert "[c1]" in res["answer"] and "[c2]" in res["answer"]
        # every surviving marker resolves to a real citation id
        valid = {c["citation_id"] for c in res["citations"]}
        for mk in re.findall(r"\[(c\d+)\]", res["answer"]):
            assert mk in valid
        assert len(res["citations"]) == 5               # 5 grounded claims seeded
        # stats
        st = res["stats"]
        assert st["n_founders"] == 1
        assert st["n_investors"] == 0
        assert st["n_dimensions_total"] == 7
        assert st["n_cited_claims"] == 5
        assert st["n_dimensions_covered"] == 4          # Team, Funding, Product, Market
        # coverage_basis lists covered vs not_collected
        cb = res["coverage_basis"]
        assert "Team" in cb["dimensions_covered"]
        assert "Funding" in cb["dimensions_covered"]
        assert "Traction" in cb["dimensions_not_collected"]
        assert "Company profile" in cb["dimensions_not_collected"]
        assert cb["company_name"] == "Acme"
        assert res["subject_id"].startswith("domain:")
        assert res["derivations_rendered"] == ""        # derive stubbed → no section
    asyncio.run(body())


def test_unresolved_company_honest_no_data(monkeypatch) -> None:
    async def _no_derive(*a, **k):
        return []
    monkeypatch.setattr(diligence_route, "derive", _no_derive)

    async def body():
        tenant = "t-" + uuid.uuid4().hex[:12]
        fake = _FakeLLM("SHOULD NOT BE CALLED")
        res = await answer_diligence(company="Nonexistent Co", tenant_id=tenant,
                                     dsn=DSN, llm=fake)
        assert res.get("error") is None
        assert res["unresolved"] is True and res["empty"] is True
        assert fake.calls == 0                           # no fabrication → no compose call
        assert res["citations"] == []
        assert res["subject_id"] is None
        assert "No grounded company named 'Nonexistent Co'" in res["answer"]
        assert tenant in res["answer"]
    asyncio.run(body())


def test_resolved_but_no_claims_honest_no_data(monkeypatch) -> None:
    async def _no_derive(*a, **k):
        return []
    monkeypatch.setattr(diligence_route, "derive", _no_derive)

    async def body():
        tenant = "t-" + uuid.uuid4().hex[:12]
        seed_store = ClaimGraphStore(DSN)
        try:                                             # entity only, NO claims
            await seed_store.upsert_entity(
                "domain:" + uuid.uuid4().hex[:12], "company", "Empty Co", tenant_id=tenant)
        finally:
            await seed_store.close()
        fake = _FakeLLM("SHOULD NOT BE CALLED")
        res = await answer_diligence(company="Empty Co", tenant_id=tenant, dsn=DSN, llm=fake)
        assert res.get("error") is None
        assert res["empty"] is True and res["unresolved"] is False
        assert fake.calls == 0                           # resolved but no grounded claim
        assert res["citations"] == []
        assert res["subject_id"] is not None             # it DID resolve
        assert "no grounded, cited claims" in res["answer"]
    asyncio.run(body())
