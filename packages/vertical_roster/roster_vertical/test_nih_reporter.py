"""Offline tests for the NIH RePORTER connector — NO network (projects injected).

Prove discover→list→fetch surfaces title + organization + amount, and that every project grades
`verified_structured` (funding-DB tier) via source_kind=funding — an attested funding signal.
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, nih_reporter_doc as nih
from .connectors.nih_reporter import NihReporterConnector

_PROJECT = {
    "project_num": "5R01AI999999-03",
    "project_title": "Machine Learning for Protein Structure Prediction",
    "abstract_text": "We build <i>deep</i> generative models to predict folded protein conformations.",
    "organization": {"org_name": "Stanford University"},
    "principal_investigators": [{"full_name": "Grace Hopper"}, {"full_name": "Alan Turing"}],
    "award_amount": 1250000,
    "fiscal_year": 2024,
}


def test_discover_list_fetch():
    c = NihReporterConnector(projects=[_PROJECT])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["5R01AI999999-03"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "Machine Learning for Protein Structure Prediction" in md
    assert "Stanford University" in md
    assert "1250000" in md
    assert "Grace Hopper" in md
    assert "grant record" in md.lower()               # framed as government grant record
    assert "<i>" not in md and "deep generative models" in md   # HTML stripped


def test_verified_structured_tier():
    f = nih.facets(_PROJECT)
    assert f["source_kind"] == "funding"
    assert f["agency"] == "NIH"
    assert f["awardee"] == "Stanford University"
    assert f["amount"] == "1250000"
    assert f["year"] == "2024"
    assert evidence_kind.classify("nih_reporter", f) == "verified_structured"
