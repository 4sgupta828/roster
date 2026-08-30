"""Tests for the corpus → claim-graph extraction JOB (Task 2b).

TWO layers, per the brief:

  * OFFLINE UNIT (no DB, no network): the cost-projection + cap math and the pure
    subject-name / slug / heading helpers. Always runs.

  * DSN-GATED INTEGRATION (skipped unless ROSTER_CORPUS_DSN is set — mirrors
    `test_claimgraph_integration.py`): seeds a few fake `rs_block` YC rows and
    MONKEYPATCHES `roster_vertical.claim_extract.extract_typed_claims` to return
    canned typed claims, so NO network / NO live LLM call happens. It asserts the JOB
    WIRING end to end: dry-run writes nothing, live writes entities+claims+evidence and
    `population()` finds the company grounded, and a cap-abort writes nothing.

WHAT THE MONKEYPATCHED TEST PROVES vs NOT (Rule 3): replacing the extractor with a canned
stub proves the JOB's mechanics — block selection, the source_key relevance gate, strong-id
subject resolution, value/category/`__unresolved` object resolution, deterministic
upsert of entity+claim+evidence with the right authority tier, the run ledger, and the
cost caps. It proves NOTHING about real extraction QUALITY (predicate choice, span/entail
grounding) — that is the extractor's own held-out concern (Task 2a's tests) and the live
eval (Task 5). We monkeypatch the extractor (not two LLM layers) precisely to keep this
test about the job.

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest apps/api/test_claim_extract_job.py -q
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.claim_extract_job import (
    _first_heading,
    _slug_of,
    _subject_name,
    project_cost,
    run_claim_extraction,
)
from api.claim_extract_job import _entity_kind_by_predicate  # data-driven object router
from api.claimgraph import ClaimGraphStore, normalize_name
from roster_vertical.claim_predicates import active_predicates


# --------------------------------------------------------------------------- #
# OFFLINE UNIT — cost/cap math + pure helpers (no DB, no network)             #
# --------------------------------------------------------------------------- #
def test_entity_kind_router_is_data_driven_from_object_entity_kind() -> None:
    # The router maps ENTITY predicates → their configured object_entity_kind (Rule 18:
    # config, not a keyword guess); value predicates never appear in the map.
    both = active_predicates(include_diligence=True)
    kbp = _entity_kind_by_predicate(both)
    assert kbp["operates_in_category"] == "category"
    assert kbp["compared_to"] == "company"
    assert kbp["has_founder"] == "person"
    assert kbp["has_investor"] == "investor"
    # a value predicate is not routed as an entity
    assert "offers_product" not in kbp
    assert "raised_funding" not in kbp
    # slice-1-only default excludes the diligence entity predicates
    kbp_s1 = _entity_kind_by_predicate(active_predicates())
    assert "has_founder" not in kbp_s1 and "operates_in_category" in kbp_s1



def test_project_cost_one_call_per_block() -> None:
    # this extractor is ONE call per block → projected_calls == blocks_considered exactly.
    p = project_cost(120, max_blocks=500, max_llm_calls=200, max_usd=5.0,
                     price_per_call_usd=0.01)
    assert p["projected_calls"] == 120
    assert p["est_usd"] == 1.2
    assert p["over_caps"] == []          # within all caps


def test_project_cost_aborts_on_max_blocks() -> None:
    p = project_cost(600, max_blocks=500, max_llm_calls=10_000, max_usd=10_000,
                     price_per_call_usd=0.01)
    assert any("max_blocks" in r for r in p["over_caps"])


def test_project_cost_aborts_on_max_llm_calls() -> None:
    p = project_cost(250, max_blocks=10_000, max_llm_calls=200, max_usd=10_000,
                     price_per_call_usd=0.01)
    assert any("max_llm_calls" in r for r in p["over_caps"])


def test_project_cost_aborts_on_max_usd() -> None:
    # 300 calls * $0.05 = $15 > $5 cap → abort on cost even though block/call counts fit.
    p = project_cost(300, max_blocks=10_000, max_llm_calls=10_000, max_usd=5.0,
                     price_per_call_usd=0.05)
    assert p["est_usd"] == 15.0
    assert any("max_usd" in r for r in p["over_caps"])


def test_project_cost_zero_blocks() -> None:
    p = project_cost(0, max_blocks=500, max_llm_calls=200, max_usd=5.0,
                     price_per_call_usd=0.01)
    assert p["projected_calls"] == 0 and p["est_usd"] == 0.0 and p["over_caps"] == []


def test_pure_helpers() -> None:
    assert _slug_of("yc:acme") == "acme"
    assert _slug_of("yc:acme-labs") == "acme-labs"
    assert _slug_of("noprefix") == "noprefix"
    assert _first_heading("# Acme\nbody") == "Acme"
    assert _first_heading("no heading here") == ""
    # subject name precedence: title > first heading > slug
    assert _subject_name("Acme Inc", "# Heading\nx", "acme") == "Acme Inc"
    assert _subject_name("", "# Heading\nx", "acme") == "Heading"
    assert _subject_name("", "no heading", "acme") == "acme"


# --------------------------------------------------------------------------- #
# DSN-GATED INTEGRATION — job wiring, offline extractor stub                  #
# --------------------------------------------------------------------------- #
DSN = os.environ.get("ROSTER_CORPUS_DSN")
integration = pytest.mark.skipif(
    not DSN, reason="set ROSTER_CORPUS_DSN for claim-extract-job integration")

_CAT_NAME = "Developer Tools"
_CAT_NORM = normalize_name(_CAT_NAME)            # 'developer tools'


def _fake_extractor(calls: list[str], *, n_entail: int = 0):
    """Return an async stand-in for `extract_typed_claims_counted` that records each call's
    subject and returns `(canned typed claims, n_entail)` (the counted API's shape). No
    network — a stub makes no real entail call, so `n_entail` defaults to 0."""
    async def fake_extract(*, block_text, subject_name, predicates, client=None, model=None):
        calls.append(subject_name)
        if subject_name == "Acme":
            return [
                {"predicate": "operates_in_category", "object_kind": "entity",
                 "object_value": "", "object_entity_name": _CAT_NAME,
                 "quote": "Acme operates in the Developer Tools category"},
                {"predicate": "offers_product", "object_kind": "value",
                 "object_value": "Acme CLI", "object_entity_name": "",
                 "quote": "Acme offers Acme CLI"},
                {"predicate": "compared_to", "object_kind": "entity",
                 "object_value": "", "object_entity_name": "Widgets Inc",
                 "quote": "Acme is often compared to Widgets Inc"},
            ], n_entail
        if subject_name == "Beta":
            return [
                {"predicate": "operates_in_category", "object_kind": "entity",
                 "object_value": "", "object_entity_name": "Fintech",
                 "quote": "Beta operates in Fintech"},
            ], n_entail
        return [], 0
    return fake_extract


async def _seed_yc_blocks(tenant: str) -> None:
    """Seed 2 real YC blocks + 1 whitespace-only block (must be skipped) into rs_block."""
    from roster_kernel.retrieval.postgres import PostgresRetrievalSource
    src = PostgresRetrievalSource(DSN)
    try:
        await src.ensure_schema()
        facets = {"source_kind": "reference", "entity_type": "company"}
        await src.upsert_block(
            tenant_id=tenant, document_id="yc:acme", block_id="b1",
            text="# Acme\nAcme operates in the Developer Tools category. "
                 "Acme offers Acme CLI. Acme is often compared to Widgets Inc.",
            facets=facets, document_title="Acme", content_type="text", source_key="yc")
        await src.upsert_block(
            tenant_id=tenant, document_id="yc:beta", block_id="b1",
            text="# Beta\nBeta operates in Fintech.",
            facets=facets, document_title="Beta", content_type="text", source_key="yc")
        # whitespace-only text → must be skipped by selection (blocks_considered stays 2)
        await src.upsert_block(
            tenant_id=tenant, document_id="yc:gamma", block_id="b1", text="   ",
            facets=facets, document_title="Gamma", content_type="text", source_key="yc")
    finally:
        await src.close()


@integration
def test_dry_run_writes_nothing_and_makes_no_llm_call(monkeypatch) -> None:
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        await _seed_yc_blocks(tenant)
        calls: list[str] = []
        import roster_vertical.claim_extract as ce
        monkeypatch.setattr(ce, "extract_typed_claims_counted", _fake_extractor(calls))

        res = await run_claim_extraction(
            dsn=DSN, source_keys=["yc"], tenant_id=tenant, dry_run=True)

        assert res["status"] == "dry_run"
        assert res["dry_run"] is True
        assert res["blocks_considered"] == 2            # whitespace block skipped
        assert res["projected_calls"] == 2              # projection present
        assert res["est_cost_usd"] == 0.02
        assert res["claims_emitted"] == 0
        assert calls == []                              # NO LLM/extractor call in dry run

        store = ClaimGraphStore(DSN)
        try:
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                n_claims = await conn.fetchval(
                    "SELECT count(*) FROM rs_claim WHERE tenant_id=$1", tenant)
                n_ent = await conn.fetchval(
                    "SELECT count(*) FROM rs_entity WHERE tenant_id=$1", tenant)
                ledger = await conn.fetchrow(
                    "SELECT status FROM rs_extraction_run WHERE run_id=$1", res["run_id"])
            assert n_claims == 0                         # nothing written
            assert n_ent == 0
            assert ledger["status"] == "dry_run"
        finally:
            await store.close()
    asyncio.run(body())


@integration
def test_live_writes_entities_claims_evidence_and_population(monkeypatch) -> None:
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        await _seed_yc_blocks(tenant)
        calls: list[str] = []
        import roster_vertical.claim_extract as ce
        monkeypatch.setattr(ce, "extract_typed_claims_counted", _fake_extractor(calls))

        res = await run_claim_extraction(
            dsn=DSN, source_keys=["yc"], tenant_id=tenant, dry_run=False)

        assert res["status"] == "done"
        assert res["blocks_considered"] == 2
        assert res["extract_calls"] == 2                 # one call per block
        assert sorted(calls) == ["Acme", "Beta"]         # extractor called per subject
        assert res["claims_emitted"] == 4                # 3 (Acme) + 1 (Beta)

        store = ClaimGraphStore(DSN)
        try:
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                # subject company (strong-id), category node, and __unresolved company node
                acme = await conn.fetchrow(
                    "SELECT kind, name FROM rs_entity WHERE entity_id='yc:acme' AND tenant_id=$1",
                    tenant)
                cat = await conn.fetchrow(
                    "SELECT kind, name FROM rs_entity WHERE entity_id=$1 AND tenant_id=$2",
                    "category:" + _CAT_NORM, tenant)
                unresolved = await conn.fetchrow(
                    "SELECT kind, name FROM rs_entity WHERE entity_id=$1 AND tenant_id=$2",
                    "__unresolved:widgets", tenant)
                # evidence carries the YC authority tier (verified_structured → rank 5)
                ev = await conn.fetchrow(
                    "SELECT authority_tier, evidence_kind, block_id FROM rs_claim_evidence "
                    "WHERE tenant_id=$1 AND document_id='yc:acme' LIMIT 1", tenant)
                ledger = await conn.fetchrow(
                    "SELECT status, blocks_considered, extract_calls, claims_emitted, est_cost_usd "
                    "FROM rs_extraction_run WHERE run_id=$1", res["run_id"])
            assert acme["kind"] == "company" and acme["name"] == "Acme"
            assert cat is not None and cat["kind"] == "category" and cat["name"] == _CAT_NAME
            assert unresolved is not None and unresolved["kind"] == "company"
            assert ev["authority_tier"] == 5 and ev["evidence_kind"] == "verified_structured"
            assert ev["block_id"] == "b1"                # real block id → span-gate re-verifiable
            assert ledger["status"] == "done"
            assert ledger["blocks_considered"] == 2
            assert ledger["extract_calls"] == 2
            assert ledger["claims_emitted"] == 4
            assert float(ledger["est_cost_usd"]) == 0.02

            # population read: who operates in 'developer tools' → exactly Acme, grounded.
            pop = await store.population("operates_in_category", _CAT_NORM, tenant_id=tenant)
            assert len(pop) == 1
            assert pop[0]["entity_id"] == "yc:acme"
            assert pop[0]["name"] == "Acme"
            assert pop[0]["evidence"]["document_id"] == "yc:acme"
            assert pop[0]["evidence"]["block_id"] == "b1"
        finally:
            await store.close()
    asyncio.run(body())


@integration
def test_cap_abort_writes_nothing(monkeypatch) -> None:
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        await _seed_yc_blocks(tenant)
        calls: list[str] = []
        import roster_vertical.claim_extract as ce
        monkeypatch.setattr(ce, "extract_typed_claims_counted", _fake_extractor(calls))

        # 2 eligible blocks > max_blocks=1 → abort BEFORE any spend, even in live mode.
        res = await run_claim_extraction(
            dsn=DSN, source_keys=["yc"], tenant_id=tenant, dry_run=False, max_blocks=1)

        assert res["status"] == "aborted"
        assert res["extract_calls"] == 0
        assert res["claims_emitted"] == 0
        assert any("max_blocks" in r for r in res["abort_reason"])
        assert calls == []                               # never called the extractor

        store = ClaimGraphStore(DSN)
        try:
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                n_claims = await conn.fetchval(
                    "SELECT count(*) FROM rs_claim WHERE tenant_id=$1", tenant)
                n_ent = await conn.fetchval(
                    "SELECT count(*) FROM rs_entity WHERE tenant_id=$1", tenant)
                ledger = await conn.fetchrow(
                    "SELECT status FROM rs_extraction_run WHERE run_id=$1", res["run_id"])
            assert n_claims == 0 and n_ent == 0          # nothing written on abort
            assert ledger["status"] == "aborted"
        finally:
            await store.close()
    asyncio.run(body())


# --------------------------------------------------------------------------- #
# DSN-GATED INTEGRATION — data-driven object routing (slice-2 diligence)      #
# --------------------------------------------------------------------------- #
# rs_entity.entity_id is a GLOBAL primary key (no tenant in the key) and
# upsert_entity's ON CONFLICT keeps the FIRST writer's tenant_id (cross-tenant
# dedup is the store's intended design). So deterministic object nodes must use
# per-run UNIQUE names here, or they collide with another test's identical node
# (e.g. the slice-1 "Developer Tools" category) and this run's tenant-filtered
# assertion would miss the row the earlier run owns.
def _fake_diligence_extractor(calls: list[str], names: dict):
    """Canned extractor returning the four ENTITY-object predicates whose routing must be
    data-driven: has_founder→person, has_investor→investor, operates_in_category→category,
    compared_to→company. Object names are per-run unique (passed in `names`). No network."""
    async def fake_extract(*, block_text, subject_name, predicates, client=None, model=None):
        calls.append(subject_name)
        if subject_name == "Delta":
            return [
                {"predicate": "has_founder", "object_kind": "entity", "object_value": "",
                 "object_entity_name": names["founder"], "quote": "founded by"},
                {"predicate": "has_investor", "object_kind": "entity", "object_value": "",
                 "object_entity_name": names["investor"], "quote": "backed by"},
                {"predicate": "operates_in_category", "object_kind": "entity", "object_value": "",
                 "object_entity_name": names["category"], "quote": "operates in"},
                {"predicate": "compared_to", "object_kind": "entity", "object_value": "",
                 "object_entity_name": names["company"], "quote": "compared to"},
            ], 0
        return [], 0
    return fake_extract


async def _seed_delta_block(tenant: str) -> None:
    from roster_kernel.retrieval.postgres import PostgresRetrievalSource
    src = PostgresRetrievalSource(DSN)
    try:
        await src.ensure_schema()
        facets = {"source_kind": "reference", "entity_type": "company"}
        # The job trusts the (monkeypatched) extractor's output verbatim — it does NOT
        # re-verify spans — so the block text need not match the canned quotes.
        await src.upsert_block(
            tenant_id=tenant, document_id="yc:delta", block_id="b1",
            text="# Delta\nDelta profile block.",
            facets=facets, document_title="Delta", content_type="text", source_key="yc")
    finally:
        await src.close()


@integration
def test_diligence_entity_objects_route_by_configured_kind(monkeypatch) -> None:
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        uniq = uuid.uuid4().hex[:10]
        names = {
            "founder": f"Jane Founder {uniq}",
            "investor": f"Sequoia {uniq}",
            "category": f"DevTools {uniq}",
            "company": f"Widgets {uniq}",
        }
        await _seed_delta_block(tenant)
        calls: list[str] = []
        import roster_vertical.claim_extract as ce
        monkeypatch.setattr(ce, "extract_typed_claims_counted", _fake_diligence_extractor(calls, names))

        # A DILIGENCE run: the caller opts into the slice-2 vocabulary. Without this the
        # has_founder/has_investor predicates are not active and the extractor's output
        # would never be reached — so pass the include_diligence set explicitly.
        res = await run_claim_extraction(
            dsn=DSN, source_keys=["yc"], tenant_id=tenant, dry_run=False,
            predicates=active_predicates(include_diligence=True))

        assert res["status"] == "done"
        assert res["claims_emitted"] == 4
        assert calls == ["Delta"]

        person_id = "person:" + normalize_name(names["founder"])
        investor_id = "investor:" + normalize_name(names["investor"])
        cat_id = "category:" + normalize_name(names["category"])
        company_id = "__unresolved:" + normalize_name(names["company"])

        store = ClaimGraphStore(DSN)
        try:
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                async def ent(eid):
                    return await conn.fetchrow(
                        "SELECT kind, name FROM rs_entity WHERE entity_id=$1 AND tenant_id=$2",
                        eid, tenant)
                # has_founder → PERSON node (kind='person', id 'person:<norm>')
                person = await ent(person_id)
                # has_investor → INVESTOR node (kind='investor', id 'investor:<norm>')
                investor = await ent(investor_id)
                # operates_in_category → CATEGORY node (unchanged slice-1 routing)
                cat = await ent(cat_id)
                # compared_to → COMPANY node via __unresolved: (unchanged slice-1 routing)
                company = await ent(company_id)

                async def obj_of(pred):
                    return await conn.fetchval(
                        "SELECT object_entity_id FROM rs_claim WHERE tenant_id=$1 "
                        "AND subject_id='yc:delta' AND predicate=$2", tenant, pred)
                founder_obj = await obj_of("has_founder")
                investor_obj = await obj_of("has_investor")
                cat_obj = await obj_of("operates_in_category")
                company_obj = await obj_of("compared_to")

            assert person is not None and person["kind"] == "person"
            assert person["name"] == names["founder"]
            assert investor is not None and investor["kind"] == "investor"
            assert investor["name"] == names["investor"]
            assert cat is not None and cat["kind"] == "category"
            assert company is not None and company["kind"] == "company"
            # every claim's object_entity_id points at the correctly-kinded minted node
            assert founder_obj == person_id
            assert investor_obj == investor_id
            assert cat_obj == cat_id
            assert company_obj == company_id
        finally:
            await store.close()
    asyncio.run(body())


# --------------------------------------------------------------------------- #
# DSN-GATED INTEGRATION — H1: entail-call cost tally + registry-authoritative  #
# --------------------------------------------------------------------------- #
@integration
def test_entail_calls_tallied_into_ledger(monkeypatch) -> None:
    # §C: each block's batched entail-judge call is tallied and folded into est_cost_usd.
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        await _seed_yc_blocks(tenant)
        calls: list[str] = []
        import roster_vertical.claim_extract as ce
        # stub reports ONE entail call per block (the real extractor's per-block batched call)
        monkeypatch.setattr(ce, "extract_typed_claims_counted",
                            _fake_extractor(calls, n_entail=1))

        res = await run_claim_extraction(
            dsn=DSN, source_keys=["yc"], tenant_id=tenant, dry_run=False,
            price_per_call_usd=0.01)

        assert res["status"] == "done"
        assert res["extract_calls"] == 2
        assert res["entail_calls"] == 2                  # one per block, tallied
        # cost now folds in the entail spend: (2 extract + 2 entail) * 0.01 = 0.04
        assert res["est_cost_usd"] == 0.04

        store = ClaimGraphStore(DSN)
        try:
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                ledger = await conn.fetchrow(
                    "SELECT extract_calls, entail_calls, est_cost_usd "
                    "FROM rs_extraction_run WHERE run_id=$1", res["run_id"])
            assert ledger["extract_calls"] == 2
            assert ledger["entail_calls"] == 2           # persisted to the ledger column
            assert float(ledger["est_cost_usd"]) == 0.04
        finally:
            await store.close()
    asyncio.run(body())


def test_project_cost_surfaces_entail_projection() -> None:
    # §C: project_cost now surfaces the entail-inclusive figures (no longer undercounts),
    # while the extract-only caps/fields stay back-compatible.
    p = project_cost(120, max_blocks=500, max_llm_calls=200, max_usd=5.0,
                     price_per_call_usd=0.01)
    assert p["projected_calls"] == 120 and p["est_usd"] == 1.2   # unchanged (extract-only)
    assert p["est_entail_calls"] == 120                          # ≤1 batched entail call/block
    assert p["est_total_calls"] == 240
    assert p["est_total_usd"] == 2.4                             # entail-inclusive spend
    assert p["over_caps"] == []                                  # caps still extract-only


def _recording_extractor(seen: dict):
    """Counted-API stub that records the predicate NAMES it was handed (so a test can prove
    the job passed the registry-derived active set, not a hardcoded default)."""
    async def fake(*, block_text, subject_name, predicates, client=None, model=None):
        seen["names"] = {p["name"] for p in predicates}
        return [], 0
    return fake


@integration
def test_job_reads_active_predicates_from_registry_when_none_passed(monkeypatch) -> None:
    # §A: with predicates=None the job constrains extraction to the LIVE rs_predicate active
    # set (authoritative), not the old slice-1 python default — and honors a retired row.
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        await _seed_yc_blocks(tenant)
        seen: dict = {}
        import roster_vertical.claim_extract as ce
        monkeypatch.setattr(ce, "extract_typed_claims_counted", _recording_extractor(seen))

        # Seed the registry (18 tech predicates), then RETIRE one in the DB.
        store = ClaimGraphStore(DSN)
        try:
            await store.ensure_schema()   # tables exist (bare store, seeds nothing)
        finally:
            await store.close()
        from api.claimgraph_tech import make_tech_claim_store
        seed_store = make_tech_claim_store(DSN)
        try:
            await seed_store.ensure_schema()   # seeds all 18
            pool = await seed_store._get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE rs_predicate SET status='retired' WHERE name='go_to_market'")
            try:
                await run_claim_extraction(
                    dsn=DSN, source_keys=["yc"], tenant_id=tenant, dry_run=False)
                # the job read the LIVE registry: diligence predicates present (proving it is
                # NOT the old slice-1 default), and the retired one EXCLUDED.
                assert "has_founder" in seen["names"]        # registry-driven (>6 predicates)
                assert "operates_in_category" in seen["names"]
                assert "go_to_market" not in seen["names"]   # retired → excluded
            finally:
                async with pool.acquire() as conn:           # restore the shared registry
                    await conn.execute(
                        "UPDATE rs_predicate SET status='active' WHERE name='go_to_market'")
        finally:
            await seed_store.close()
    asyncio.run(body())


@integration
def test_mention_policy_parks_objects_no_entity_pollution(monkeypatch) -> None:
    """FRESH-graph zero-pollution: object_policy='mention' must mint NO object entity nodes
    (no `category:<norm>`, no `__unresolved:`, no soft `<kind>:<norm>`). Bare-name objects
    (the category, the compared-to company 'Widgets Inc') degrade to grounded VALUE claims and
    are parked in the rs_mention lane; only SUBJECT companies remain canonical entities."""
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        await _seed_yc_blocks(tenant)
        import roster_vertical.claim_extract as ce
        monkeypatch.setattr(ce, "extract_typed_claims_counted", _fake_extractor([]))

        res = await run_claim_extraction(
            dsn=DSN, source_keys=["yc"], tenant_id=tenant, dry_run=False,
            object_policy="mention")
        assert res["status"] == "done"

        store = ClaimGraphStore(DSN)
        try:
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                polluted = await conn.fetchval(
                    "SELECT count(*) FROM rs_entity WHERE tenant_id=$1 AND "
                    "(entity_id LIKE 'category:%' OR entity_id LIKE '%__unresolved:%' "
                    " OR kind = 'category')", tenant)
                assert polluted == 0, "mention policy minted an object entity node (pollution)"
                n_mentions = await conn.fetchval(
                    "SELECT count(*) FROM rs_mention WHERE tenant_id=$1", tenant)
                assert n_mentions >= 2, "object names must be parked in the mention lane"
                n_company = await conn.fetchval(
                    "SELECT count(*) FROM rs_entity WHERE tenant_id=$1 AND kind='company'",
                    tenant)
                assert n_company >= 1, "subject companies must still be canonical entities"
                # the degraded object facts survive as grounded VALUE claims (evidence retained)
                n_val = await conn.fetchval(
                    "SELECT count(*) FROM rs_claim WHERE tenant_id=$1 AND object_kind='value'",
                    tenant)
                assert n_val >= 2
        finally:
            await store.close()
    asyncio.run(body())
