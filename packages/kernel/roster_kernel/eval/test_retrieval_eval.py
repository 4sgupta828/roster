"""Retrieval eval harness tests, incl. an adversarial decoy that BM25+signal beats."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.eval.retrieval_scoring import RetrievalCase, aggregate, score_retrieval
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


def test_scoring_math() -> None:
    case = RetrievalCase(id="c", query="q", relevant=frozenset({"r1", "r2"}),
                         decoys=frozenset({"d1"}))
    s = score_retrieval(case, ["d1", "r1", "x", "r2"], k=10)
    assert s.recall_at_k == 1.0
    assert s.first_relevant_rank == 2 and s.mrr == 0.5
    assert not s.decoy_avoided                # d1 (rank 1) beat r1 (rank 2)
    s2 = score_retrieval(case, ["r1", "d1", "r2"], k=10)
    assert s2.decoy_avoided                    # r1 outranks the decoy


def test_adversarial_decoy_is_beaten_by_ranking() -> None:
    # Relevant block answers the question; the decoy shares surface words but is
    # a low-signal boilerplate stub. BM25 length-norm + signal demotion must rank
    # the relevant block above the decoy.
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="answer", document_id="d", tenant_id="t",
                         text="The Commission approved a return on equity of nine point six "
                              "percent for the company in this proceeding."))
    src.add(IndexedBlock(block_id="decoy", document_id="d", tenant_id="t",
                         text="Return on Equity"))          # header stub, high overlap
    ranked = [h.block_id for h in asyncio.run(
        src.search(RetrievalRequest(query="approved return on equity", tenant_id="t")))]
    case = RetrievalCase(id="eq", query="approved return on equity",
                         relevant=frozenset({"answer"}), decoys=frozenset({"decoy"}))
    s = score_retrieval(case, ranked, k=5)
    assert s.recall_at_k == 1.0
    assert s.first_relevant_rank == 1
    assert s.decoy_avoided
    agg = aggregate([s])
    assert agg["mean_recall_at_k"] == 1.0 and agg["decoy_avoid_rate"] == 1.0
