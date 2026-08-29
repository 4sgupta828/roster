"""Tests for the ranking upgrades that put roster at-par-or-better than factra's
prod lexical leg (ts_rank_cd, which has neither IDF nor length normalization)."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.retrieval.scoring import bm25_rank, signal


def test_bm25_idf_rewards_rare_terms() -> None:
    # "zebra" is rare (1 doc), "apple" common (all docs). The doc with the rare
    # term should win — ts_rank without IDF would not distinguish this.
    docs = [
        ("rare", "zebra apple orchard"),
        ("c1", "apple apple pie"),
        ("c2", "apple apple tart"),
        ("c3", "apple apple cake"),
    ]
    assert bm25_rank("zebra apple", docs)[0] == "rare"


def test_bm25_length_normalization() -> None:
    # Both contain "target" once; the shorter doc should rank higher.
    docs = [
        ("short", "target"),
        ("long", "target " + "filler " * 60),
    ]
    assert bm25_rank("target", docs)[0] == "short"


def test_signal_scores() -> None:
    assert signal("Report") == 0.3                       # stub / header
    assert signal("The annual report describes the approved figures in detail.") == 1.0
    assert signal(" ".join(str(i) for i in range(30))) == 0.5   # mostly numbers


def test_signal_demotes_boilerplate_below_content() -> None:
    # A short boilerplate "Report" header vs a real content block, both matching
    # "report" lexically. BM25 alone would favor the shorter stub; signal demotion
    # must push the content block above it.
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="stub", document_id="d", tenant_id="t", text="Report"))
    src.add(IndexedBlock(block_id="content", document_id="d", tenant_id="t",
                         text="The annual report describes the approved return figures "
                              "in detail across several sections of the document."))
    hits = asyncio.run(src.search(RetrievalRequest(query="report", tenant_id="t")))
    assert [h.block_id for h in hits][0] == "content"
    assert dict((h.block_id, h.extra["signal"]) for h in hits)["stub"] == 0.3
