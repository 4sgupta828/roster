"""D-4 (Noesis IN): the country boost is TIER-AWARE — region preference must never let weak
regional evidence displace stronger global evidence inside the compose cap."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from roster_kernel.research.react import _rank_claims_by_relevance


@dataclass
class _C:
    text: str
    evidence_kind: str = ""
    facets: dict = field(default_factory=dict)


class _SameEmbedder:
    """All texts embed identically → cosine ties; only the boosts decide the order."""
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


_RANK = {"guideline": 6, "systematic_review": 6, "rct": 5, "cohort": 3, "case_report": 1}


def _rank(claims, top, country_boost=None, ranker=_RANK.get):
    return asyncio.run(_rank_claims_by_relevance(
        "q", claims, _SameEmbedder(), top,
        evidence_ranker=ranker, country_boost=country_boost))


def test_global_guideline_beats_in_case_report_despite_boost():
    claims = [
        _C("IN case report", "case_report", {"source_country": "IN"}),
        _C("global systematic review", "systematic_review", {}),
        _C("global guideline", "guideline", {}),
    ]
    kept = _rank(claims, top=2, country_boost={"IN"})
    texts = [c.text for c in kept]
    assert "IN case report" not in texts, texts   # tier-scaled boost can't lift rank-1 past rank-6


def test_in_guideline_beats_global_cohort_the_intended_win():
    claims = [
        _C("global cohort", "cohort", {}),
        _C("IN guideline", "guideline", {"source_country": "IN"}),
    ]
    kept = _rank(claims, top=1, country_boost={"IN"})
    assert kept[0].text == "IN guideline"


def test_no_ranker_uses_conservative_half_weight_and_no_boost_is_noop():
    claims = [_C("IN a", facets={"source_country": "IN"}), _C("global b")]
    kept = _rank(claims, top=1, country_boost={"IN"}, ranker=None)
    assert kept[0].text == "IN a"                 # still boosts on ties, conservatively
    kept2 = _rank(claims, top=1, country_boost=None, ranker=None)
    assert kept2[0].text == "IN a"                # no boost → original order preserved