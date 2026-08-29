"""Offline tests for the Hugging Face Hub connector — NO network (models injected).

Prove discover→list→fetch produces a citable model-adoption doc, and that every model carries the
`source_kind=model` / `technical_signal` framing (a SELF-REPORTED adoption signal, never audited fact).
Self-contained: manifest/evidence-kind wiring is added separately by the orchestrator.
"""
from __future__ import annotations

import asyncio

from . import evidence_kind, hf_doc
from .connectors.huggingface import HuggingFaceConnector

_MODEL = {
    "id": "meta-llama/Llama-3.1-8B", "downloads": 1234567, "likes": 4200,
    "pipeline_tag": "text-generation", "library_name": "transformers",
    "tags": ["text-generation", "llama"], "createdAt": "2024-07-23T00:00:00.000Z",
    "lastModified": "2025-01-10T00:00:00.000Z",
    "readme": "# Llama 3.1\nA foundation model.",
}


def test_discover_list_fetch_produces_adoption_markdown():
    c = HuggingFaceConnector(models=[_MODEL])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["meta-llama/Llama-3.1-8B"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "meta-llama/Llama-3.1-8B" in md              # the model id
    assert "1,234,567 downloads" in md                  # a downloads figure
    assert "adoption" in md.lower() and "signal" in md.lower()   # signal framing, not fact
    assert "A foundation model." in md                  # model card README included


def test_facets_stamp_model_source_kind():
    f = hf_doc.facets(_MODEL)
    assert f["source_kind"] == "model"
    assert f["pipeline_tag"] == "text-generation"
    assert f["org"] == "meta-llama" and f["downloads"] == "1234567"
    assert f["year"] == "2024"
    # evidence_kind mapping is wired by the orchestrator separately; "" is expected until then.
    assert evidence_kind.classify("huggingface", f) in ("technical_signal", "")


def test_metadata_only_when_no_readme():
    bare = {"id": "org/model-x", "downloads": 10, "likes": 1, "pipeline_tag": "fill-mask"}
    c = HuggingFaceConnector(models=[bare])
    ents = asyncio.run(c.discover_entities({}))
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "org/model-x" in md and "## Model card" not in md
    assert "10 downloads" in md
