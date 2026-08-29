"""Offline tests for RRF fusion + the in-memory hybrid retrieval source.

Includes the TENANT-ISOLATION security probe (plan P2 gate) and generic facet
filtering. Domain-neutral throughout.
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator, RetrievalRequest
from roster_kernel.contract.protocols import RetrievalSource
from roster_kernel.retrieval.fusion import rrf_fuse
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


# ---- fusion --------------------------------------------------------------

def test_rrf_rewards_agreement_across_legs() -> None:
    fused = rrf_fuse({"lexical": ["a", "b", "c"], "dense": ["b", "a", "d"]})
    ids = [i for i, _, _ in fused]
    assert ids[0] in ("a", "b")            # both legs rank a,b high
    top = dict((i, legs) for i, _, legs in fused)
    assert top["a"] == ("dense", "lexical") # a hit in both legs
    assert top["c"] == ("lexical",)


def test_rrf_deterministic_tiebreak() -> None:
    a = rrf_fuse({"l": ["x", "y"]})
    b = rrf_fuse({"l": ["x", "y"]})
    assert a == b


# ---- helpers -------------------------------------------------------------

def _src_with_two_tenants() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="A1", document_id="dA", text="alpha shared term",
                         tenant_id="A", embedding=(1.0, 0.0), facets={"region": "north"}))
    src.add(IndexedBlock(block_id="A2", document_id="dA", text="beta only",
                         tenant_id="A", embedding=(0.0, 1.0), facets={"region": "south"}))
    src.add(IndexedBlock(block_id="B1", document_id="dB", text="alpha shared term secret",
                         tenant_id="B", embedding=(1.0, 0.0), facets={"region": "north"}))
    return src


def _run(src, req):
    return asyncio.run(src.search(req))


# ---- tenant isolation (SECURITY probe) -----------------------------------

def test_tenant_isolation_never_leaks_other_tenant() -> None:
    src = _src_with_two_tenants()
    hits = _run(src, RetrievalRequest(query="alpha shared term", tenant_id="A",
                                      query_embedding=[1.0, 0.0], k=10))
    ids = {h.block_id for h in hits}
    assert "A1" in ids
    assert "B1" not in ids                 # tenant B's block must NEVER surface for A
    assert all(h.document_id != "dB" for h in hits)


def test_workspace_scoped_block_hidden_from_corpus_query() -> None:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="C", document_id="d", text="corpus wide", tenant_id="A"))
    src.add(IndexedBlock(block_id="W", document_id="d", text="corpus wide", tenant_id="A",
                         workspace_id="ws1"))
    # corpus query (no workspace) sees only tenant-wide block
    corpus = _run(src, RetrievalRequest(query="corpus wide", tenant_id="A"))
    assert {h.block_id for h in corpus} == {"C"}
    # workspace query sees tenant-wide + its own workspace block
    ws = _run(src, RetrievalRequest(query="corpus wide", tenant_id="A", workspace_id="ws1"))
    assert {h.block_id for h in ws} == {"C", "W"}


# ---- facet filtering (generic, IN-semantics) -----------------------------

def test_facet_filter_single_and_set() -> None:
    src = _src_with_two_tenants()
    north = _run(src, RetrievalRequest(query="alpha beta shared term only", tenant_id="A",
                                       facets={"region": "north"}))
    assert {h.block_id for h in north} == {"A1"}
    both = _run(src, RetrievalRequest(query="alpha beta shared term only", tenant_id="A",
                                      facets={"region": ("north", "south")}))
    assert {h.block_id for h in both} == {"A1", "A2"}


# ---- hybrid behavior -----------------------------------------------------

def test_dense_leg_surfaces_semantic_match_and_legs_traced() -> None:
    src = InMemoryRetrievalSource()
    # "P" matches lexically; "Q" matches only via the query embedding direction
    src.add(IndexedBlock(block_id="P", document_id="d", text="keyword here", tenant_id="A",
                         embedding=(0.0, 1.0)))
    src.add(IndexedBlock(block_id="Q", document_id="d", text="unrelated words", tenant_id="A",
                         embedding=(1.0, 0.0)))
    hits = _run(src, RetrievalRequest(query="keyword", tenant_id="A", query_embedding=[1.0, 0.0]))
    ids = {h.block_id for h in hits}
    assert ids == {"P", "Q"}               # P via lexical, Q via dense
    legs = {h.block_id: h.legs for h in hits}
    assert legs["P"] == ("lexical",)
    assert legs["Q"] == ("dense",)


def test_source_satisfies_protocol() -> None:
    assert isinstance(InMemoryRetrievalSource(), RetrievalSource)
