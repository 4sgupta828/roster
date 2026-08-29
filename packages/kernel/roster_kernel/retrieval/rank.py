"""Shared candidate ranking — used by every RetrievalSource backend.

A backend's only job is to produce a filtered CANDIDATE POOL (hard filters +
ANN/lexical candidate-gen). The RANKING (BM25 lexical + dense cosine + RRF fusion
+ signal demotion) lives here, so the in-memory and Postgres sources rank
identically and the ranking is tested once. This is why the Postgres source can
be thin and trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from roster_kernel.contract.dto import BlockHit, Facets, Locator
from roster_kernel.retrieval.fusion import rrf_fuse
from roster_kernel.retrieval.scoring import bm25_rank, signal


@dataclass
class Candidate:
    block_id: str
    document_id: str
    text: str
    embedding: tuple[float, ...] = ()
    facets: Facets = field(default_factory=dict)
    locator: Locator | None = None
    document_title: str = ""
    content_type: str = ""
    source_key: str = ""


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b))   # both L2-normalized → cosine


def rank_candidates(
    query: str,
    query_embedding: list[float] | None,
    cands: list[Candidate],
    *,
    k: int = 20,
    fetch_pool: int = 60,
) -> list[BlockHit]:
    if not cands:
        return []
    by_id = {c.block_id: c for c in cands}

    lexical = bm25_rank(query, [(c.block_id, c.text) for c in cands])[:fetch_pool]

    dense: list[str] = []
    if query_embedding is not None:
        qv = tuple(query_embedding)
        scored = [(c.block_id, _cosine(qv, c.embedding)) for c in cands if c.embedding]
        scored = [(b, s) for b, s in scored if s > 0.0]
        scored.sort(key=lambda t: (-t[1], t[0]))
        dense = [b for b, _ in scored[:fetch_pool]]

    legs: dict[str, list[str]] = {}
    if lexical:
        legs["lexical"] = lexical
    if dense:
        legs["dense"] = dense
    if not legs:
        return []

    fused = rrf_fuse(legs)
    demoted = []
    for bid, score, legs_hit in fused:
        sig = signal(by_id[bid].text)
        demoted.append((bid, score * (0.4 + 0.6 * sig), legs_hit, sig))
    demoted.sort(key=lambda t: (-t[1], t[0]))

    out: list[BlockHit] = []
    for bid, score, legs_hit, sig in demoted[:k]:
        c = by_id[bid]
        out.append(BlockHit(
            document_id=c.document_id, block_id=bid, text=c.text, score=score,
            facets=dict(c.facets), locator=c.locator, document_title=c.document_title,
            content_type=c.content_type, source_key=c.source_key,
            legs=legs_hit, extra={"signal": sig},
        ))
    return out
