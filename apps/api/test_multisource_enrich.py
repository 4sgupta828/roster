"""Tests for the multi-source enrichment PIPELINE (Task S2b).

TWO layers:

  * OFFLINE (no DB, no network): the pure structural helpers, and the DRY-RUN + fail-safe
    COLLECTION behavior driven through a fake store + fake fetchers/extractor that RAISE if
    the pipeline ever calls the LLM in a dry run. Always runs.

  * DSN-GATED INTEGRATION (skipped unless ROSTER_CORPUS_DSN is set — mirrors
    `test_canonicalize_integration.py`): drives `enrich_company` LIVE against a REAL Postgres
    with FAKE fetchers + a FAKE extractor (so NO network / NO live LLM call), the REAL S2a
    `resolve_entity` / `resolve_conflicts`, and the REAL claim-graph store. It asserts the
    PIPELINE WIRING end to end: a news doc attaches at `analysis` tier; a Form-D issuer that
    resolves to the company attaches at `primary_filing` tier and WINS conflict resolution
    over the news claim (alternates recorded); a Form-D NAMESAKE is REJECTED (not attached).

WHAT THE FAKE-FETCHER TESTS PROVE vs NOT (Rule 3): replacing the fetchers + extractor with
canned stubs proves the PIPELINE's mechanics — doc collection + fail-safe, block ingest,
the STRUCTURAL news-vs-filing routing, the ER-driven attach/reject decision (the S2b
payoff), the value/entity object routing + deterministic upsert with the right authority
tier, and the post-pass conflict resolution. It proves NOTHING about real fetch quality or
real extraction/merge quality (predicate choice, span/entail grounding, whether the live
EDGAR issuer truly is the company) — that is S2c's live run + the extractor's own held-out
gates. We stub the fetchers + extractor (not the S2a ER) precisely to keep this test about
the pipeline and the ER WIRING.

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_s2b_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest apps/api/test_multisource_enrich.py -q
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.canonicalize import resolve_conflicts, resolve_entity
from api.claimgraph import ClaimGraphStore, normalize_name
from api.claimgraph_tech import make_tech_claim_store
from api.multisource_enrich import (
    _entity_kind_by_predicate,
    _is_filing_doc,
    enrich_company,
)
from roster_vertical.authority import TechAuthorityPolicy
from roster_vertical.claim_predicates import active_predicates

_POLICY = TechAuthorityPolicy()


# --------------------------------------------------------------------------- #
# OFFLINE UNIT — pure structural helpers (no DB, no network)                   #
# --------------------------------------------------------------------------- #
def test_is_filing_doc_is_structural() -> None:
    # filing / patent source_kind, or an edgar/sec source_key → filing (ER-check the issuer)
    assert _is_filing_doc("edgar", {"source_kind": "filing"}) is True
    assert _is_filing_doc("edgar", {}) is True
    assert _is_filing_doc("sec", {}) is True
    assert _is_filing_doc("", {"source_kind": "patent"}) is True
    # news / analysis / reference → NOT a filing (subject IS the searched company)
    assert _is_filing_doc("news", {"source_kind": "news"}) is False
    assert _is_filing_doc("reuters", {}) is False
    assert _is_filing_doc("", {}) is False


def test_entity_kind_router_is_data_driven() -> None:
    kbp = _entity_kind_by_predicate(active_predicates(include_diligence=True))
    assert kbp["has_founder"] == "person"
    assert kbp["has_investor"] == "investor"
    assert kbp["operates_in_category"] == "category"
    assert kbp["compared_to"] == "company"
    # value predicates never appear
    assert "raised_funding" not in kbp and "headcount" not in kbp


# --------------------------------------------------------------------------- #
# OFFLINE — dry-run + fail-safe collection, fully faked (no DB, no LLM)        #
# --------------------------------------------------------------------------- #
class _FakeStore:
    """Minimal store stand-in for the OFFLINE dry-run/collection tests: enrich's dry-run path
    reads only vocabulary accessors + `ensure_schema`, and writes nothing."""
    subject_kind = "company"
    mintable_entity_kinds = ("category", "person", "investor", "company")

    async def ensure_schema(self) -> None:
        return None

    async def active_predicate_names(self) -> list[dict]:
        return active_predicates(include_diligence=True)


def _news_doc(doc_id: str, text: str = "Acme raised a Series A.") -> dict:
    return {"source_key": "news", "document_id": doc_id,
            "text": text, "facets": {"source_kind": "news"}}


async def _boom_llm_fn(*a, **k):  # any LLM-facing fn that must NOT be called in a dry run
    raise AssertionError("no LLM/extractor/resolve call may happen in a dry run")


def test_dry_run_returns_plan_writes_nothing_no_llm() -> None:
    async def body():
        async def news_fetcher(name):
            return [_news_doc("news:1"), _news_doc("news:2")]

        res = await enrich_company(
            store=_FakeStore(), dsn="postgresql://unused", company_id="yc:acme",
            company_name="Acme", fetchers=[news_fetcher],
            extract_fn=_boom_llm_fn, resolve_entity_fn=_boom_llm_fn,
            resolve_conflicts_fn=_boom_llm_fn, authority_policy=_POLICY,
            predicates=active_predicates(include_diligence=True), dry_run=True)

        assert res["dry_run"] is True
        assert res["docs_fetched"] == 2
        assert res["blocks_ingested"] == 0 and res["claims_added"] == 0
        assert res["rejected"] == [] and res["resolutions"] == []
        # the plan enumerates the docs it WOULD ingest + a cost note
        assert len(res["plan"]["docs"]) == 2
        assert {d["document_id"] for d in res["plan"]["docs"]} == {"news:1", "news:2"}
        assert all(d["is_filing"] is False for d in res["plan"]["docs"])
        assert "extract call" in res["plan"]["cost_note"]
        assert res["per_source"]["news"]["docs"] == 2
    asyncio.run(body())


def test_fetch_error_is_skipped_run_continues() -> None:
    async def body():
        async def broken_fetcher(name):
            raise RuntimeError("network down")

        async def good_fetcher(name):
            # includes a blank-text doc that must be filtered out of the plan
            return [_news_doc("news:ok"), {"source_key": "news", "document_id": "news:blank",
                                           "text": "   ", "facets": {"source_kind": "news"}}]

        res = await enrich_company(
            store=_FakeStore(), dsn="postgresql://unused", company_id="yc:acme",
            company_name="Acme", fetchers=[broken_fetcher, good_fetcher],
            extract_fn=_boom_llm_fn, resolve_entity_fn=_boom_llm_fn,
            resolve_conflicts_fn=_boom_llm_fn, authority_policy=_POLICY,
            predicates=active_predicates(include_diligence=True), dry_run=True)

        # broken fetcher skipped, good fetcher's real doc survives, blank doc filtered
        assert res["docs_fetched"] == 1
        assert [d["document_id"] for d in res["plan"]["docs"]] == ["news:ok"]
    asyncio.run(body())


# --------------------------------------------------------------------------- #
# DSN-GATED INTEGRATION — live pipeline, offline fetchers/extractor            #
# --------------------------------------------------------------------------- #
DSN = os.environ.get("ROSTER_CORPUS_DSN")
# Each DSN-gated test carries BOTH marks (integration + skip-without-DSN); the offline tests
# above are unmarked so they always run (do NOT set a module-level `integration` mark).
integration = pytest.mark.skipif(
    not DSN, reason="set ROSTER_CORPUS_DSN for multisource-enrich integration")


def _fake_funding_extractor(calls: list[str]):
    """Canned extractor keyed on the block text: '$10M' → a $10M raised_funding claim,
    '$12M' → a $12M one. Records each call's subject so a test can prove a REJECTED doc was
    never extracted (no spend). No network — a stub makes no LLM/entail call."""
    async def fake_extract(*, block_text, subject_name, predicates, client=None):
        calls.append(subject_name)
        if "$12M" in block_text:
            return [{"predicate": "raised_funding", "object_kind": "value",
                     "object_value": "$12M", "object_entity_name": "",
                     "quote": "raised $12M in the Form D"}]
        if "$10M" in block_text:
            return [{"predicate": "raised_funding", "object_kind": "value",
                     "object_value": "$10M", "object_entity_name": "",
                     "quote": "reportedly raised $10M"}]
        return []
    return fake_extract


async def _seed_company(store, tenant: str, company_id: str, name: str) -> None:
    """Create the already-canonical company (as S2a would have) so a news/filing claim has a
    real subject to attach to, and so a same-name issuer resolves to it by exact-norm."""
    await store.upsert_entity(company_id, "company", name, tenant_id=tenant)
    await store.add_alias(name, company_id, source="seed")


@integration
def test_news_doc_attaches_at_analysis_tier() -> None:
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        uniq = uuid.uuid4().hex[:10]
        company_id, name = f"yc:acme-{uniq}", f"Acme {uniq}"
        store = make_tech_claim_store(DSN)
        try:
            await _seed_company(store, tenant, company_id, name)

            async def news_fetcher(_name):
                return [{"source_key": "news", "document_id": f"news:{uniq}",
                         "text": f"{name} reportedly raised $10M in a Series A.",
                         "facets": {"source_kind": "news"}}]

            calls: list[str] = []
            res = await enrich_company(
                store=store, dsn=DSN, company_id=company_id, company_name=name,
                fetchers=[news_fetcher], extract_fn=_fake_funding_extractor(calls),
                resolve_entity_fn=resolve_entity, resolve_conflicts_fn=resolve_conflicts,
                authority_policy=_POLICY, tenant_id=tenant, llm=None, dry_run=False)

            assert res["dry_run"] is False
            assert res["blocks_ingested"] == 1
            assert res["claims_added"] == 1
            assert res["rejected"] == []
            assert calls == [name]                       # extractor ran once, on the company

            pool = await store._get_pool()
            async with pool.acquire() as conn:
                # the block was ingested
                nb = await conn.fetchval(
                    "SELECT count(*) FROM rs_block WHERE tenant_id=$1 AND document_id=$2",
                    tenant, f"news:{uniq}")
                # the claim attached to the COMPANY, evidence at ANALYSIS tier (rank 4)
                ev = await conn.fetchrow(
                    """SELECT ev.authority_tier, ev.evidence_kind, c.object_value
                       FROM rs_claim c JOIN rs_claim_evidence ev ON ev.claim_id=c.claim_id
                       WHERE c.tenant_id=$1 AND c.subject_id=$2 AND c.predicate='raised_funding'""",
                    tenant, company_id)
            assert nb == 1
            assert ev["object_value"] == "$10M"
            assert ev["evidence_kind"] == "analysis" and ev["authority_tier"] == 4
        finally:
            await store.close()
    asyncio.run(body())


@integration
def test_formd_resolves_to_company_and_wins_conflict() -> None:
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        uniq = uuid.uuid4().hex[:10]
        company_id, name = f"yc:acme-{uniq}", f"Acme {uniq}"
        store = make_tech_claim_store(DSN)
        try:
            await _seed_company(store, tenant, company_id, name)

            async def news_fetcher(_name):
                return [{"source_key": "news", "document_id": f"news:{uniq}",
                         "text": f"{name} reportedly raised $10M.",
                         "facets": {"source_kind": "news"}}]

            async def formd_fetcher(_name):
                # issuer name == the company name → resolve_entity exact-norm hit (no LLM)
                return [{"source_key": "edgar", "document_id": f"edgar:{uniq}",
                         "issuer_name": name,
                         "text": f"{name} raised $12M in the Form D filing.",
                         "facets": {"source_kind": "filing"}}]

            calls: list[str] = []
            res = await enrich_company(
                store=store, dsn=DSN, company_id=company_id, company_name=name,
                fetchers=[news_fetcher, formd_fetcher],
                extract_fn=_fake_funding_extractor(calls),
                resolve_entity_fn=resolve_entity, resolve_conflicts_fn=resolve_conflicts,
                authority_policy=_POLICY, tenant_id=tenant, llm=None, dry_run=False)

            assert res["claims_added"] == 2              # both attached to the company
            assert res["rejected"] == []

            # the Form-D claim landed at primary_filing tier (rank 6, controlling)
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT c.claim_id, c.object_value, ev.authority_tier, ev.evidence_kind
                       FROM rs_claim c JOIN rs_claim_evidence ev ON ev.claim_id=c.claim_id
                       WHERE c.tenant_id=$1 AND c.subject_id=$2 AND c.predicate='raised_funding'
                       ORDER BY ev.authority_tier""",
                    tenant, company_id)
            by_val = {r["object_value"]: r for r in rows}
            assert by_val["$10M"]["evidence_kind"] == "analysis"
            assert by_val["$10M"]["authority_tier"] == 4
            assert by_val["$12M"]["evidence_kind"] == "primary_filing"
            assert by_val["$12M"]["authority_tier"] == 6

            # conflict resolution ran and the FORM-D (controlling) claim WON over the news one
            resns = [r for r in res["resolutions"] if r["predicate"] == "raised_funding"]
            assert len(resns) == 1
            r = resns[0]
            assert r["winning_claim_id"] == by_val["$12M"]["claim_id"]
            assert r["conflict_claim_ids"] == [by_val["$10M"]["claim_id"]]

            # winner-preferred read: the $12M winner, the $10M loser kept as an ALTERNATE
            wp = await store.resolved_entity_claims(company_id, predicates=["raised_funding"],
                                                    tenant_id=tenant)
            funding = [x for x in wp if x["predicate"] == "raised_funding"]
            assert len(funding) == 1 and funding[0]["object_value"] == "$12M"
            assert funding[0]["is_resolved"] is True
            assert [a["object_value"] for a in funding[0]["alternates"]] == ["$10M"]
        finally:
            await store.close()
    asyncio.run(body())


