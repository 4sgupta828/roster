"""Offline tests for the Reddit connector — NO network, NO creds (posts injected).

Prove discover→list→fetch over the OAuth-shaped "t3" record, that every post grades
`sentiment_signal` (lowest tier, NEVER controlling) with a labeled 'market signal' framing, and that
without creds the live path is inert (a keyed source, not a hard dependency).
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, reddit_doc as rd
from .authority import TechAuthorityPolicy
from .connectors import RedditConnector

_POST = {"data": {
    "id": "abc12", "title": "New 7B model tops MMLU", "subreddit": "LocalLLaMA",
    "score": 412, "num_comments": 88, "author": "someone", "created_utc": 1723900000,
    "permalink": "/r/LocalLLaMA/comments/abc12/x/", "selftext": "Benchmarks inside.",
    "url": "https://example.com/bench",
}}


def test_discover_list_fetch():
    c = RedditConnector(posts=[_POST])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["abc12"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "New 7B model tops MMLU" in md and "412 points" in md
    assert "r/LocalLLaMA" in md and "reddit.com/r/LocalLLaMA/comments/abc12" in md
    assert "market signal" in md.lower()               # labeled as signal, not fact
    assert "Benchmarks inside" in md


def test_sentiment_signal_tier_never_controlling():
    f = rd.facets(_POST)
    assert f["source_kind"] == "social" and f["subreddit"] == "LocalLLaMA" and f["score"] == "412"
    kind = evidence_kind.classify("reddit", f)
    assert kind == "sentiment_signal"
    assert not TechAuthorityPolicy().is_controlling(kind)


def test_inert_without_creds(monkeypatch):
    """No client id/secret → live search returns nothing (connector degrades to inert, no crash)."""
    monkeypatch.delenv("ROSTER_REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("ROSTER_REDDIT_CLIENT_SECRET", raising=False)
    c = RedditConnector()                               # no fixtures, no creds
    ents = asyncio.run(c.discover_entities({"query": "large language models", "limit": 5}))
    assert ents == []                                   # inert, not an exception


def test_registered_in_manifest():
    from .manifest import build_manifest
    assert "reddit" in build_manifest().connectors
