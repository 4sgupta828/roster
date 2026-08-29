"""Multi-query retrieval dispatch — the recall booster (parity with factra).

Runs the original query plus agent-generated reformulations against a source and
fuses across them with weighted RRF: the original query weighs more, and a block
retrieved by multiple variants gets a repeat bonus. Mirrors factra's dispatcher
(original weight 1.5, repeat bonus 0.15) — kept in the kernel, engine-agnostic.
Reformulation itself is the agent's job (LLM); this fuses the results.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace

from roster_kernel.contract.dto import BlockHit, RetrievalRequest
from roster_kernel.contract.protocols import RetrievalSource
from roster_kernel.providers.embeddings import Embedder
from roster_kernel.retrieval.fusion import RRF_K

ORIGINAL_WEIGHT = 1.5
REPEAT_BONUS = 0.15


async def multi_query_retrieve(
    source: RetrievalSource,
    base_req: RetrievalRequest,
    variants: list[str],
    *,
    embedder: Embedder | None = None,
    original_weight: float = ORIGINAL_WEIGHT,
    repeat_bonus: float = REPEAT_BONUS,
    source_cap_frac: float | None = None,
) -> list[BlockHit]:
    import asyncio
    # Reuse the original query's embedding (already computed by the caller) — no double-embed —
    # and embed ALL variants in ONE batched call, off the event loop. Then search every variant
    # CONCURRENTLY. Was: an embed + a search serially per variant (N+1 blocking round-trips).
    variant_qs = [v for v in variants if v and v != base_req.query]
    weighted: list[tuple[str, float, list[float]]] = [
        (base_req.query, original_weight, base_req.query_embedding)]
    if variant_qs:
        if embedder is not None:
            vecs = await asyncio.to_thread(embedder.embed, variant_qs)
            weighted += [(v, 1.0, list(vec)) for v, vec in zip(variant_qs, vecs)]
        else:
            # no embedder → reuse the base embedding (may be None; source matches by string in tests)
            weighted += [(v, 1.0, base_req.query_embedding) for v in variant_qs]

    reqs = [(w, replace(base_req, query=q, query_embedding=emb)) for q, w, emb in weighted]
    results = await asyncio.gather(
        *(source.search(r) for _, r in reqs), return_exceptions=True)

    hit_by_id: dict[str, BlockHit] = {}
    scores: dict[str, float] = {}
    appears: Counter[str] = Counter()
    for (w, _req), hits in zip(reqs, results):
        if isinstance(hits, Exception):
            continue
        for rank, h in enumerate(hits, start=1):
            hit_by_id.setdefault(h.block_id, h)
            scores[h.block_id] = scores.get(h.block_id, 0.0) + w * (1.0 / (RRF_K + rank))
            appears[h.block_id] += 1

    for bid in scores:
        scores[bid] *= (1.0 + repeat_bonus * (appears[bid] - 1))

    order = sorted(scores, key=lambda b: (-scores[b], b))   # full fused order, best-first
    k = base_req.k
    # SOURCE-DIVERSITY CAP (flag-gated by the caller; None → byte-identical top-k). A corpus skewed
    # heavily (e.g. 200:1) toward one source_key would otherwise fill every top-k seat with that source
    # on a broad query, starving the thinner sources BEFORE the ranker ever sees them.
    # The cap admits at most ceil(k * frac) blocks per source_key, then BACKFILLS from the overflow in
    # score order if too few distinct sources exist — so recall is never reduced, only rebalanced.
    if source_cap_frac and source_cap_frac > 0 and len(order) > k:
        import math
        cap = max(1, math.ceil(k * source_cap_frac))

        def _src(bid: str) -> str:
            h = hit_by_id[bid]
            return (h.source_key or (h.document_id.split(":", 1)[0] if ":" in h.document_id else "")) or "?"

        selected: list[str] = []
        overflow: list[str] = []
        per: Counter[str] = Counter()
        for bid in order:
            if per[_src(bid)] < cap:
                selected.append(bid)
                per[_src(bid)] += 1
            else:
                overflow.append(bid)
            if len(selected) >= k:
                break
        if len(selected) < k:                       # too few distinct sources → backfill by score
            selected += overflow[: k - len(selected)]
        ranked = selected[:k]
    else:
        ranked = order[:k]
    return [replace(hit_by_id[bid], score=scores[bid],
                    extra={**hit_by_id[bid].extra, "queries_hit": appears[bid]})
            for bid in ranked]
