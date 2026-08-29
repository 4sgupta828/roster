"""Offline tests for the OpenReview connector — NO network (notes injected).

Prove discover→list→fetch, that API-v2 {"value": …} envelopes are unwrapped, and that the
peer-review facet drives the tier: an ACCEPTED-venue note grades `verified_structured` while a
"Submitted"-only note grades `technical_signal` (same tier as an unreviewed preprint).
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, openreview_doc as od
from .connectors.openreview import OpenReviewConnector

# Accepted venue → peer-reviewed. Nested {"value": …} envelopes throughout (API v2 shape).
_ACCEPTED = {
    "id": "abc123",
    "content": {
        "title": {"value": "Scaling Laws for Sparse Attention"},
        "abstract": {"value": "We study how sparse attention scales with model size."},
        "venue": {"value": "NeurIPS 2024 poster"},
        "keywords": {"value": ["attention", "scaling laws", "efficiency"]},
    },
}

# Un-accepted: venue merely says "Submitted" → NOT peer-reviewed.
_SUBMITTED = {
    "id": "def456",
    "content": {
        "title": {"value": "A New Optimizer"},
        "abstract": {"value": "A preliminary optimizer under review."},
        "venue": {"value": "Submitted to ICLR 2025"},
    },
}


def test_discover_list_fetch_and_envelope_unwrap():
    c = OpenReviewConnector(notes=[_ACCEPTED])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["abc123"]
    assert ents[0].title == "Scaling Laws for Sparse Attention"   # {"value": …} unwrapped
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "Scaling Laws for Sparse Attention" in md              # title
    assert "We study how sparse attention scales" in md           # abstract unwrapped
    assert "peer-reviewed: yes" in md                             # peer-review meta line
    assert "NeurIPS 2024 poster" in md                            # venue
    assert "scaling laws" in md.lower()                           # keywords rendered


def test_val_unwraps_both_shapes():
    # envelope form and bare-value form both resolve to the underlying value
    assert od._val({"title": {"value": "X"}}, "title") == "X"
    assert od._val({"title": "X"}, "title") == "X"
    assert od._val({}, "title") is None


def test_facets_and_tier_accepted_vs_submitted():
    fa = od.facets(_ACCEPTED)
    assert fa["source_kind"] == "paper"
    assert fa["entity_type"] == "paper"
    assert fa["source_country"] == "global"
    assert fa["venue"] == "NeurIPS 2024 poster"
    assert fa["is_peer_reviewed"] == "true"
    assert fa["year"] == "2024"                                   # parsed from venue string

    fs = od.facets(_SUBMITTED)
    assert fs["source_kind"] == "paper"
    assert fs["is_peer_reviewed"] == "false"

    # the facet contract drives evidence_kind's existing paper branch:
    assert evidence_kind.classify("openreview", fa) == "verified_structured"
    assert evidence_kind.classify("openreview", fs) == "technical_signal"
