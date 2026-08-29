"""Held-out offline tests for the tech vertical: conformance + corpus grounding.

These run with NO LLM and NO network — they prove the manifest conforms and that every
factual gold fact is retrievable from the fixture corpus AND span-verifies through the
kernel's provenance gate at the declared authority tier. The refuse case must NOT be in
the corpus (an honest gap, never a fabricated number).
"""
from __future__ import annotations

import asyncio

from roster_kernel.conformance.runner import run_conformance
from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.research.provenance import BlockSpanVerifier

from . import evidence_kind
from .authority import TechAuthorityPolicy
from .eval_gold import GOLD
from .manifest import build_manifest
from .source import TechRetrievalSource


def test_manifest_conforms_through_p4():
    report = run_conformance(build_manifest(), phase="P4")
    assert report.ok, report.summary()


def test_manifest_name_is_tech():
    assert build_manifest().name == "tech"


def _search(src, emb, q, k=8):
    qv = emb.embed([q])[0]
    req = RetrievalRequest(query=q, tenant_id="demo", query_embedding=list(qv), k=k)
    return asyncio.run(src.search(req))


def test_gold_facts_span_verify_at_declared_tier():
    emb = FakeEmbedder(dim=16)
    src = TechRetrievalSource(embedder=emb)
    checker = BlockSpanVerifier(src.make_block_loader("demo"))
    authority = TechAuthorityPolicy()
    for key, g in GOLD.items():
        if g["expect"] != "value":
            continue
        hits = _search(src, emb, g["question"])
        quote = g["supporting_quote"]
        verified = next((h for h in hits
                         if quote.lower() in h.text.lower()
                         and h.locator and checker.verify(quote, h.locator)), None)
        assert verified is not None, f"{key}: gold quote not retrieved+span-verified"
        tier = evidence_kind.classify(verified.source_key, verified.facets)
        floor = g["evidence_floor"]
        assert authority.rank(tier) >= authority.rank(floor), \
            f"{key}: tier {tier} below floor {floor}"


def test_refuse_case_subject_absent_from_corpus():
    emb = FakeEmbedder(dim=16)
    src = TechRetrievalSource(embedder=emb)
    g = GOLD["coverage_gap_unknown_company"]
    hits = _search(src, emb, g["question"])
    assert not any("helio" in h.text.lower() for h in hits), \
        "refuse-case subject must NOT be in the fixture corpus"


def test_sentiment_is_never_controlling():
    # The code-level guarantee that market sentiment is a signal, not fact.
    authority = TechAuthorityPolicy()
    assert not authority.is_controlling("sentiment_signal")
    assert authority.is_controlling("primary_filing")
    assert authority.rank("primary_filing") > authority.rank("sentiment_signal")
