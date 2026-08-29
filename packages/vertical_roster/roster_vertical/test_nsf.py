"""Offline tests for the NSF Awards connector — NO network (awards injected).

Prove discover→list→fetch surfaces title + awardee + amount, and that every award grades
`verified_structured` (funding-DB tier) via source_kind=funding — an attested funding signal.
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, nsf_doc
from .connectors.nsf import NsfConnector

_AWARD = {
    "id": "2312345",
    "title": "Scalable Neural Architectures for Edge Inference",
    "abstractText": "This project develops <b>efficient</b> transformer variants for low-power devices.",
    "awardeeName": "Massachusetts Institute of Technology",
    "piFirstName": "Ada",
    "piLastName": "Lovelace",
    "startDate": "2023-09-01",
    "fundsObligatedAmt": "650000",
    "agency": "NSF",
}


def test_discover_list_fetch():
    c = NsfConnector(awards=[_AWARD])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["2312345"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "Scalable Neural Architectures for Edge Inference" in md
    assert "Massachusetts Institute of Technology" in md
    assert "650000" in md
    assert "Ada Lovelace" in md
    assert "grant record" in md.lower()               # framed as government grant record
    assert "<b>" not in md and "efficient transformer" in md   # HTML stripped


def test_verified_structured_tier():
    f = nsf_doc.facets(_AWARD)
    assert f["source_kind"] == "funding"
    assert f["agency"] == "NSF"
    assert f["awardee"] == "Massachusetts Institute of Technology"
    assert f["amount"] == "650000"
    assert f["year"] == "2023"
    assert evidence_kind.classify("nsf", f) == "verified_structured"
