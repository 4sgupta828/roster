"""Source routing (flag ROSTER_SOURCE_ROUTING → ResearchService.source_routing): when the agent names
`source_kinds` on a search step, an ADDITIVE scoped retrieval leg runs ON TOP OF the flat pass (never a
filter — a mis-route can't lose recall). OFF → the field is ignored, no scoped leg, byte-identical.

Verified structurally by recording the `facets` of every RetrievalRequest the corpus source receives."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.runtime.research import ResearchService

_TEXT = "NSF awarded a grant for photonic computing research to a university lab."


class RecordingSource:
    """Wraps an in-memory source; records the facets of every request (to see routing legs).
    Delegates every other method to the inner source so it's a drop-in RetrievalSource."""
    def __init__(self, inner):
        self._inner = inner
        self.seen_facets: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)          # delegate covers()/make_block_loader()/… to inner

    async def search(self, req):
        self.seen_facets.append(dict(req.facets or {}))
        return await self._inner.search(req)


class RoutingLLM:
    def __init__(self, source_kinds):
        self._loop = [
            AgentStep(action="search", query="photonic computing research grant",
                      source_kinds=source_kinds),
            AgentStep(action="answer", claims=[
                ClaimOut(text="NSF funded photonic computing research", atom_id="a1",
                         quote="nsf awarded a grant for photonic computing research")]),
        ]

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        if response_format is ComposedAnswer:
            return LLMResult(parsed=ComposedAnswer(answer="Funded [1].", directly_addresses=True), model="c")
        return LLMResult(parsed=self._loop.pop(0), output_tokens=5, model="c")


def _run(*, source_routing: bool, source_kinds):
    inner = InMemoryRetrievalSource()
    inner.add(IndexedBlock(block_id="b1", document_id="nsf:1", tenant_id="A", text=_TEXT,
                           locator=Locator("block_span", "nsf:1", {"block_id": "b1"})))
    rec = RecordingSource(inner)
    svc = ResearchService(llm=RoutingLLM(source_kinds), embedder=FakeEmbedder(dim=8),
                          sources={"corpus": rec}, source_routing=source_routing)
    res = asyncio.run(svc.ask(question="What photonic-computing research is funded?", tenant_id="A"))
    return rec, res


def test_routing_on_adds_scoped_leg_and_keeps_flat_pass():
    rec, res = _run(source_routing=True, source_kinds=["funding"])
    # additive: BOTH a flat request (no source_kind) AND a scoped one (source_kind=funding) were issued.
    scoped = [f for f in rec.seen_facets if f.get("source_kind")]
    flat = [f for f in rec.seen_facets if not f.get("source_kind")]
    assert scoped, "expected a source-kind-scoped leg"
    assert scoped[0]["source_kind"] == ("funding",)
    assert flat, "the broad flat pass must still run (recall never lost)"
    assert res.verified_claims               # answer still composes


def test_routing_off_is_byte_identical_no_scoped_leg():
    rec, res = _run(source_routing=False, source_kinds=["funding"])   # step names kinds, but flag OFF
    assert all(not f.get("source_kind") for f in rec.seen_facets)     # NO scoped leg
    assert res.verified_claims


def test_empty_source_kinds_no_scoped_leg_even_when_on():
    rec, _ = _run(source_routing=True, source_kinds=[])               # flag on, agent chose not to route
    assert all(not f.get("source_kind") for f in rec.seen_facets)
