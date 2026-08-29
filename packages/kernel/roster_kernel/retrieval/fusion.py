"""Reciprocal Rank Fusion — engine-agnostic, pure.

Combines N ranked legs (lexical, dense, structured, …) into one ranking:
    score(id) = Σ_legs 1 / (k + rank_in_leg)   (rank is 1-based)
RRF is robust to a weak partner leg (why a dense leg can carry a coarse lexical
one). Kept in the kernel — not hidden in a backend — so legs stay inspectable
and any engine (Postgres/OpenSearch/…) maps behind the same retrieval port.
"""
from __future__ import annotations

RRF_K = 60


def rrf_fuse(legs: dict[str, list[str]], *, k: int = RRF_K) -> list[tuple[str, float, tuple[str, ...]]]:
    """legs: leg_name → ids best-first. Returns (id, score, legs_hit) best-first.

    Deterministic: ties broken by id so runs are reproducible.
    """
    scores: dict[str, float] = {}
    hits: dict[str, set[str]] = {}
    for leg_name, ids in legs.items():
        for rank, _id in enumerate(ids, start=1):
            scores[_id] = scores.get(_id, 0.0) + 1.0 / (k + rank)
            hits.setdefault(_id, set()).add(leg_name)
    fused = [(i, scores[i], tuple(sorted(hits[i]))) for i in scores]
    fused.sort(key=lambda t: (-t[1], t[0]))
    return fused
