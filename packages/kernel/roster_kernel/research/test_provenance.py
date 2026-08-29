"""Offline tests for the provenance hard gate (span-check) + tenant isolation."""
from __future__ import annotations

from roster_kernel.contract.dto import Locator
from roster_kernel.contract.protocols import CitationVerifier
from roster_kernel.research.provenance import BlockSpanVerifier, normalize
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


def _src() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A",
                         text="The approved figure was 9.8 percent overall."))
    src.add(IndexedBlock(block_id="s1", document_id="dS", tenant_id="B",
                         text="Tenant B secret: the code is 4242."))
    return src


def _loc(document_id="d1", block_id="b1"):
    return Locator(kind="block_span", document_id=document_id, ref={"block_id": block_id})


def test_exact_and_whitespace_normalized_quote_verifies() -> None:
    v = BlockSpanVerifier(_src().make_block_loader("A"))
    assert v.verify("the approved figure was 9.8 percent", _loc())
    assert v.verify("The   approved\n figure  was", _loc())   # reflow-tolerant


def test_fabricated_quote_fails() -> None:
    v = BlockSpanVerifier(_src().make_block_loader("A"))
    assert not v.verify("the approved figure was 12.3 percent", _loc())


def test_missing_block_fails_closed() -> None:
    v = BlockSpanVerifier(_src().make_block_loader("A"))
    assert not v.verify("anything", _loc(block_id="nope"))
    assert not v.verify("anything", _loc(document_id="nope"))


def test_wrong_locator_kind_fails() -> None:
    v = BlockSpanVerifier(_src().make_block_loader("A"))
    assert not v.verify("x", Locator(kind="fact_coordinate", document_id="d1", ref={"block_id": "b1"}))


def test_cross_tenant_verification_blocked() -> None:
    # A verifier scoped to tenant A must NOT verify a quote against tenant B's block,
    # even given B's exact locator + exact quote (the cross-tenant FALSE-PASS guard).
    src = _src()
    v_a = BlockSpanVerifier(src.make_block_loader("A"))
    b_loc = _loc(document_id="dS", block_id="s1")
    assert not v_a.verify("tenant b secret: the code is 4242", b_loc)
    # tenant B's own verifier CAN verify it
    v_b = BlockSpanVerifier(src.make_block_loader("B"))
    assert v_b.verify("tenant b secret: the code is 4242", b_loc)


def test_verifier_satisfies_protocol() -> None:
    assert isinstance(BlockSpanVerifier(_src().make_block_loader("A")), CitationVerifier)


def test_normalize() -> None:
    assert normalize("  Hello   World \n") == "hello world"
