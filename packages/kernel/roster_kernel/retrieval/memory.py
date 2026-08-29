"""InMemoryRetrievalSource — the reference RetrievalSource impl (offline).

Implements the kernel retrieval port with a lexical leg + a dense (cosine) leg
fused by RRF, generic facet IN-filters, and FIRST-CLASS tenant/workspace
isolation. The PostgresRetrievalSource (pgvector HNSW + tsvector + SQL RRF) is a
later adapter behind this same port; this one keeps the whole dev/eval loop
offline and is the tenant-isolation conformance probe.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from roster_kernel.contract.dto import (
    BlockHit,
    Capability,
    Facets,
    FacetFilter,
    Locator,
    RetrievalRequest,
)
from roster_kernel.retrieval.rank import Candidate, rank_candidates


def _facets_match(have: Facets, want: FacetFilter) -> bool:
    for key, allowed in want.items():
        allowed_set = (allowed,) if isinstance(allowed, str) else tuple(allowed)
        if have.get(key) not in allowed_set:
            return False
    return True


def _facets_excluded(have: Facets, banned: FacetFilter) -> bool:
    """True if the block should be DROPPED: it HAS the key with a banned value (untagged blocks pass —
    mirrors the Postgres `NOT (facets ? key) OR value <> ALL(...)` semantics)."""
    for key, vals in (banned or {}).items():
        banned_set = (vals,) if isinstance(vals, str) else tuple(vals)
        if key in have and have.get(key) in banned_set:
            return True
    return False


@dataclass
class IndexedBlock:
    block_id: str
    document_id: str
    text: str
    tenant_id: str
    embedding: tuple[float, ...] = ()
    workspace_id: str | None = None          # None = tenant-wide corpus
    facets: Facets = field(default_factory=dict)  # narrowing dims (denormalized to block)
    locator: Locator | None = None
    document_title: str = ""                  # provenance (display/scoping)
    content_type: str = ""
    source_key: str = ""


class InMemoryRetrievalSource:
    key = "memory"

    def __init__(self) -> None:
        self._blocks: list[IndexedBlock] = []

    def add(self, block: IndexedBlock) -> None:
        self._blocks.append(block)

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.RETRIEVAL, Capability.PRECISION})

    def covers(self) -> FacetFilter:
        return {}   # in-memory reference source covers everything

    def make_block_loader(self, tenant_id: str, workspace_id: str | None = None):
        """A block loader scoped to (tenant, workspace) — the provenance gate's
        isolation boundary. It can only return blocks visible to that scope, so a
        cited quote can never be verified against another tenant's document."""
        def _load(document_id: str, block_id: str) -> str | None:
            for b in self._blocks:
                if b.block_id != block_id or b.document_id != document_id:
                    continue
                if b.tenant_id != tenant_id:
                    return None
                if b.workspace_id is not None and b.workspace_id != workspace_id:
                    return None
                return b.text
            return None
        return _load

    def _visible(self, req: RetrievalRequest) -> list[IndexedBlock]:
        """Hard isolation + facet filter — applied BEFORE ranking, always."""
        out = []
        for b in self._blocks:
            if b.tenant_id != req.tenant_id:                 # tenant boundary (security)
                continue
            if b.workspace_id is not None and b.workspace_id != req.workspace_id:
                continue                                     # workspace-scoped: only its own workspace
            if not _facets_match(b.facets, req.facets):
                continue
            if _facets_excluded(b.facets, getattr(req, "exclude_facets", {})):
                continue
            out.append(b)
        return out

    async def search(self, req: RetrievalRequest) -> list[BlockHit]:
        # backend job: hard-filter to a candidate pool; ranking is shared (rank.py)
        cands = [
            Candidate(block_id=b.block_id, document_id=b.document_id, text=b.text,
                      embedding=b.embedding, facets=dict(b.facets), locator=b.locator,
                      document_title=b.document_title, content_type=b.content_type,
                      source_key=b.source_key)
            for b in self._visible(req)
        ]
        return rank_candidates(req.query, req.query_embedding, cands,
                               k=req.k, fetch_pool=req.fetch_pool)
