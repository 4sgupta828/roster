"""Offline tests for the Semantic Scholar connector — NO network (papers injected).

Prove the discover→list→fetch chain, the facet normalization, and that the tier logic
(`evidence_kind.classify`) grades a peer-reviewed venue as `verified_structured` and an
arXiv-only preprint as `technical_signal` — the same discipline as OpenAlex.
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, semantic_scholar_doc as s2doc
from .connectors import SemanticScholarConnector

# a peer-reviewed conference paper (NeurIPS) with a DOI + arXiv id + citations
_REVIEWED = {
    "paperId": "abc123", "title": "Attention Is All You Need",
    "abstract": "The dominant sequence transduction models are based on recurrent networks.",
    "year": 2017, "venue": "NeurIPS",
    "externalIds": {"DOI": "10.5555/xxxx", "ArXiv": "1706.03762"},
    "citationCount": 100000, "authors": [{"name": "A. Vaswani"}, {"name": "N. Shazeer"}],
    "publicationTypes": ["JournalArticle", "Conference"],
    "publicationVenue": {"type": "conference", "name": "NeurIPS"},
}
# an arXiv-only preprint: no venue, Preprint type → NOT peer-reviewed
_PREPRINT = {
    "paperId": "def456", "title": "Some New LLM Trick",
    "abstract": "We propose a new trick.", "year": 2024, "venue": "",
    "externalIds": {"ArXiv": "2401.00001"},
    "citationCount": 3, "authors": [{"name": "J. Doe"}],
    "publicationTypes": ["Preprint"], "publicationVenue": None,
}


def test_discover_list_fetch_offline():
    c = SemanticScholarConnector(papers=[_REVIEWED, _PREPRINT])
    ents = asyncio.run(c.discover_entities({}))          # no query + injected → returns fixtures
    assert {e.native_id for e in ents} == {"abc123", "def456"}
    ent = next(e for e in ents if e.native_id == "abc123")
    docs = asyncio.run(c.list_documents(ent))
    assert docs and docs[0].content_type == "text/markdown"
    md = asyncio.run(c.fetch_artifact(docs[0])).decode()
    assert "Attention Is All You Need" in md and "NeurIPS" in md and "1706.03762" in md


def test_facets_and_tier_reviewed_vs_preprint():
    fr = s2doc.facets(_REVIEWED)
    assert fr["source_kind"] == "paper" and fr["is_peer_reviewed"] == "true"
    assert fr["doi"] == "10.5555/xxxx" and fr["arxiv_id"] == "1706.03762"
    assert evidence_kind.classify("semantic_scholar", fr) == "verified_structured"

    fp = s2doc.facets(_PREPRINT)
    assert fp["is_peer_reviewed"] == "false"
    assert evidence_kind.classify("semantic_scholar", fp) == "technical_signal"


def test_connector_registered_in_manifest():
    from .manifest import build_manifest
    assert "semantic_scholar" in build_manifest().connectors
