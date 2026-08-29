"""Offline tests for the USPTO connector — NO network (patents injected), NO key.

Prove discover→list→fetch renders a citable patent markdown; that a GRANTED patent grades
`primary_filing` while a pre-grant APPLICATION grades `technical_signal` (evidence tiering intact);
and that the connector is INERT without ROSTER_USPTO_KEY (KEYED, fail-safe contract). Self-contained:
patents are already in patent_doc's expected shape, so no live-field assumptions are exercised.
"""
from __future__ import annotations

import asyncio

from . import evidence_kind
from .connectors.uspto import UsptoConnector

_GRANTED = {
    "patent_id": "US10000000B2",
    "patent_title": "Solid-state battery electrolyte",
    "assignees": [{"assignee_organization": "QuantumScape Corp"}],
    "grant_status": "granted",
    "patent_date": "2021-06-15",
    "patent_abstract": "A solid electrolyte separator for a lithium battery.",
}

_APPLICATION = {
    "patent_id": "US20220123456A1",
    "patent_title": "Anode-free lithium cell",
    "assignees": [{"assignee_organization": "SES AI"}],
    "grant_status": "application",
    "patent_date": "2022-04-21",
}


def test_discover_list_fetch(monkeypatch):
    monkeypatch.delenv("ROSTER_USPTO_KEY", raising=False)
    c = UsptoConnector(patents=[_GRANTED])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["US10000000B2"]
    docs = asyncio.run(c.list_documents(ents[0]))
    md = asyncio.run(c.fetch_artifact(docs[0])).decode()
    assert "Solid-state battery electrolyte" in md   # title present
    assert "QuantumScape Corp" in md                  # assignee present
    assert "GRANTED" in md


def test_granted_is_primary_filing(monkeypatch):
    monkeypatch.delenv("ROSTER_USPTO_KEY", raising=False)
    c = UsptoConnector(patents=[_GRANTED])
    ent = asyncio.run(c.discover_entities({}))[0]
    assert ent.facets["grant_status"] == "granted"
    assert evidence_kind.classify("uspto", ent.facets) == "primary_filing"


def test_application_is_technical_signal(monkeypatch):
    monkeypatch.delenv("ROSTER_USPTO_KEY", raising=False)
    c = UsptoConnector(patents=[_APPLICATION])
    ent = asyncio.run(c.discover_entities({}))[0]
    assert ent.facets["grant_status"] == "application"
    assert evidence_kind.classify("uspto", ent.facets) == "technical_signal"


def test_inert_without_key(monkeypatch):
    monkeypatch.delenv("ROSTER_USPTO_KEY", raising=False)
    c = UsptoConnector()   # no fixtures, no key
    ents = asyncio.run(c.discover_entities({"query": "solid state battery"}))
    assert ents == []


def test_normalize_maps_real_odp_response_shape():
    """The live ODP Patent File Wrapper record nests fields under applicationMetaData. _normalize must
    flatten it to patent_doc's shape and read grant vs pre-grant correctly (confirmed Aug 2026)."""
    from .connectors.uspto import _normalize, _granted
    granted = _normalize({"applicationNumberText": "17123456", "applicationMetaData": {
        "inventionTitle": "Attention-based transformer", "patentNumber": "11123456",
        "grantDate": "2023-05-02", "applicationStatusDescriptionText": "Patented Case",
        "applicantBag": [{"applicantNameText": "OpenAI Inc"}]}})
    assert granted["patent_id"] == "11123456" and granted["patent_title"] == "Attention-based transformer"
    assert granted["assignees"] == [{"assignee_organization": "OpenAI Inc"}]
    assert granted["grant_status"] == "granted" and _granted(granted)
    pregrant = _normalize({"applicationNumberText": "17999999", "applicationMetaData": {
        "inventionTitle": "Pending method", "applicationStatusDescriptionText": "Docketed New Case",
        "applicantBag": [{"applicantNameText": "Acme AI"}]}})
    assert pregrant["patent_id"] == "17999999" and pregrant["grant_status"] == "application"
    assert not _granted(pregrant)
