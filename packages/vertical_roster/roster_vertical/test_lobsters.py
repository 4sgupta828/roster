"""Offline tests for the Lobsters connector — NO network (stories injected).

Prove discover→list→fetch, that HTML in the description is stripped, and that every story grades
`sentiment_signal` (lowest tier, NEVER controlling) with a labeled discussion/signal framing.
Self-contained: imports the connector directly (no manifest wiring assertion).
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, lobsters_doc as ld
from .connectors.lobsters import LobstersConnector

_STORY = {
    "short_id": "abc123",
    "title": "A deep dive into lock-free data structures",
    "url": "https://example.com/lockfree",
    "score": 142,
    "comment_count": 33,
    "created_at": "2025-06-14T09:00:00.000-05:00",
    "submitter_user": "carol",
    "tags": ["compsci", "performance"],
    "description": "<p>Notes on <b>lock-free</b> queues.</p>",
    "comments_url": "https://lobste.rs/s/abc123",
}


def test_discover_list_fetch_and_html_strip():
    c = LobstersConnector(stories=[_STORY])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["abc123"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "A deep dive into lock-free data structures" in md
    assert "142 points" in md                              # a points figure
    assert "discussion" in md.lower()                      # labeled discussion/signal framing
    assert "<p>" not in md and "Notes on lock-free queues" in md   # HTML stripped


def test_sentiment_signal_tier():
    f = ld.facets(_STORY)
    assert f["source_kind"] == "social"
    assert f["tags"] == "compsci, performance"             # tags joined
    assert f["score"] == "142"
    kind = evidence_kind.classify("lobsters", f)
    assert kind == "sentiment_signal"
