"""Integration tests for the Task-3 aggregation reads — DB round-trips.

Skipped unless ROSTER_CORPUS_DSN points at a Postgres (mirrors
`test_claimgraph_integration.py`). Covers `distinct_categories` and
`population_claims`: grounded counts, stale-evidence exclusion, and cap +
truncation reporting (no silent truncation).

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest \
        apps/api/test_claimgraph_aggregate_integration.py -q

Tests use unique per-run ids and a unique category norm, so they are safe against
a populated claim graph and never TRUNCATE.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.claimgraph import ClaimGraphStore

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN for claim-graph integration"),
]


def _uid(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


async def _seed_company(store: ClaimGraphStore, cat_norm: str, cat_entity: str,
                        *, name: str, product: str) -> str:
    """A company placed in category `cat_norm` with one offers_product cell, both
    grounded. Returns the company entity_id."""
    eid = _uid("domain")
    await store.upsert_entity(eid, "company", name, primary_domain=name.lower() + ".com")
    cat_claim = await store.upsert_claim(
        subject_id=eid, predicate="operates_in_category", object_kind="entity",
        object_entity_id=cat_entity, object_norm=cat_norm, confidence=0.9)
    await store.add_evidence(cat_claim, _uid("doc"), "b1", f"{name} operates in {cat_norm}",
                             authority_tier=3, source_key="reg")
    prod_claim = await store.upsert_claim(
        subject_id=eid, predicate="offers_product", object_kind="value",
        object_value=product, confidence=0.8)
    await store.add_evidence(prod_claim, _uid("doc"), "b2", f"{name} offers {product}",
                             authority_tier=2)
    return eid


def test_distinct_categories_grounded_and_counted() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            cat_norm = "cat-" + uuid.uuid4().hex[:10]
            cat_entity = "category:" + cat_norm
            # Name the category entity so `name` comes from the join, not object_norm.
            await store.upsert_entity(cat_entity, "category", "Widget Platforms")
            await _seed_company(store, cat_norm, cat_entity, name="Acme", product="AcmeWidget")
            await _seed_company(store, cat_norm, cat_entity, name="Beta", product="BetaWidget")

            cats = await store.distinct_categories()
            mine = [c for c in cats if c["object_norm"] == cat_norm]
            assert len(mine) == 1
            assert mine[0]["members"] == 2
            assert mine[0]["name"] == "Widget Platforms"   # from rs_entity join

            # min_members filter drops the 2-member category when the floor is higher.
            cats_hi = await store.distinct_categories(min_members=3)
            assert all(c["object_norm"] != cat_norm for c in cats_hi)
        finally:
            await store.close()
    asyncio.run(body())


def test_population_claims_grounded_with_stale_excluded() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            cat_norm = "cat-" + uuid.uuid4().hex[:10]
            cat_entity = "category:" + cat_norm
            eid_a = await _seed_company(store, cat_norm, cat_entity, name="Acme", product="AcmeWidget")

            # A second company whose ONLY evidence we then mark stale → must drop out.
            eid_b = _uid("domain")
            await store.upsert_entity(eid_b, "company", "Ghost")
            cat_b = await store.upsert_claim(
                subject_id=eid_b, predicate="operates_in_category", object_kind="entity",
                object_entity_id=cat_entity, object_norm=cat_norm, confidence=0.9)
            stale_doc = _uid("doc")
            await store.add_evidence(cat_b, stale_doc, "b1", "Ghost operates here", authority_tier=1)

            res = await store.population_claims(category_norms=[cat_norm])
            ids = {c["entity_id"] for c in res["companies"]}
            assert eid_a in ids and eid_b in ids           # both grounded so far

            flipped = await store.mark_evidence_stale([stale_doc])
            assert flipped == 1
            res2 = await store.population_claims(category_norms=[cat_norm])
            ids2 = {c["entity_id"] for c in res2["companies"]}
            assert eid_a in ids2
            assert eid_b not in ids2                        # no active evidence → excluded

            # Acme carries its grounded claims, each with an active evidence row.
            acme = next(c for c in res2["companies"] if c["entity_id"] == eid_a)
            preds = {cl["predicate"] for cl in acme["claims"]}
            assert {"operates_in_category", "offers_product"} <= preds
            for cl in acme["claims"]:
                assert cl["evidence"]["document_id"]        # never an ungrounded claim
            assert res2["meta"]["companies_truncated"] is False
            assert res2["meta"]["claims_truncated"] is False
        finally:
            await store.close()
    asyncio.run(body())


def test_population_claims_caps_report_truncation() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            cat_norm = "cat-" + uuid.uuid4().hex[:10]
            cat_entity = "category:" + cat_norm
            for i in range(3):
                await _seed_company(store, cat_norm, cat_entity,
                                    name=f"Co{i}", product=f"Prod{i}")

            # company_cap below the population → clip + surfaced truncation.
            res = await store.population_claims(category_norms=[cat_norm], company_cap=2)
            assert len(res["companies"]) == 2
            assert res["meta"]["companies_truncated"] is True

            # claims_per_company_cap below a company's claim count → per-company clip.
            res2 = await store.population_claims(category_norms=[cat_norm],
                                                 claims_per_company_cap=1)
            for c in res2["companies"]:
                assert len(c["claims"]) == 1                # capped
            assert res2["meta"]["claims_truncated"] is True
            assert len(res2["meta"]["clipped_company_ids"]) >= 1
        finally:
            await store.close()
    asyncio.run(body())
