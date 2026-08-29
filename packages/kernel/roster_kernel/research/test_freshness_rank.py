"""Freshness-ranking unit tests (flag ROSTER_FRESHNESS_RANKING seam).

The recency term must (a) stay INERT for non-controlling evidence when no policy is passed
(byte-identical OFF), and (b) re-order same-tier claims by year when a tech-style policy IS passed
(min_rank=0, short horizon). Uses identical claim text so cosine relevance ties and recency decides.
"""
from __future__ import annotations

import asyncio

from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.research.react import VerifiedClaim, _rank_claims_by_relevance

_TECH_RANK = {"sentiment_signal": 1, "technical_signal": 2, "analysis": 4,
              "verified_structured": 5, "primary_filing": 6}


def _rank(claims, freshness):
    # top=1 so the pool (2) is actually SCORED (len<=top early-returns unscored)
    return asyncio.run(_rank_claims_by_relevance(
        "which is newer", claims, FakeEmbedder(dim=16), top=1,
        evidence_ranker=lambda k: _TECH_RANK.get(k, 0), freshness=freshness))


def _pair():
    # identical text → identical cosine → recency is the ONLY differentiator; both non-controlling
    return [VerifiedClaim(text="frontier model landscape", atom_id="old", quote="q",
                          evidence_kind="technical_signal", facets={"year": "2020"}),
            VerifiedClaim(text="frontier model landscape", atom_id="new", quote="q",
                          evidence_kind="technical_signal", facets={"year": "2026"})]


def test_off_is_recency_inert_for_non_controlling():
    # No policy → the legacy recency term needs rank>=6; technical_signal (2) never qualifies →
    # tie on cosine → stable order preserves input (2020 first). Byte-identical to today.
    top = _rank(_pair(), None)
    assert top[0].atom_id == "old"


def test_tech_policy_surfaces_the_newer_claim():
    # min_rank=0 → recency applies to technical_signal; 2026 (age 0) beats 2020 (age 6, past horizon).
    top = _rank(_pair(), {"min_rank": 0, "weight": 0.22, "horizon_years": 2})
    assert top[0].atom_id == "new"


def test_unknown_year_never_demotes():
    claims = [VerifiedClaim(text="x", atom_id="dated", quote="q", evidence_kind="technical_signal",
                            facets={"year": "2026"}),
              VerifiedClaim(text="x", atom_id="undated", quote="q", evidence_kind="technical_signal",
                            facets={})]
    # the undated claim gets no recency term (absence is a no-op); the dated-fresh one wins, and
    # neither errors — ranking must never break on a missing year.
    top = _rank(claims, {"min_rank": 0, "weight": 0.22, "horizon_years": 2})
    assert top[0].atom_id == "dated"
