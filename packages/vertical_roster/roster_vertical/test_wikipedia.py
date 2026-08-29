"""Offline tests for the Wikipedia connector — NO network (pages injected).

Prove discover→list→fetch builds a grounded encyclopedic reference (title + injected extract + rendered
categories), facet extraction (source_kind=reference → verified_structured, the secondary-source tier:
a real boost but NON-controlling), and that fixtures with an embedded "extract" render without any fetch.
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, wikipedia_doc as wp
from .authority import TechAuthorityPolicy
from .connectors.wikipedia import WikipediaConnector

_TRANSFORMER = {
    "pageid": 55488099,
    "title": "Transformer (deep learning architecture)",
    "extract": "The transformer is a deep learning architecture based on the multi-head "
               "attention mechanism, introduced in the 2017 paper 'Attention Is All You Need'. "
               "It has since become foundational to modern large language models.",
    "categories": [
        {"title": "Category:Neural network architectures"},
        {"title": "Category:2017 introductions"},
        {"title": "Category:Hidden categories"},
    ],
}


def test_discover_list_fetch_reference():
    c = WikipediaConnector(pages=[_TRANSFORMER])
    ents = asyncio.run(c.discover_entities({}))
    assert {e.native_id for e in ents} == {"55488099"}
    ent = ents[0]
    assert ent.title == "Transformer (deep learning architecture)"
    docs = asyncio.run(c.list_documents(ent))
    md = asyncio.run(c.fetch_artifact(docs[0])).decode()
    # title + injected extract text render (no network)
    assert "Transformer (deep learning architecture) — Wikipedia" in md
    assert "multi-head attention mechanism" in md
    assert "Attention Is All You Need" in md
    # categories rendered, hidden ones dropped
    assert "## Categories" in md
    assert "Neural network architectures" in md
    assert "Hidden categories" not in md


def test_facets_and_reference_tier():
    f = wp.facets(_TRANSFORMER)
    assert f["source_kind"] == "reference"
    assert f["entity_type"] == "article"
    assert f["source_country"] == "global"
    assert f["wikipedia_pageid"] == "55488099"
    assert f["category"] == "neural network architectures"
    # secondary-source reference → verified_structured: a real boost (above press) but NON-controlling
    # (only a filing is controlling — it grounds history/genesis, never overrides a filing/patent/paper).
    kind = evidence_kind.classify("wikipedia", f)
    assert kind == "verified_structured"
    pol = TechAuthorityPolicy()
    assert pol.rank(kind) > 0 and not pol.is_controlling(kind)


def test_no_year_without_clean_four_digit():
    # NO year unless a clean 4-digit is explicitly present on the record
    assert "year" not in wp.facets(_TRANSFORMER)
    assert wp.facets({"pageid": 1, "title": "X", "year": "2015"}).get("year") == "2015"
