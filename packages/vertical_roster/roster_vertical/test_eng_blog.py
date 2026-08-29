"""Offline tests for the company engineering-blog feed connector — NO network (items injected).

Prove discover→list→fetch, that the byline names the company blog and frames the item as a
SELF-REPORTED corporate account (not independent press / not a primary fact), that HTML is stripped,
and — the load-bearing correctness property — that a company eng blog grades to `technical_signal`
(rank 2, self-reported) and sits STRICTLY BELOW a named individual's essay (`expert_analysis`, rank 3)
and below fact-checked press (`analysis`, rank 4). That ordering is the whole point of the separate
`corp_eng` source_kind: a company praising its own tech must never outrank independent analysis.
"""
from __future__ import annotations

import asyncio

from . import eng_blog_doc as ed
from . import evidence_kind
from .authority import TechAuthorityPolicy
from .connectors.eng_blog import EngBlogConnector

# RSS-shaped (content:encoded body, guid, pubDate) — already normalized.
_RSS_ITEM = {
    "id": "https://netflixtechblog.com/rebuilding-netflix-video-processing-abc",
    "guid": "https://netflixtechblog.com/rebuilding-netflix-video-processing-abc",
    "link": "https://netflixtechblog.com/rebuilding-netflix-video-processing-abc",
    "title": "Rebuilding Netflix Video Processing Pipeline",
    "author": "Netflix Technology Blog",
    "publication": "Netflix TechBlog",
    "published": "Tue, 15 Apr 2025 10:00:00 GMT",
    "summary": "<p>A short take.</p>",
    "content": "<p>We rebuilt the pipeline on a <b>microservices</b> architecture.</p>",
}

# Atom-shaped (content body, id, updated) — already normalized.
_ATOM_ITEM = {
    "id": "https://oxide.computer/blog/a-hardware-story",
    "guid": "https://oxide.computer/blog/a-hardware-story",
    "link": "https://oxide.computer/blog/a-hardware-story",
    "title": "How we built our service processor",
    "author": "",
    "publication": "Oxide Computer Company Blog",
    "published": "2025-05-11T08:30:00Z",
    "summary": "Summary text.",
    "content": "<div>The hypervisor boots from a <i>custom</i> ROM.</div>",
}


def test_discover_list_fetch_byline_and_html_strip():
    c = EngBlogConnector(items=[_RSS_ITEM])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == [_RSS_ITEM["guid"]]

    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "Rebuilding Netflix Video Processing Pipeline" in md
    # byline names the company blog
    assert "Netflix TechBlog" in md
    # explicit SELF-REPORTED framing (not independent press / not a primary fact)
    assert "self-reported" in md.lower()
    assert "not independently verified" in md.lower() or "not independently" in md.lower()
    # HTML stripped, body content preserved
    assert "<p>" not in md and "<b>" not in md
    assert "microservices architecture" in md
    assert "URL: https://netflixtechblog.com/rebuilding-netflix-video-processing-abc" in md


def test_facets_corp_eng_kind():
    f = ed.facets(_RSS_ITEM)
    assert f["source_kind"] == "corp_eng"
    assert f["entity_type"] == "corp_eng"
    assert f["publication"] == "Netflix TechBlog"
    assert f["year"] == "2025"


def test_classifies_to_technical_signal_below_expert_and_press():
    """The correctness property: corp_eng → technical_signal, strictly below expert_analysis & analysis."""
    pol = TechAuthorityPolicy()
    corp = evidence_kind.classify("eng_blog", {"source_kind": "corp_eng"})
    assert corp == "technical_signal"
    # a company eng blog must NOT outrank a named individual's essay, nor fact-checked press.
    assert pol.rank(corp) < pol.rank("expert_analysis")
    assert pol.rank(corp) < pol.rank("analysis")
    assert pol.rank(corp) < pol.rank("primary_filing")
    # and it is never controlling (only primary_filing is)
    assert not pol.is_controlling(corp)
    # classification also works off the source_key alone
    assert evidence_kind.classify("eng_blog", None) == "technical_signal"


def test_rss_and_atom_shapes_both_render():
    c = EngBlogConnector(items=[_RSS_ITEM, _ATOM_ITEM])
    ents = asyncio.run(c.discover_entities({}))
    ids = {e.native_id for e in ents}
    assert _RSS_ITEM["guid"] in ids and _ATOM_ITEM["guid"] in ids

    atom_ent = next(e for e in ents if e.native_id == _ATOM_ITEM["guid"])
    atom_md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(atom_ent))[0])).decode()
    assert _ATOM_ITEM["title"] in atom_md
    assert "Oxide Computer Company Blog" in atom_md
    # Atom body HTML stripped too
    assert "<div>" not in atom_md and "<i>" not in atom_md
    assert "The hypervisor boots from a custom ROM" in atom_md


def test_registered_in_manifest():
    from .manifest import build_manifest
    m = build_manifest()
    assert "eng_blog" in m.connectors
    assert m.connectors["eng_blog"].key == "eng_blog"
