"""Offline tests for the expert-newsletter/blog feed connector — NO network (items injected).

Prove discover→list→fetch, that the byline names the expert + publication and frames the item as
expert analysis / opinion, that HTML is stripped from the body, and that both RSS-shaped and
Atom-shaped (already-parsed) item dicts render. Injected items are stored pre-parsed in `_by_id`, so
these tests never touch live XML parsing. Self-contained: no manifest / classify assertions (the
"expert_analysis" tier is wired separately).
"""
from __future__ import annotations

import asyncio

from . import expert_feed_doc as ed
from .connectors.expert_feed import ExpertFeedConnector

# RSS-shaped (content:encoded body, dc:creator author, guid, pubDate) — already normalized.
_RSS_ITEM = {
    "id": "https://simonwillison.net/2025/mar/02/llms/",
    "guid": "https://simonwillison.net/2025/mar/02/llms/",
    "link": "https://simonwillison.net/2025/mar/02/llms/",
    "title": "Where LLMs are heading in 2025",
    "author": "Simon Willison",
    "publication": "Simon Willison's Weblog",
    "published": "Sun, 02 Mar 2025 10:00:00 GMT",
    "summary": "<p>A short take.</p>",
    "content": "<p>My read is that <b>agents</b> will dominate the year.</p>",
}

# Atom-shaped (content body, id, updated) — already normalized.
_ATOM_ITEM = {
    "id": "tag:magazine.sebastianraschka.com,2025:/p/144",
    "guid": "tag:magazine.sebastianraschka.com,2025:/p/144",
    "link": "https://magazine.sebastianraschka.com/p/scaling-laws",
    "title": "What scaling laws still tell us",
    "author": "Sebastian Raschka",
    "publication": "Ahead of AI",
    "published": "2025-04-11T08:30:00Z",
    "summary": "Summary text.",
    "content": "<div>Compute-optimal training remains <i>underused</i> in practice.</div>",
}


def test_discover_list_fetch_byline_and_html_strip():
    c = ExpertFeedConnector(items=[_RSS_ITEM])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == [_RSS_ITEM["guid"]]

    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "Where LLMs are heading in 2025" in md
    # byline names the expert + publication
    assert "Simon Willison" in md and "Simon Willison's Weblog" in md
    # explicit expert-analysis / opinion framing
    assert "expert analysis" in md.lower() and "opinion" in md.lower()
    # HTML stripped, body content preserved
    assert "<p>" not in md and "<b>" not in md
    assert "agents will dominate the year" in md
    assert "URL: https://simonwillison.net/2025/mar/02/llms/" in md


def test_facets_essay_tier_author_and_publication():
    f = ed.facets(_RSS_ITEM)
    assert f["source_kind"] == "essay"
    assert f["entity_type"] == "essay"
    assert f["author"] == "Simon Willison"
    assert f["publication"] == "Simon Willison's Weblog"
    assert f["year"] == "2025"


def test_rss_and_atom_shapes_both_render():
    c = ExpertFeedConnector(items=[_RSS_ITEM, _ATOM_ITEM])
    ents = asyncio.run(c.discover_entities({}))
    ids = {e.native_id for e in ents}
    assert _RSS_ITEM["guid"] in ids and _ATOM_ITEM["guid"] in ids

    for item in (_RSS_ITEM, _ATOM_ITEM):
        ent = next(e for e in ents if e.native_id == item["guid"])
        md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ent))[0])).decode()
        assert item["title"] in md
        assert item["author"] in md and item["publication"] in md
        assert "expert analysis" in md.lower()

    # Atom body HTML stripped too
    atom_ent = next(e for e in ents if e.native_id == _ATOM_ITEM["guid"])
    atom_md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(atom_ent))[0])).decode()
    assert "<div>" not in atom_md and "<i>" not in atom_md
    assert "Compute-optimal training remains underused in practice" in atom_md


def test_body_truncated_to_16k():
    big = dict(_RSS_ITEM, content="<p>" + ("word " * 8000) + "</p>")
    c = ExpertFeedConnector(items=[big])
    ents = asyncio.run(c.discover_entities({}))
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "…" in md and len(md) < 17000
