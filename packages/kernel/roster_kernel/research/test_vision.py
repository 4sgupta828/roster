"""Held-out checks for the vision pre-step + its grounding isolation.

The invariant under test: a user IMAGE only frames the search — its description can NEVER
become a span-verified claim, and an image alone (empty/irrelevant corpus) is not grounded.
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.research.vision import VisualObservation, observe_images
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource

# a 1x1 png (kept tiny so it never bloats the repo / cassettes)
_PNG_1x1 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


class _VisionLLM:
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        # assert the image block rode through as content, and the guard is in the system prompt
        content = messages[0]["content"]
        assert isinstance(content, list) and any(b.get("type") == "image" for b in content)
        assert "Do NOT name a diagnosis" in system
        return LLMResult(parsed=VisualObservation(observation="a purple star with jagged edges"),
                         output_tokens=5, model="fake")


def test_vision_pre_step_returns_observation() -> None:
    obs = asyncio.run(observe_images(
        llm=_VisionLLM(), vision_prompt="Describe the image.",
        images=[{"media_type": "image/png", "data": _PNG_1x1}], budget=BudgetState(max_calls=5)))
    assert "purple star" in obs


class _Scripted:
    def __init__(self, steps): self._s = list(steps)
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._s.pop(0), output_tokens=5, model="scripted")


def _corpus(text: str):
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=text,
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    return src


def test_image_text_cannot_become_a_verified_claim() -> None:
    # The agent tries to "cite" the image description; it isn't in any corpus atom → rejected.
    src = _corpus("Aspirin reduces fever in adults.")
    llm = _Scripted([
        AgentStep(action="search", query="aspirin fever"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the lesion is a purple star", atom_id="a1",
                     quote="a purple star with jagged edges")]),   # image text, not in the atom
    ])
    res = asyncio.run(run_react(
        question="what is this?", llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=10),
        attachment_context="a purple star with jagged edges"))
    assert not res.verified_claims                                       # never verified
    assert not res.grounded
    assert any("purple star" in r.quote for r in res.rejected_claims)    # caught by the gate


def test_image_frames_but_corpus_grounds() -> None:
    # With the image as context, a claim grounded in the CORPUS still verifies normally,
    # and the visual observation stays separate from the verified findings.
    src = _corpus("Aspirin reduces fever in adults.")
    llm = _Scripted([
        AgentStep(action="search", query="aspirin fever"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="aspirin reduces fever", atom_id="a1",
                     quote="aspirin reduces fever in adults")]),
    ])
    res = asyncio.run(run_react(
        question="does aspirin help fever?", llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=10),
        attachment_context="a purple star with jagged edges"))
    assert res.grounded and len(res.verified_claims) == 1
    assert "purple star" not in " ".join(c.quote + c.text for c in res.verified_claims)


def test_service_surfaces_observation_and_grounds_in_corpus() -> None:
    # End-to-end via ResearchService: image → observation (surfaced), a document is passed
    # as context, and the answer is still grounded ONLY in the corpus.
    from roster_kernel.research.react import ComposedAnswer
    from roster_kernel.runtime.research import ResearchService

    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            content = messages[0]["content"]
            if response_format is VisualObservation:
                return LLMResult(parsed=VisualObservation(observation="reddish scaly patch"), output_tokens=5, model="f")
            if isinstance(content, str) and "VERIFIED FINDINGS" in content:
                return LLMResult(parsed=ComposedAnswer(answer="Topical steroids help [1]."), output_tokens=5, model="f")
            if isinstance(content, str) and "no evidence yet" in content:
                return LLMResult(parsed=AgentStep(action="search", query="topical steroids scaling"), output_tokens=5, model="f")
            return LLMResult(parsed=AgentStep(action="answer", claims=[
                ClaimOut(text="steroids help", atom_id="a1",
                         quote="topical steroids reduce scaling")]), output_tokens=5, model="f")

    src = _corpus("Topical steroids reduce scaling in plaque conditions.")
    svc = ResearchService(llm=_LLM(), embedder=FakeEmbedder(dim=8), sources={"corpus": src},
                          vision_prompt="Describe the image.")
    res = asyncio.run(svc.ask(
        question="what helps this?", tenant_id="A",
        images=[{"media_type": "image/png", "data": _PNG_1x1}],
        documents=[{"name": "note.txt", "text": "itching for two weeks"}]))
    assert res.visual_observation == "reddish scaly patch"      # image reading surfaced
    assert res.grounded and len(res.verified_claims) == 1       # grounded in the corpus
