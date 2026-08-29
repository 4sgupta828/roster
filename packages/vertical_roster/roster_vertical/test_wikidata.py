"""Offline tests for the Wikidata connector — NO network (companies injected).

Prove discover→list→fetch builds a grounded company profile, facet extraction (source_kind=reference,
inception year, US country), the neutral tier (reference → rank 0, never controlling), and that a
company with NO structured rows is skipped (fail-safe, no empty profile).
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, wikidata_doc as wd
from .authority import TechAuthorityPolicy
from .connectors import WikidataConnector

_OPENAI = {
    "qid": "Q21708200", "label": "OpenAI", "description": "artificial intelligence research company",
    "rows": [("Founder", "Sam Altman"), ("Founder", "Elon Musk"), ("CEO", "Sam Altman"),
             ("Inception", "2015-12-11T00:00:00Z"), ("Industry", "artificial intelligence"),
             ("Country", "United States of America"), ("Owned by", "Microsoft")],
}
_EMPTY = {"qid": "Q999999", "label": "Nothing Co", "description": "", "rows": []}


def test_discover_list_fetch_profile():
    c = WikidataConnector(companies=[_OPENAI, _EMPTY])
    ents = asyncio.run(c.discover_entities({}))
    assert {e.native_id for e in ents} == {"Q21708200"}   # empty-rows company skipped (fail-safe)
    ent = ents[0]
    docs = asyncio.run(c.list_documents(ent))
    md = asyncio.run(c.fetch_artifact(docs[0])).decode()
    assert "OpenAI — company profile" in md and "Sam Altman" in md and "Microsoft" in md
    assert "**Founder:**" in md and "Elon Musk" in md


def test_facets_and_reference_tier():
    f = wd.facets("Q21708200", [tuple(r) for r in _OPENAI["rows"]])
    assert f["source_kind"] == "reference" and f["entity_type"] == "company"
    assert f["source_country"] == "US" and f["year"] == "2015"
    # curated structured reference (founder/inception/ownership) → verified_structured: a real boost
    # (previously UNMAPPED → silently rank 0), but still NON-controlling (only a filing is controlling).
    kind = evidence_kind.classify("wikidata", f)
    assert kind == "verified_structured"
    pol = TechAuthorityPolicy()
    assert pol.rank(kind) > 0 and not pol.is_controlling(kind)


def test_registered_in_manifest():
    from .manifest import build_manifest
    assert "wikidata" in build_manifest().connectors
