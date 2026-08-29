"""MultiSourceRetriever — answer from ANY combination of sources.

Itself a RetrievalSource, so the research loop is unchanged: it just receives a
composite. It queries each member source (each applies its own tenant/facet
scoping + internal ranking), fuses the per-source rankings with weighted RRF, and
returns unified, source-tagged BlockHits. Its block loader dispatches across the
members, so the span-check gate verifies a claim against whichever source it came
from — uniform provenance across corpus / workspace / web / (future) datasets.

Platform pattern: a "research over corpus + web + my workspace" request is just
MultiSourceRetriever({...the chosen sources...}). Which sources apply can be
chosen by a vertical ScopeRouter; the default is "all given, by declared cover".
"""
from __future__ import annotations

from collections import Counter

from roster_kernel.contract.dto import BlockHit, Capability, FacetFilter, RetrievalRequest
from roster_kernel.contract.protocols import RetrievalSource
from roster_kernel.retrieval.fusion import RRF_K


class MultiSourceRetriever:
    key = "multi"

    def __init__(self, sources: dict[str, RetrievalSource], *,
                 weights: dict[str, float] | None = None):
        if not sources:
            raise ValueError("MultiSourceRetriever needs at least one source")
        self._sources = dict(sources)
        self._weights = weights or {}

    def capabilities(self) -> frozenset[Capability]:
        caps: set[Capability] = set()
        for s in self._sources.values():
            caps |= set(s.capabilities())
        return frozenset(caps)

    def covers(self) -> FacetFilter:
        return {}                                   # union coverage

    def make_block_loader(self, tenant_id: str, workspace_id: str | None = None):
        loaders = [s.make_block_loader(tenant_id, workspace_id) for s in self._sources.values()]
        def _load(document_id: str, block_id: str) -> str | None:
            for load in loaders:                    # first source that owns it (fail-closed)
                text = load(document_id, block_id)
                if text is not None:
                    return text
            return None
        return _load

    async def search(self, req: RetrievalRequest) -> list[BlockHit]:
        import asyncio
        by_cid: dict[str, BlockHit] = {}
        scores: dict[str, float] = {}
        appears: Counter[str] = Counter()
        self.failed_sources: dict[str, str] = {}   # key -> error, for observability

        # Query all sources CONCURRENTLY (fan-out) — a slow source (e.g. web/Tavily's 18s
        # timeout) overlaps the fast corpus search instead of adding to it serially. A flaky
        # source must never kill a query the others can answer (return_exceptions=True).
        keys = list(self._sources)
        results = await asyncio.gather(
            *(self._sources[k].search(req) for k in keys), return_exceptions=True)
        for skey, res in zip(keys, results):
            if isinstance(res, Exception):
                self.failed_sources[skey] = f"{type(res).__name__}: {res}"
                continue
            w = self._weights.get(skey, 1.0)
            for rank, h in enumerate(res, start=1):
                cid = f"{skey}::{h.block_id}"
                by_cid.setdefault(cid, h)
                scores[cid] = scores.get(cid, 0.0) + w * (1.0 / (RRF_K + rank))
                appears[cid] += 1

        if not scores:
            return []
        ranked = sorted(scores, key=lambda c: (-scores[c], c))[: req.k]
        out: list[BlockHit] = []
        for cid in ranked:
            h = by_cid[cid]
            out.append(BlockHit(
                document_id=h.document_id, block_id=h.block_id, text=h.text,
                score=scores[cid], facets=dict(h.facets), locator=h.locator,
                document_title=h.document_title, content_type=h.content_type,
                source_key=h.source_key, legs=h.legs,
                extra={**h.extra, "source": h.source_key},
            ))
        return out
