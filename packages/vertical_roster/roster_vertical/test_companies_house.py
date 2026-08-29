"""Offline tests for the UK Companies House connector — NO network, companies injected, no key set.

Prove discover→list→fetch renders an official registry record with the company name/number/status
and an `## Officers` section (the team/execution signal); that facets grade `primary_filing` and are
stamped UK; and that the connector is INERT (returns []) when neither a key nor fixtures are present.
"""
from __future__ import annotations

import asyncio

import pytest

from . import companies_house_doc as ch, evidence_kind
from .connectors.companies_house import CompaniesHouseConnector

_COMPANY = {
    "company_number": "07094451",
    "title": "MONZO BANK LIMITED",
    "company_status": "active",
    "date_of_creation": "2009-12-01",
    "company_type": "ltd",
    "officers": [
        {"name": "ANDERSON, Thomas", "officer_role": "director", "appointed_on": "2015-02-11",
         "occupation": "Banker"},
        {"name": "SMITH, Jane", "officer_role": "secretary", "appointed_on": "2016-06-30"},
    ],
}


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    monkeypatch.delenv("ROSTER_COMPANIES_HOUSE_KEY", raising=False)


def test_discover_list_fetch_renders_officers():
    c = CompaniesHouseConnector(companies=[_COMPANY])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["07094451"]

    docs = asyncio.run(c.list_documents(ents[0]))
    md = asyncio.run(c.fetch_artifact(docs[0])).decode()

    assert "MONZO BANK LIMITED" in md
    assert "07094451" in md              # company number
    assert "active" in md                # status
    assert "2009-12-01" in md            # incorporation date
    assert "## Officers" in md           # team/execution section
    assert "ANDERSON, Thomas" in md and "director" in md   # officer name + role
    assert "official UK Companies House" in md             # framed as a registry record


def test_facets_grade_primary_filing_uk():
    f = ch.facets(_COMPANY)
    assert f["source_kind"] == "filing"
    assert f["source_country"] == "UK"
    assert f["entity_type"] == "company"
    assert f["company_status"] == "active"
    assert f["year"] == "2009"
    assert evidence_kind.classify("companies_house", f) == "primary_filing"


def test_inert_without_key():
    c = CompaniesHouseConnector()   # no key, no fixtures
    ents = asyncio.run(c.discover_entities({"query": "monzo"}))
    assert ents == []
