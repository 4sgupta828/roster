"""Retrieval eval — measures ranking quality (recall@k, MRR, decoy avoidance).

This is how we claim "at par or better than factra" with evidence rather than
assertion (Rule 3). Pure math over a ranked list of block ids; the gold relevant
set is held out from any prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from roster_kernel.contract.dto import FacetFilter


@dataclass(frozen=True)
class RetrievalCase:
    id: str
    query: str
    relevant: frozenset[str]                 # gold relevant block ids
    facets: FacetFilter = field(default_factory=dict)
    decoys: frozenset[str] = frozenset()     # must NOT outrank a relevant block


@dataclass
class RetrievalScore:
    case_id: str
    recall_at_k: float
    mrr: float
    first_relevant_rank: int | None
    decoy_avoided: bool


def score_retrieval(case: RetrievalCase, ranked_ids: list[str], *, k: int = 10) -> RetrievalScore:
    top = ranked_ids[:k]
    hit = len(set(top) & case.relevant)
    recall = hit / len(case.relevant) if case.relevant else 0.0

    first_rel = None
    mrr = 0.0
    for i, bid in enumerate(ranked_ids, start=1):
        if bid in case.relevant:
            first_rel = i
            mrr = 1.0 / i
            break

    # decoy avoided: no decoy ranks above the first relevant block
    first_decoy = next((i for i, b in enumerate(ranked_ids, 1) if b in case.decoys), None)
    decoy_avoided = (
        not case.decoys
        or first_rel is not None and (first_decoy is None or first_rel < first_decoy)
    )
    return RetrievalScore(case.id, recall, mrr, first_rel, decoy_avoided)


def aggregate(scores: list[RetrievalScore]) -> dict[str, float]:
    n = len(scores) or 1
    return {
        "mean_recall_at_k": sum(s.recall_at_k for s in scores) / n,
        "mean_mrr": sum(s.mrr for s in scores) / n,
        "decoy_avoid_rate": sum(1 for s in scores if s.decoy_avoided) / n,
    }
