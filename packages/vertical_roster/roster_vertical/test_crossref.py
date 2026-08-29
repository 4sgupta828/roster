"""Offline tests for the Crossref connector — NO network (works injected).

Prove discover→list→fetch, DOI-keyed refs, JATS-abstract stripping, and that the tier logic
grades a journal-article as `verified_structured` and a `posted-content` preprint as
`technical_signal`.
"""
from __future__ import annotations

import asyncio

from . import crossref_doc as cr, evidence_kind
from .connectors import CrossrefConnector

_JOURNAL = {
    "DOI": "10.1145/3292500", "title": ["Deep Residual Learning for Image Recognition"],
    "abstract": "<jats:p>Deeper neural networks are more difficult to train.</jats:p>",
    "container-title": ["Communications of the ACM"], "issued": {"date-parts": [[2016, 6, 1]]},
    "author": [{"given": "Kaiming", "family": "He"}, {"given": "Xiangyu", "family": "Zhang"}],
    "is-referenced-by-count": 150000, "type": "journal-article", "publisher": "ACM",
}
_PREPRINT = {
    "DOI": "10.48550/arxiv.2005.14165", "title": ["Language Models are Few-Shot Learners"],
    "container-title": [], "issued": {"date-parts": [[2020]]},
    "author": [{"given": "Tom", "family": "Brown"}],
    "is-referenced-by-count": 40000, "type": "posted-content", "publisher": "arXiv",
}


def test_discover_list_fetch_and_abstract_strip():
    c = CrossrefConnector(works=[_JOURNAL, _PREPRINT])
    ents = asyncio.run(c.discover_entities({}))
    assert {e.native_id for e in ents} == {"10.1145/3292500", "10.48550/arxiv.2005.14165"}
    ent = next(e for e in ents if e.native_id == "10.1145/3292500")
    docs = asyncio.run(c.list_documents(ent))
    md = asyncio.run(c.fetch_artifact(docs[0])).decode()
    assert "Deep Residual Learning" in md and "Communications of the ACM" in md
    assert "<jats:p>" not in md and "Deeper neural networks" in md   # JATS tags stripped


def test_facets_and_tier():
    fj = cr.facets(_JOURNAL)
    assert fj["source_kind"] == "paper" and fj["is_peer_reviewed"] == "true" and fj["year"] == "2016"
    assert fj["doi"] == "10.1145/3292500"
    assert evidence_kind.classify("crossref", fj) == "verified_structured"

    fp = cr.facets(_PREPRINT)
    assert fp["is_peer_reviewed"] == "false" and fp["crossref_type"] == "posted-content"
    assert evidence_kind.classify("crossref", fp) == "technical_signal"


def test_registered_in_manifest():
    from .manifest import build_manifest
    assert "crossref" in build_manifest().connectors
