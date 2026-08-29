"""Multi-source research: corpus + web + workspace combinations, offline."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator, RetrievalRequest
from roster_kernel.contract.protocols import RetrievalSource
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.providers.websearch import FakeWebSearch, WebResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.provenance import BlockSpanVerifier
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.retrieval.multi import MultiSourceRetriever
from roster_kernel.retrieval.web import WebRetrievalSource

_CORPUS_TEXT = "The committee approved a target value of nine point six percent this period."
_WEB_BODY = "According to the press summary, the transaction closed on schedule in Q3."


def _corpus(tenant="t") -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="c1", document_id="doc", tenant_id=tenant, text=_CORPUS_TEXT,
                         locator=Locator("block_span", "doc", {"block_id": "c1"}), source_key="corpus"))
    return src


def _web() -> WebRetrievalSource:
    return WebRetrievalSource(FakeWebSearch({
        "target value transaction": [
            WebResult(url="https://ex.com/a", title="Deal news", snippet="", body=_WEB_BODY)],
    }))


def test_sources_satisfy_protocol() -> None:
    assert isinstance(_web(), RetrievalSource)
    assert isinstance(MultiSourceRetriever({"corpus": _corpus(), "web": _web()}), RetrievalSource)


def test_web_grounding_rejects_fabrication() -> None:
    web = _web()
    hits = asyncio.run(web.search(RetrievalRequest(query="target value transaction", tenant_id="t")))
    assert hits and hits[0].source_key == "web"
    v = BlockSpanVerifier(web.make_block_loader("t"))
    assert v.verify("the transaction closed on schedule", hits[0].locator)
    assert not v.verify("the transaction was cancelled", hits[0].locator)     # fabricated


def test_multi_source_fuses_corpus_and_web() -> None:
    multi = MultiSourceRetriever({"corpus": _corpus(), "web": _web()})
    hits = asyncio.run(multi.search(RetrievalRequest(query="target value transaction", tenant_id="t", k=10)))
    sources = {h.source_key for h in hits}
    assert sources == {"corpus", "web"}                # answer draws from both


def test_cross_source_tenant_isolation() -> None:
    # corpus is tenant-scoped; a foreign-tenant corpus block never surfaces.
    corpus = _corpus(tenant="t")
    corpus.add(IndexedBlock(block_id="secret", document_id="d2", tenant_id="other",
                            text="target value secret for another tenant", source_key="corpus"))
    multi = MultiSourceRetriever({"corpus": corpus, "web": _web()})
    hits = asyncio.run(multi.search(RetrievalRequest(query="target value transaction", tenant_id="t", k=10)))
    assert "secret" not in {h.block_id for h in hits}


class _LLM:
    def __init__(self, steps): self._s = list(steps)
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._s.pop(0), output_tokens=5)


def test_run_react_answers_from_corpus_and_web() -> None:
    multi = MultiSourceRetriever({"corpus": _corpus(), "web": _web()})
    emb = FakeEmbedder(dim=16)
    # fused order is deterministic: "corpus::.." < "web::.." → a1=corpus, a2=web
    llm = _LLM([
        AgentStep(action="search", query="target value transaction"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="approved value", atom_id="a1",
                     quote="approved a target value of nine point six percent"),
            ClaimOut(text="deal timing", atom_id="a2",
                     quote="the transaction closed on schedule"),
        ]),
    ])
    res = asyncio.run(run_react(
        question="what was approved and when did the deal close?", llm=llm, embedder=emb,
        source=multi, tenant_id="t", budget=BudgetState(max_calls=10)))
    assert res.grounded
    assert len(res.verified_claims) == 2                # one corpus-cited, one web-cited
    assert {c.atom_id for c in res.verified_claims} == {"a1", "a2"}


def test_failing_source_is_skipped_not_fatal():
    class _Boom:
        key = "boom"
        def capabilities(self): 
            from roster_kernel.contract.dto import Capability
            return frozenset({Capability.RETRIEVAL})
        def covers(self): return {}
        def make_block_loader(self, t, w=None): return lambda d,b: None
        async def search(self, req): raise RuntimeError("provider down")
    multi = MultiSourceRetriever({"corpus": _corpus(), "boom": _Boom()})
    hits = asyncio.run(multi.search(RetrievalRequest(query="target value approved period", tenant_id="t", k=10)))
    assert hits                                   # corpus still answered
    assert multi.failed_sources.get("boom", "").startswith("RuntimeError")
