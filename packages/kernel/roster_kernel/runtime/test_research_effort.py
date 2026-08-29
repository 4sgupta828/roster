"""Integration: effort threaded through ResearchService.ask actually scales the retrieval k and
budget that reach run_react — with no real LLM/credits (scripted LLM + spy source)."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.runtime.research import ResearchService

_TEXT = "The approved metric value was 9.8 percent for the term period."


class ScriptedLLM:
    def __init__(self, items):
        self._items = list(items)

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._items.pop(0), output_tokens=5, model="scripted")


class SpyK:
    """Wraps a source and records the retrieval `k` seen on each search()."""
    def __init__(self, inner):
        self.inner = inner
        self.ks: list[int] = []

    async def search(self, req):
        self.ks.append(req.k)
        return await self.inner.search(req)

    def make_block_loader(self, tenant_id, workspace_id=None):
        return self.inner.make_block_loader(tenant_id, workspace_id)


def _service_and_spy():
    inner = InMemoryRetrievalSource()
    inner.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"})))
    spy = SpyK(inner)
    llm = ScriptedLLM([
        AgentStep(action="search", query="metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="metric was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent")]),
        ComposedAnswer(answer="The metric value was 9.8 percent [1].", directly_addresses=True),
    ])
    return ResearchService(llm=llm, embedder=FakeEmbedder(dim=8), sources={"corpus": spy}), spy


def test_effort_1_0_uses_baseline_k_and_is_noop():
    svc, spy = _service_and_spy()
    res = asyncio.run(svc.ask(question="what was the metric value?", tenant_id="A", effort=1.0))
    assert spy.ks == [10]                 # baseline retrieval k, unchanged
    assert res.effort == 1.0
    assert res.grounded


def test_effort_2_5_scales_k_to_the_cap():
    svc, spy = _service_and_spy()
    res = asyncio.run(svc.ask(question="what was the metric value?", tenant_id="A", effort=2.5))
    assert spy.ks == [20]                 # 10*2.5=25 → clamped to the k cap (20)
    assert res.effort == 2.5
    assert res.grounded


def test_default_effort_is_baseline():
    svc, spy = _service_and_spy()
    asyncio.run(svc.ask(question="what was the metric value?", tenant_id="A"))
    assert spy.ks == [10]                 # no effort passed → 1.0 no-op
