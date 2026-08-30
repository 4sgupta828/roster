"""Integration tests for ClaimGraphStore — DB round-trips.

Skipped unless ROSTER_CORPUS_DSN points at a Postgres, so the offline suite stays
green without a DB (mirrors `roster_kernel/retrieval/test_postgres.py`).

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest apps/api/test_claimgraph_integration.py -q

Each test runs its whole async body in ONE event loop (an asyncpg pool is bound to
the loop that created it). Tests use unique per-run ids and never TRUNCATE, so they
are safe against a populated claim graph.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.claimgraph import ClaimGraphStore, normalize_name
from api.claimgraph_tech import make_tech_claim_store
from roster_vertical.claim_predicates import active_predicates

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN for claim-graph integration"),
]

_ALL_TABLES = ["rs_entity", "rs_entity_alias", "rs_claim", "rs_claim_evidence",
               "rs_claim_resolution", "rs_predicate", "rs_extraction_run"]
_SEED_PREDICATES = ["operates_in_category", "offers_product", "targets_customer",
                    "uses_technology", "claims_differentiator", "compared_to"]
# All 18 tech predicates (SLICE1 6 + DILIGENCE 12) now seed into rs_predicate via the wiring.
_ALL_18 = {p["name"] for p in active_predicates(include_diligence=True)}


def _uid(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


def test_ensure_schema_creates_tables_and_seeds_predicates() -> None:
    # Post-decouple (§D): the STORE seeds nothing on its own — the tech WIRING injects the
    # predicate registry. So the seed assertion is expressed via `make_tech_claim_store`.
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            await store.ensure_schema()
            await store.ensure_schema()   # idempotent second call
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                for t in _ALL_TABLES:
                    exists = await conn.fetchval("SELECT to_regclass($1)", t)
                    assert exists is not None, f"table {t} missing"
                active = {r["name"] for r in await conn.fetch(
                    "SELECT name FROM rs_predicate WHERE status='active'")}
            assert set(_SEED_PREDICATES) <= active
        finally:
            await store.close()
    asyncio.run(body())


def test_bare_store_seeds_nothing_and_imports_no_vertical() -> None:
    # §D: a bare ClaimGraphStore is vocabulary-neutral — it seeds NO predicates of its own
    # (the caller injects them) and its module imports nothing from the tech vertical.
    import api.claimgraph as cg_mod
    src = open(cg_mod.__file__, encoding="utf-8").read()
    assert "roster_vertical" not in src, "store module must not import the vertical"

    async def body():
        store = ClaimGraphStore(DSN)          # no seed_predicates → seeds nothing
        try:
            await store.ensure_schema()
            assert store.seed_predicates is None
            assert store.placement_predicate == "operates_in_category"   # back-compat default
            assert store.subject_kind == "company"
            assert await store.active_predicate_names() != None  # read works (may be non-empty
            #                                                       from sibling tech seeds — global)
        finally:
            await store.close()
    asyncio.run(body())


def test_tech_store_seeds_all_18_predicates_with_object_entity_kind() -> None:
    # §A: the DB registry is authoritative — all 18 tech predicates seed WITH object_entity_kind.
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            await store.ensure_schema()
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT name, object_kind, object_entity_kind FROM rs_predicate "
                    "WHERE status='active'")
            by = {r["name"]: r for r in rows}
            assert _ALL_18 <= set(by), "all 18 active tech predicates must be seeded"
            # the 12 diligence predicates now appear in rs_predicate
            assert "has_founder" in by and "raised_funding" in by and "risk_factor" in by
            # object_entity_kind persisted for entity predicates; '' for value predicates
            assert by["operates_in_category"]["object_entity_kind"] == "category"
            assert by["compared_to"]["object_entity_kind"] == "company"
            assert by["has_founder"]["object_entity_kind"] == "person"
            assert by["has_investor"]["object_entity_kind"] == "investor"
            assert by["offers_product"]["object_entity_kind"] == ""      # value predicate
            assert by["raised_funding"]["object_entity_kind"] == ""
        finally:
            await store.close()
    asyncio.run(body())


def test_active_predicate_names_returns_registry_config() -> None:
    # §A: active_predicate_names() is the authoritative active read (name + routing config).
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            rows = await store.active_predicate_names()
            by = {r["name"]: r for r in rows}
            assert _ALL_18 <= set(by)
            r = by["has_founder"]
            assert r["object_kind"] == "entity" and r["object_entity_kind"] == "person"
            assert r["status"] == "active"
            assert "description" in r      # carries the gloss the extractor prompt needs
        finally:
            await store.close()
    asyncio.run(body())


def test_covering_indexes_exist() -> None:
    # §B: the two covering indexes are created by ensure_schema (idempotent).
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            await store.ensure_schema()
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                names = {r["indexname"] for r in await conn.fetch(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE indexname IN ('ix_rs_claim_cat_rollup', 'ix_rs_claim_ev_active')")}
            assert "ix_rs_claim_cat_rollup" in names
            assert "ix_rs_claim_ev_active" in names
        finally:
            await store.close()
    asyncio.run(body())


def test_retired_predicate_not_resurrected_by_reseed() -> None:
    # §A: re-seed updates descriptive columns but NEVER un-retires a manually-retired predicate.
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            await store.ensure_schema()
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE rs_predicate SET status='retired' WHERE name='compared_to'")
            store._ready = False                       # force a re-seed pass
            await store.ensure_schema()
            async with pool.acquire() as conn:
                st = await conn.fetchval(
                    "SELECT status FROM rs_predicate WHERE name='compared_to'")
            assert st == "retired"                     # never resurrected
        finally:
            # restore for sibling tests sharing this (global) registry
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE rs_predicate SET status='active' WHERE name='compared_to'")
            await store.close()
    asyncio.run(body())


def test_custom_config_store_is_vocabulary_neutral() -> None:
    # §D: a store built with a NOVEL placement predicate + subject kind uses them in its
    # reads — proving the tech vocabulary is injected config, not baked into the store.
    async def body():
        store = ClaimGraphStore(
            DSN, placement_predicate="cites", subject_kind="case",
            mintable_entity_kinds=("statute", "court"))
        try:
            case_id = _uid("case")
            await store.upsert_entity(case_id, "case", "Roe v. Wade")
            statute_norm = "stat-" + uuid.uuid4().hex[:10]
            statute_id = "statute:" + statute_norm
            await store.upsert_entity(statute_id, "statute", "Title VII")
            cl = await store.upsert_claim(
                subject_id=case_id, predicate="cites", object_kind="entity",
                object_entity_id=statute_id, object_norm=statute_norm, confidence=0.9)
            await store.add_evidence(cl, _uid("doc"), "b1", "the court cites Title VII",
                                     authority_tier=3)

            # find_company resolves by the injected subject_kind ('case'), not 'company'
            found = await store.find_company("Roe v. Wade")
            assert found is not None and found["entity_id"] == case_id
            assert found["kind"] == "case"

            # distinct_categories groups by the injected placement predicate ('cites')
            cats = await store.distinct_categories()
            mine = [c for c in cats if c["object_norm"] == statute_norm]
            assert len(mine) == 1 and mine[0]["members"] == 1

            # population_claims scopes subjects by the injected subject kind + placement pred
            pop = await store.population_claims(category_norms=[statute_norm])
            ids = {c["entity_id"] for c in pop["companies"]}
            assert case_id in ids
        finally:
            await store.close()
    asyncio.run(body())


def test_entity_upsert_idempotent_and_status_preserved() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            eid = _uid("domain")
            await store.upsert_entity(eid, "company", "Acme Inc", primary_domain="acme.com")
            # suppress, then re-upsert — status must NOT be reset to active
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                await conn.execute("UPDATE rs_entity SET status='suppressed' WHERE entity_id=$1", eid)
            await store.upsert_entity(eid, "company", "Acme Incorporated", primary_domain="acme.io")
            async with pool.acquire() as conn:
                row = await conn.fetchrow("SELECT name, primary_domain, status FROM rs_entity WHERE entity_id=$1", eid)
                n = await conn.fetchval("SELECT count(*) FROM rs_entity WHERE entity_id=$1", eid)
            assert n == 1                                   # idempotent, no dup
            assert row["name"] == "Acme Incorporated"       # refreshed
            assert row["primary_domain"] == "acme.io"
            assert row["status"] == "suppressed"            # never resurrected
        finally:
            await store.close()
    asyncio.run(body())


def test_alias_roundtrip() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            eid = _uid("domain")
            base = "Acmeco" + uuid.uuid4().hex[:8]            # unique so alias_norm can't collide
            await store.upsert_entity(eid, "company", base + " Inc")
            await store.add_alias(base + ", Inc.", eid, source="test")
            assert await store.resolve_alias(base.lower()) == eid          # normalized match
            assert await store.resolve_alias(base + " LLC") == eid         # suffix-stripped match
            assert await store.resolve_alias("nonexistent-" + uuid.uuid4().hex) is None
        finally:
            await store.close()
    asyncio.run(body())


def test_claim_upsert_idempotent() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            eid = _uid("domain")
            cat = _uid("category")
            await store.upsert_entity(eid, "company", "Acme")
            c1 = await store.upsert_claim(subject_id=eid, predicate="operates_in_category",
                                          object_kind="entity", object_entity_id=cat,
                                          confidence=0.9)
            c2 = await store.upsert_claim(subject_id=eid, predicate="operates_in_category",
                                          object_kind="entity", object_entity_id=cat,
                                          confidence=0.5)
            assert c1 == c2                                  # same logical claim → same id
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                n = await conn.fetchval("SELECT count(*) FROM rs_claim WHERE claim_id=$1", c1)
                conf = await conn.fetchval("SELECT confidence FROM rs_claim WHERE claim_id=$1", c1)
            assert n == 1                                    # no dup
            assert float(conf) == 0.5                        # confidence refreshed
        finally:
            await store.close()
    asyncio.run(body())


def test_evidence_dedup() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            eid = _uid("domain")
            await store.upsert_entity(eid, "company", "Acme")
            cid = await store.upsert_claim(subject_id=eid, predicate="offers_product",
                                           object_kind="value", object_value="Widget")
            doc = _uid("doc")
            e1 = await store.add_evidence(cid, doc, "b1", "Acme  ships a Widget", authority_tier=2)
            # same block + reflowed/case-variant quote → same quote_hash → dedup no-op
            e2 = await store.add_evidence(cid, doc, "b1", "acme ships a widget", authority_tier=2)
            assert e1 == e2
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                n = await conn.fetchval("SELECT count(*) FROM rs_claim_evidence WHERE claim_id=$1", cid)
            assert n == 1
        finally:
            await store.close()
    asyncio.run(body())


def test_population_and_mark_stale() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            eid = _uid("domain")
            await store.upsert_entity(eid, "company", "Zeta Labs")
            obj_norm = "cat-" + uuid.uuid4().hex[:10]       # unique so population is clean
            cid = await store.upsert_claim(subject_id=eid, predicate="operates_in_category",
                                           object_kind="entity",
                                           object_entity_id="category:" + obj_norm,
                                           object_norm=obj_norm, confidence=0.8)
            doc = _uid("doc")
            await store.add_evidence(cid, doc, "b1", "Zeta operates in the space",
                                     authority_tier=3, source_key="reg")
            pop = await store.population("operates_in_category", obj_norm)
            assert len(pop) == 1
            row = pop[0]
            assert row["entity_id"] == eid
            assert row["name"] == "Zeta Labs"
            assert row["claim_id"] == cid
            assert row["evidence"]["document_id"] == doc
            assert row["evidence"]["block_id"] == "b1"

            # entity_claims read
            claims = await store.entity_claims(eid, predicates=["operates_in_category"])
            assert len(claims) == 1
            assert claims[0]["evidence"]["quote"] == "Zeta operates in the space"

            # GC coupling: mark the doc's evidence stale → excluded from population
            flipped = await store.mark_evidence_stale([doc])
            assert flipped == 1
            assert await store.mark_evidence_stale([doc]) == 0   # already stale, no-op
            pop2 = await store.population("operates_in_category", obj_norm)
            # grounding+GC invariant: a claim with no ACTIVE evidence is EXCLUDED
            # (not returned with null evidence) — excluded until re-extracted.
            assert pop2 == []
        finally:
            await store.close()
    asyncio.run(body())


def test_run_ledger() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            rid = _uid("run")
            await store.record_run(rid, source_keys="reg", params={"dry": True})
            await store.finish_run(rid, status="done", claims_emitted=7, extract_calls=3,
                                   est_cost_usd=0.0012)
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                r = await conn.fetchrow(
                    "SELECT status, claims_emitted, extract_calls, finished_at "
                    "FROM rs_extraction_run WHERE run_id=$1", rid)
            assert r["status"] == "done"
            assert r["claims_emitted"] == 7
            assert r["extract_calls"] == 3
            assert r["finished_at"] is not None
            with pytest.raises(ValueError):
                await store.finish_run(rid, bogus_counter=1)
        finally:
            await store.close()
    asyncio.run(body())