@integration
def test_formd_namesake_is_rejected_not_attached() -> None:
    async def body():
        tenant = "t_" + uuid.uuid4().hex[:12]
        uniq = uuid.uuid4().hex[:10]
        company_id, name = f"yc:acme-{uniq}", f"Acme {uniq}"
        store = make_tech_claim_store(DSN)
        try:
            await _seed_company(store, tenant, company_id, name)

            # a Form D whose ISSUER is a NAMESAKE / SPV — a DIFFERENT company name (no strong
            # id, no LLM) → resolve_entity mints a NEW entity (method 'new'), NOT company_id.
            namesake = f"Cognition Therapeutics {uniq}"

            async def formd_fetcher(_name):
                return [{"source_key": "edgar", "document_id": f"edgar:{uniq}",
                         "issuer_name": namesake,
                         "text": f"{namesake} raised $12M in the Form D filing.",
                         "facets": {"source_kind": "filing"}}]

            calls: list[str] = []
            res = await enrich_company(
                store=store, dsn=DSN, company_id=company_id, company_name=name,
                fetchers=[formd_fetcher], extract_fn=_fake_funding_extractor(calls),
                resolve_entity_fn=resolve_entity, resolve_conflicts_fn=resolve_conflicts,
                authority_policy=_POLICY, tenant_id=tenant, llm=None, dry_run=False)

            # the block was still ingested, but the claims were NOT attached (no spend either)
            assert res["blocks_ingested"] == 1
            assert res["claims_added"] == 0
            assert calls == []                           # extractor NEVER ran on a rejected doc
            assert len(res["rejected"]) == 1
            rej = res["rejected"][0]
            assert rej["document_id"] == f"edgar:{uniq}"
            assert rej["issuer_name"] == namesake
            assert rej["resolved_entity_id"] != company_id
            assert rej["method"] == "new"

            pool = await store._get_pool()
            async with pool.acquire() as conn:
                # NOTHING attached to the targeted company (the ER payoff)
                n_on_company = await conn.fetchval(
                    "SELECT count(*) FROM rs_claim WHERE tenant_id=$1 AND subject_id=$2",
                    tenant, company_id)
                # the namesake entity was minted separately (kept out of the company)
                namesake_row = await conn.fetchrow(
                    "SELECT kind FROM rs_entity WHERE entity_id=$1 AND tenant_id=$2",
                    rej["resolved_entity_id"], tenant)
            assert n_on_company == 0
            assert namesake_row is not None and namesake_row["kind"] == "company"
        finally:
            await store.close()
    asyncio.run(body())
