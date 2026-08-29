"""End-to-end kernel integration (offline): ingest -> parse/block/embed ->
materialize -> retrieve -> ReAct -> grounded answer. Proves the whole spine
composes and stays tenant-scoped. Domain-neutral fakes throughout."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import DocumentRef, EntityRef
from roster_kernel.corpus.parsers import default_registry
from roster_kernel.corpus.repository import InMemoryCorpusRepository
from roster_kernel.ingestion.pipeline import index_document, ingest_source
from roster_kernel.ingestion.storage import InMemoryObjectStore
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.materialize import materialize
from roster_kernel.retrieval.memory import InMemoryRetrievalSource

_BODY = b"Overview paragraph.\n\nThe approved metric value was 9.8 percent this period."


class _Strategy:
    egress_class = "datacenter"; engine = "http"; proxy_enabled = False
    async def fetch(self, url, **o):  # noqa: ANN001
        return b""


class _Connector:
    def __init__(self):
        self.key = "src"; self.fetch_strategy = _Strategy()
    async def discover_entities(self, window):  # noqa: ANN001
        return [EntityRef(source_key="src", native_id="e1")]
    async def list_documents(self, entity):  # noqa: ANN001
        return [DocumentRef(source_key="src", native_id="doc1", content_type="text/plain",
                            facets={"region": "north"})]
    async def fetch_artifact(self, doc):  # noqa: ANN001
        return _BODY


class _LLM:
    def __init__(self, steps): self._s = list(steps)
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._s.pop(0), output_tokens=5)


def _build_index(tenant="acme"):
    store, repo = InMemoryObjectStore(), InMemoryCorpusRepository()
    emb = FakeEmbedder(dim=8)
    # ingest (fetch -> store -> Document), then index each stored doc
    asyncio.run(ingest_source(_Connector(), store, repo, tenant_id=tenant))
    for doc in repo.iter_documents():
        index_document(doc, store.get(doc.sha256), parsers=default_registry(), embedder=emb, repo=repo)
    source = InMemoryRetrievalSource()
    materialize(repo, source)
    return source, emb


def test_end_to_end_grounded_answer() -> None:
    source, emb = _build_index()
    llm = _LLM([
        AgentStep(action="search", query="metric value percent"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent this period"),
        ]),
    ])
    res = asyncio.run(run_react(
        question="what was the metric value?", llm=llm, embedder=emb, source=source,
        tenant_id="acme", budget=BudgetState(max_calls=10),
    ))
    assert res.atoms_gathered >= 1
    assert res.grounded
    assert res.verified_claims[0].quote.startswith("the approved metric value")


def test_end_to_end_other_tenant_sees_nothing() -> None:
    source, emb = _build_index(tenant="acme")
    llm = _LLM([AgentStep(action="search", query="metric value percent"),
                AgentStep(action="answer", claims=[])])
    res = asyncio.run(run_react(
        question="?", llm=llm, embedder=emb, source=source,
        tenant_id="other", budget=BudgetState(max_calls=10),
    ))
    assert res.atoms_gathered == 0     # different tenant -> no evidence visible
