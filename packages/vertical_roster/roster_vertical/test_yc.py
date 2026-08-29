"""Offline tests for the YC company-directory connector — NO network, companies injected.

Prove discover→list→fetch renders a YC directory profile with the company name, batch, founders
(names + titles = the team/execution signal), one-liner and long description; that facets grade
`verified_structured` (the Wikidata reference precedent) and stamp batch/status/US; and that a record
carrying an injected `founders` list renders it without hitting the detail page (no network).
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, yc_doc
from .connectors.yc import YcConnector

# Two fixtures shaped like the Algolia search record, one enriched with detail-page founders.
_AISDR = {
    "slug": "aisdr",
    "name": "AiSDR",
    "batch": "Summer 2023",
    "status": "Active",
    "year_founded": 2023,
    "team_size": 5,
    "all_locations": "San Francisco, CA, USA; Remote",
    "one_liner": "AI sales prospecting. Replace your SDR with AiSDR",
    "long_description": "AiSDR automates sales prospecting using AI to write and send outreach.",
    "industry": "B2B",
    "industries": ["B2B", "Sales"],
    "tags": ["Artificial Intelligence", "SaaS", "Sales"],
    "website": "https://aisdr.com/",
    "founders": [
        {"full_name": "Yuriy Zaremba", "title": "Founder/CEO"},
        {"full_name": "Oleg Zaremba", "title": "Founder"},
    ],
}
_ANDAI = {
    "slug": "and-ai",
    "name": "&AI",
    "batch": "Summer 2024",
    "status": "Active",
    "team_size": 7,
    "all_locations": "San Francisco, CA, USA",
    "one_liner": "Collaborative workspace for patent litigators",
    "industry": "B2B",
    "industries": ["B2B", "Legal"],
    "tags": ["SaaS", "LegalTech"],
    "website": "https://www.tryandai.com/",
    "founders": [{"full_name": "Jane Doe", "title": "Co-Founder"}],
}


def test_discover_list_fetch_renders_company_and_founders():
    c = YcConnector(companies=[_AISDR, _ANDAI])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["aisdr", "and-ai"]

    ent = ents[0]
    docs = asyncio.run(c.list_documents(ent))
    md = asyncio.run(c.fetch_artifact(docs[0])).decode()   # no network — founders already on the record

    assert "AiSDR" in md                                   # name
    assert "Summer 2023" in md                             # YC batch
    assert "founded 2023" in md                            # founded year
    assert "## Founders" in md                             # team/execution section
    assert "Yuriy Zaremba" in md and "Founder/CEO" in md   # founder name + title
    assert "AI sales prospecting" in md                    # one-liner
    assert "AiSDR automates sales prospecting" in md       # long description
    assert "B2B" in md and "Sales" in md                   # industry/tags
    assert "https://aisdr.com/" in md                      # website
    assert "Y Combinator company directory" in md          # framed as a YC directory profile


def test_facets_grade_verified_structured():
    f = yc_doc.facets(_AISDR)
    assert f["source_kind"] == "reference"
    assert f["entity_type"] == "company"
    assert f["source_country"] == "US"
    assert f["batch"] == "Summer 2023"
    assert f["yc_status"] == "active"
    assert f["year"] == "2023"
    assert evidence_kind.classify("yc", f) == "verified_structured"


def test_inert_without_query_or_fixtures():
    c = YcConnector()   # no fixtures; empty window → no query/filter → no network, no entities
    ents = asyncio.run(c.discover_entities({}))
    assert ents == []
