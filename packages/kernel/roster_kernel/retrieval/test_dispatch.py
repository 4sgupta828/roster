"""Offline tests for multi-query retrieval dispatch (recall + repeat bonus)."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.retrieval.dispatch import multi_query_retrieve
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


def _src() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="eq", document_id="d", tenant_id="t",
                         text="The approved return on equity was set at nine point six percent."))
    src.add(IndexedBlock(block_id="capstruct", document_id="d", tenant_id="t",
                         text="The capital structure reflects an equity ratio consistent with peers."))
    return src


def _run(src, req, variants):
    return asyncio.run(multi_query_retrieve(src, req, variants))


def test_multi_query_improves_recall() -> None:
    src = _src()
    base = RetrievalRequest(query="return on equity", tenant_id="t", k=10)
    # the original query alone misses the capital-structure block; a variant finds it
    ids = {h.block_id for h in _run(src, base, ["equity ratio capital structure"])}
    assert ids == {"eq", "capstruct"}


def _skewed_src() -> InMemoryRetrievalSource:
    """4 'bulk' blocks matching the ORIGINAL query + 1 each from 'srcx'/'srcy' matching a VARIANT
    (so the thin sources survive into the fused pool via a different leg — how recall really works).
    source_key is derived from the document_id prefix by the cap."""
    src = InMemoryRetrievalSource()
    for i in range(4):
        src.add(IndexedBlock(block_id=f"e{i}", document_id=f"bulk:{i}", tenant_id="t",
                             text="alpha alpha alpha doc1"))
    src.add(IndexedBlock(block_id="a0", document_id="srcx:0", tenant_id="t", text="beta beta doc2"))
    src.add(IndexedBlock(block_id="h0", document_id="srcy:0", tenant_id="t", text="beta beta doc3"))
    return src


def test_diversity_cap_off_is_byte_identical() -> None:
    src = _skewed_src()
    base = RetrievalRequest(query="alpha", tenant_id="t", k=4)
    no_cap = [h.block_id for h in asyncio.run(multi_query_retrieve(src, base, ["beta"]))]
    explicit_none = [h.block_id for h in asyncio.run(
        multi_query_retrieve(src, base, ["beta"], source_cap_frac=None))]
    assert no_cap == explicit_none                      # None → unchanged selection
    assert len(no_cap) == 4


def test_diversity_cap_limits_one_source() -> None:
    src = _skewed_src()
    base = RetrievalRequest(query="alpha", tenant_id="t", k=4)
    # base "alpha" retrieves the 4 bulk blocks; variant "beta" retrieves srcx+srcy → fused pool has all 6.
    hits = asyncio.run(multi_query_retrieve(src, base, ["beta"], source_cap_frac=0.5))
    srcs = [h.document_id.split(":", 1)[0] for h in hits]
    assert len(hits) == 4
    assert srcs.count("bulk") <= 2                     # cap = ceil(4*0.5) = 2
    assert "srcx" in srcs and "srcy" in srcs             # reserved seats for the thin sources


def test_diversity_cap_backfills_when_sources_scarce() -> None:
    """Only one source matches → the cap must NOT starve the result; it backfills to preserve recall."""
    src = InMemoryRetrievalSource()
    for i in range(5):
        src.add(IndexedBlock(block_id=f"e{i}", document_id=f"bulk:{i}", tenant_id="t",
                             text="alpha alpha alpha"))
    base = RetrievalRequest(query="alpha", tenant_id="t", k=4)
    hits = asyncio.run(multi_query_retrieve(src, base, ["alpha"], source_cap_frac=0.5))
    assert len(hits) == 4                               # backfilled to k despite the 2-per-source cap


def test_repeat_bonus_prefers_multi_variant_hits() -> None:
    # "both" is retrieved by the original AND the variant (rank 2 in each);
    # "a1"/"b1" are each retrieved by only one query (rank 1). The repeat bonus +
    # cross-query agreement lifts "both" to the top over the single-query rank-1s.
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="both", document_id="d", tenant_id="t", text="alpha beta together"))
    src.add(IndexedBlock(block_id="a1", document_id="d", tenant_id="t", text="alpha alpha alpha"))
    src.add(IndexedBlock(block_id="b1", document_id="d", tenant_id="t", text="beta beta beta"))
    base = RetrievalRequest(query="alpha", tenant_id="t", k=10)
    hits = _run(src, base, ["beta"])
    assert hits[0].block_id == "both"
    assert hits[0].extra["queries_hit"] == 2
