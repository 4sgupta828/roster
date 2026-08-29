"""DSN-gated integration tests for the canonicalization store additions + the two CORE
primitives against a REAL Postgres (Task S2a).

Skipped unless ROSTER_CORPUS_DSN points at a Postgres (mirrors
`test_claimgraph_integration.py`). Uses unique per-run ids and never TRUNCATEs, so it is
safe against a populated claim graph.

    ROSTER_CORPUS_DSN=postgresql://strata:strata@localhost:5433/roster_canon_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest apps/api/test_canonicalize_integration.py -q
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.canonicalize import resolve_conflicts, resolve_entity
from api.claimgraph import ClaimGraphStore, normalize_name
from roster_vertical.authority import TechAuthorityPolicy

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN for canonicalize integration"),
]

_POLICY = TechAuthorityPolicy()


def _uid(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- #
# Store additions — round-trips                                               #
# --------------------------------------------------------------------------- #
def test_find_entities_by_norm_matches_name_and_alias() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            tag = uuid.uuid4().hex[:10]
            name = f"Acme {tag} Inc."               # normalizes to "acme <tag>"
            norm = normalize_name(name)
            eid = _uid("yc")
            await store.upsert_entity(eid, "company", name)
            # match by NAME normalization
            got = await store.find_entities_by_norm(norm, "company")
            assert any(g["entity_id"] == eid for g in got)
            # match by ALIAS normalization (a different surface form → same norm bucket via alias)
            other = _uid("yc")
            await store.upsert_entity(other, "company", f"Something Else {tag}")
            await store.add_alias(name, other, source="news")
            got2 = await store.find_entities_by_norm(norm, "company")
            ids = {g["entity_id"] for g in got2}
            assert eid in ids and other in ids       # both the name-match and the alias-match
            # kind isolation: a person with the same norm never appears in a company lookup
            pid = _uid("person")
            await store.upsert_entity(pid, "person", name)
            got3 = await store.find_entities_by_norm(norm, "company")
            assert pid not in {g["entity_id"] for g in got3}
        finally:
            await store.close()
    asyncio.run(body())


def test_claim_evidence_tiers_and_record_resolution_roundtrip() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            subj = _uid("yc")
            await store.upsert_entity(subj, "company", "Acme")
            c_hi = await store.upsert_claim(subject_id=subj, predicate="raised_funding",
                                            object_kind="value", object_value="$12M",
                                            confidence=0.5)
            c_lo = await store.upsert_claim(subject_id=subj, predicate="raised_funding",
                                            object_kind="value", object_value="$10M",
                                            confidence=0.9)
            await store.add_evidence(c_hi, _uid("doc"), "b1", "raised $12M in Form D",
                                     source_key="edgar", authority_tier=6,
                                     evidence_kind="primary_filing")
            await store.add_evidence(c_lo, _uid("doc"), "b2", "reportedly raised $10M",
                                     source_key="news", authority_tier=4,
                                     evidence_kind="analysis")
            tiers = await store.claim_evidence_tiers(c_hi)
            assert tiers and tiers[0]["evidence_kind"] == "primary_filing"
            assert tiers[0]["authority_tier"] == 6

            # record_resolution upsert + idempotent overwrite
            await store.record_resolution(
                subject_id=subj, predicate="raised_funding", valid_bucket="",
                winning_claim_id=c_hi, conflict_claim_ids=[c_lo],
                winner_authority_tier=6, rationale="controlling evidence")
            await store.record_resolution(   # re-run rewrites the same winner (idempotent PK)
                subject_id=subj, predicate="raised_funding", valid_bucket="",
                winning_claim_id=c_hi, conflict_claim_ids=[c_lo],
                winner_authority_tier=6, rationale="controlling evidence")
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                n = await conn.fetchval(
                    "SELECT count(*) FROM rs_claim_resolution "
                    "WHERE subject_id=$1 AND predicate='raised_funding'", subj)
            assert n == 1
        finally:
            await store.close()
    asyncio.run(body())


# --------------------------------------------------------------------------- #
# End-to-end: resolve_entity + resolve_conflicts + resolved_entity_claims     #
# --------------------------------------------------------------------------- #
def test_resolve_entity_strong_id_then_exact_norm_against_db() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            tag = uuid.uuid4().hex[:10]
            # first mention from EDGAR: new entity keyed by strong id (cik)
            r1 = await resolve_entity(store, name=f"Acme {tag} Inc.", kind="company",
                                      strong_ids={"cik": tag}, source_key="edgar")
            assert r1["is_new"] and r1["entity_id"] == f"cik:{tag.lower()}"
            # second mention, same cik from news → strong-id hit, same entity
            r2 = await resolve_entity(store, name=f"Acme {tag}", kind="company",
                                      strong_ids={"cik": tag}, source_key="news")
            assert r2["entity_id"] == r1["entity_id"] and r2["method"] == "strong_id"
            # third mention, NO strong id, same normalized name → exact-norm single hit
            r3 = await resolve_entity(store, name=f"ACME  {tag}", kind="company")
            assert r3["entity_id"] == r1["entity_id"] and r3["method"] == "exact_norm"
        finally:
            await store.close()
    asyncio.run(body())


def test_resolve_entity_no_llm_ambiguous_is_unresolved_against_db(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            tag = uuid.uuid4().hex[:10]
            nm = f"Apex {tag}"
            # seed TWO distinct companies with the same normalized name
            a, b = _uid("yc"), _uid("yc")
            await store.upsert_entity(a, "company", nm)
            await store.upsert_entity(b, "company", nm)
            # a third mention with no LLM → NEVER a false merge; a flagged unresolved node
            r = await resolve_entity(store, name=nm, kind="company", source_key="news", llm=None)
            assert r["method"] == "unresolved" and r["is_new"]
            # verify the flag persisted
            pool = await store._get_pool()
            async with pool.acquire() as conn:
                facets = await conn.fetchval(
                    "SELECT facets FROM rs_entity WHERE entity_id=$1", r["entity_id"])
            import json as _json
            fd = facets if isinstance(facets, dict) else _json.loads(facets)
            assert fd["resolution"] == "unresolved_candidate"
            assert set(fd["candidate_ids"]) == {a, b}
        finally:
            await store.close()
    asyncio.run(body())


def test_conflict_resolution_and_winner_preferred_read_against_db() -> None:
    async def body():
        store = ClaimGraphStore(DSN)
        try:
            subj = _uid("yc")
            await store.upsert_entity(subj, "company", "Acme")
            # two contradictory funding claims, static bucket
            c_news = await store.upsert_claim(subject_id=subj, predicate="raised_funding",
                                              object_kind="value", object_value="$10M",
                                              confidence=0.9)
            c_filing = await store.upsert_claim(subject_id=subj, predicate="raised_funding",
                                                object_kind="value", object_value="$12M",
                                                confidence=0.5)
            await store.add_evidence(c_news, _uid("doc"), "b1", "reportedly raised $10M",
                                     source_key="news", authority_tier=4,
                                     evidence_kind="analysis")
            await store.add_evidence(c_filing, _uid("doc"), "b2", "raised $12M per Form D",
                                     source_key="edgar", authority_tier=6,
                                     evidence_kind="primary_filing")
            written = await resolve_conflicts(store, subject_id=subj, predicate="raised_funding",
                                              tenant_id="demo", authority_policy=_POLICY)
            assert len(written) == 1
            assert written[0]["winning_claim_id"] == c_filing            # controlling wins
            assert written[0]["conflict_claim_ids"] == [c_news]

            # winner-preferred read returns ONLY the winner + the loser as an alternate
            rows = await store.resolved_entity_claims(subj, predicates=["raised_funding"])
            funding = [r for r in rows if r["predicate"] == "raised_funding"]
            assert len(funding) == 1
            w = funding[0]
            assert w["claim_id"] == c_filing and w["is_resolved"] is True
            assert w["object_value"] == "$12M"
            assert [a["claim_id"] for a in w["alternates"]] == [c_news]
            assert w["alternates"][0]["object_value"] == "$10M"

            # the loser is NOT retracted — still queryable via the plain read
            plain = await store.entity_claims(subj, predicates=["raised_funding"])
            assert {c["claim_id"] for c in plain} == {c_news, c_filing}
        finally:
            await store.close()
    asyncio.run(body())
