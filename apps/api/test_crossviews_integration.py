"""Integration tests for the CROSSVIEWS data layer — DB round-trips.

Skipped unless ROSTER_CORPUS_DSN points at a Postgres (mirrors
`test_claimgraph_aggregate_integration.py`). Seeds companies + a has_founder→person
edge + a category, then asserts the three new store reads (`predicate_coverage`,
`founder_rows`, `category_member_companies`) AND `build_grid` for all three row_kinds
produce grounded, cited grids.

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_cv_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest \
        apps/api/test_crossviews_integration.py -q

Uses unique per-run ids + a unique category norm so it is safe against a populated
graph and never truncates.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.claimgraph import ClaimGraphStore
from api.crossviews import CrossviewSpec, build_grid

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN for crossviews integration"),
]


def _uid(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


async def _seed(store: ClaimGraphStore, cat_norm: str, cat_entity: str) -> dict:
    """Two companies placed in `cat_norm`; company A also has a founder (has_founder →
    person) and an offers_product cell. Everything grounded. Returns the ids."""
    await store.upsert_entity(cat_entity, "category", "Widget Platforms")

    a = _uid("domain")
    await store.upsert_entity(a, "company", "Acme", primary_domain="acme.com")
    cat_a = await store.upsert_claim(
        subject_id=a, predicate="operates_in_category", object_kind="entity",
        object_entity_id=cat_entity, object_norm=cat_norm, confidence=0.9)
    await store.add_evidence(cat_a, _uid("doc"), "b1", "Acme operates in widgets",
                             authority_tier=3)
    prod_a = await store.upsert_claim(
        subject_id=a, predicate="offers_product", object_kind="value",
        object_value="AcmeWidget", object_norm="acmewidget", confidence=0.8)
    await store.add_evidence(prod_a, _uid("doc"), "b2", "Acme offers AcmeWidget",
                             authority_tier=2)

    person = _uid("person")
    await store.upsert_entity(person, "person", "Jane Roe")
    f_a = await store.upsert_claim(
        subject_id=a, predicate="has_founder", object_kind="entity",
        object_entity_id=person, object_norm="jane roe", confidence=0.9)
    await store.add_evidence(f_a, _uid("doc"), "b3", "Jane Roe co-founded Acme",
                             authority_tier=3)

    b = _uid("domain")
    await store.upsert_entity(b, "company", "Beta", primary_domain="beta.com")
    cat_b = await store.upsert_claim(
        subject_id=b, predicate="operates_in_category", object_kind="entity",
        object_entity_id=cat_entity, object_norm=cat_norm, confidence=0.9)
    await store.add_evidence(cat_b, _uid("doc"), "b4", "Beta operates in widgets",
                             authority_tier=2)
    return {"a": a, "b": b, "person": person, "cat_entity": cat_entity}


def test_reads_and_grids_grounded() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            cat_norm = "cat-" + uuid.uuid4().hex[:10]
            cat_entity = "category:" + cat_norm
            ids = await _seed(store, cat_norm, cat_entity)

            # ---- predicate_coverage (scoped to this category so counts are exact) ----
            cov = await store.predicate_coverage(category_norm=cat_norm)
            cov_map = {c["predicate"]: c["rows_covered"] for c in cov}
            assert cov_map["operates_in_category"] == 2   # A + B
            assert cov_map["offers_product"] == 1         # A only
            assert cov_map["has_founder"] == 1            # A only
            # ordered by rows_covered DESC
            counts = [c["rows_covered"] for c in cov]
            assert counts == sorted(counts, reverse=True)

            # ---- founder_rows (reverse edge) ----
            founders = await store.founder_rows()
            mine = [f for f in founders if f["person_id"] == ids["person"]]
            assert len(mine) == 1
            f = mine[0]
            assert f["name"] == "Jane Roe"
            assert [c["company_id"] for c in f["companies"]] == [ids["a"]]
            assert f["companies"][0]["quote"] == "Jane Roe co-founded Acme"
            assert f["companies"][0]["document_id"]        # grounded

            # ---- category_member_companies ----
            members = await store.category_member_companies(category_norm=cat_norm)
            names = {m["name"] for m in members}
            assert names == {"Acme", "Beta"}
            for m in members:
                assert m["document_id"] and m["quote"]     # each exemplar cited

            # ---- build_grid: company ----
            cgrid = await build_grid(store, CrossviewSpec(
                row_kind="company", filters={"category_norm": cat_norm},
                columns=[{"id": "has_founder", "kind": "predicate"},
                         {"id": "offers_product", "kind": "predicate"},
                         {"id": "raised_funding", "kind": "predicate"}]))
            assert cgrid["meta"]["row_count"] == 2
            acme = next(r for r in cgrid["rows"] if r["id"] == ids["a"])
            assert acme["cells"]["has_founder"]["collected"] is True
            assert acme["cells"]["has_founder"]["citations"][0]["document_id"]
            assert acme["cells"]["offers_product"]["value"] == "AcmeWidget"
            assert acme["cells"]["raised_funding"] == {"collected": False}
            beta = next(r for r in cgrid["rows"] if r["id"] == ids["b"])
            assert beta["cells"]["has_founder"] == {"collected": False}
            # every non-empty predicate cell is cited
            for r in cgrid["rows"]:
                for cell in r["cells"].values():
                    if cell.get("collected"):
                        assert len(cell["citations"]) >= 1

            # ---- build_grid: person ----
            pgrid = await build_grid(store, CrossviewSpec(row_kind="person"))
            jane = next(r for r in pgrid["rows"] if r["id"] == ids["person"])
            assert jane["cells"]["companies_founded"]["value"] == ["Acme"]
            assert len(jane["cells"]["companies_founded"]["citations"]) == 1
            assert jane["cells"]["n_companies"]["value"] == "1"

            # ---- build_grid: category ----
            gcat = await build_grid(store, CrossviewSpec(row_kind="category"))
            mine_cat = next(r for r in gcat["rows"] if r["id"] == cat_norm)
            assert mine_cat["cells"]["member_count"]["value"] == "2"
            reps = mine_cat["cells"]["representative_companies"]
            assert set(reps["value"]) == {"Acme", "Beta"}
            assert len(reps["citations"]) == 2
        finally:
            await store.close()
    asyncio.run(body())


def test_predicate_coverage_excludes_stale_evidence() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            cat_norm = "cat-" + uuid.uuid4().hex[:10]
            cat_entity = "category:" + cat_norm
            ids = await _seed(store, cat_norm, cat_entity)

            # Stale the offers_product evidence doc → offers_product must drop from
            # coverage (grounding-exclusion), operates_in_category stays.
            # Find the product evidence doc by re-reading is overkill; instead stale ALL
            # of Acme's product span via a fresh claim/evidence pair is complex — simpler:
            # coverage before, then stale the whole company's category evidence for B.
            cov_before = {c["predicate"]: c["rows_covered"]
                          for c in await store.predicate_coverage(category_norm=cat_norm)}
            assert cov_before["operates_in_category"] == 2

            # Re-place B via a doc we can stale, then confirm exclusion drops B's count.
            # (B currently has one category evidence row; stale it by document id.)
            # Fetch B's category evidence document id through population, then stale it.
            pop = await store.population_claims(category_norms=[cat_norm])
            beta = next(c for c in pop["companies"] if c["entity_id"] == ids["b"])
            beta_cat = next(cl for cl in beta["claims"]
                            if cl["predicate"] == "operates_in_category")
            doc = beta_cat["evidence"]["document_id"]
            flipped = await store.mark_evidence_stale([doc])
            assert flipped >= 1

            cov_after = {c["predicate"]: c["rows_covered"]
                         for c in await store.predicate_coverage(category_norm=cat_norm)}
            # B no longer has an active-evidence category placement → coverage drops to 1
            # (B also falls out of category scope for the placement predicate itself).
            assert cov_after.get("operates_in_category") == 1
        finally:
            await store.close()
    asyncio.run(body())
