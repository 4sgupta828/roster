"""Offline tests for the Hacker News connector — NO network (stories injected).

Prove discover→list→fetch, that HTML in self-posts is stripped, and that every story grades
`sentiment_signal` (lowest tier, NEVER controlling) with a labeled 'market signal' framing.
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, hackernews_doc as hd
from .authority import TechAuthorityPolicy
from .connectors import HackerNewsConnector

_STORY = {
    "objectID": "12345", "title": "Anthropic raises new round", "url": "https://example.com/x",
    "points": 512, "num_comments": 87, "author": "pg", "created_at": "2025-03-02T10:00:00Z",
    "story_text": "<p>Big news for <b>Claude</b>.</p>",
}


def test_discover_list_fetch_and_html_strip():
    c = HackerNewsConnector(stories=[_STORY])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["12345"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "Anthropic raises new round" in md and "512 points" in md
    assert "market signal" in md.lower()               # labeled as signal, not fact
    assert "<p>" not in md and "Big news for Claude" in md   # HTML stripped


def test_sentiment_signal_tier_never_controlling():
    f = hd.facets(_STORY)
    assert f["source_kind"] == "social" and f["points"] == "512"
    kind = evidence_kind.classify("hackernews", f)
    assert kind == "sentiment_signal"
    pol = TechAuthorityPolicy()
    assert not pol.is_controlling(kind)


def test_registered_in_manifest():
    from .manifest import build_manifest
    assert "hackernews" in build_manifest().connectors
