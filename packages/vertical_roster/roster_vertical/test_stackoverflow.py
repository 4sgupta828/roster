"""Offline tests for the Stack Overflow connector — NO network (questions injected).

Prove discover→list→fetch, that HTML in the question body is stripped, and that every question grades a
labeled developer-adoption 'signal' with source_kind=social (lowest tier, never fact).
"""
from __future__ import annotations

import asyncio

from . import stackoverflow_doc as sd
from .connectors.stackexchange import StackExchangeConnector

_QUESTION = {
    "question_id": 67890, "title": "How to stream tokens from the Anthropic API?",
    "link": "https://stackoverflow.com/q/67890",
    "score": 42, "answer_count": 3, "is_answered": True,
    "tags": ["python", "anthropic", "streaming"],
    "creation_date": 1740909600,  # 2025-03-02 UTC
    "body": "<p>I want to <b>stream</b> tokens from <code>Claude</code>.</p>",
}


def test_discover_list_fetch_and_html_strip():
    c = StackExchangeConnector(questions=[_QUESTION])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["67890"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "How to stream tokens from the Anthropic API?" in md and "42 votes" in md
    assert "signal" in md.lower()                                  # labeled as signal, not fact
    assert "<p>" not in md and "I want to stream tokens from Claude" in md   # HTML stripped


def test_facets_social_signal():
    f = sd.facets(_QUESTION)
    assert f["source_kind"] == "social"
    assert f["score"] == "42"
    assert f["answer_count"] == "3"
    assert f["tags"] == "python, anthropic, streaming"          # comma-joined
    assert f["entity_type"] == "qa"
    assert f["year"] == "2025"                                  # from epoch creation_date (UTC)
