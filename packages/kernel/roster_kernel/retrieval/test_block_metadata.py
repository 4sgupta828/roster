"""Block-level metadata: facets denormalized to blocks (+ per-block override) and
generic document provenance carried on hits — the basis for narrowed search."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.corpus.models import Block, Document
from roster_kernel.corpus.repository import InMemoryCorpusRepository
from roster_kernel.ingestion.pipeline import index_document
from roster_kernel.corpus.parsers import default_registry
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.retrieval.materialize import materialize
from roster_kernel.retrieval.memory import InMemoryRetrievalSource


def _index(tenant="t"):
    repo = InMemoryCorpusRepository()
    emb = FakeEmbedder(dim=8)
    doc = Document(id="d1", sha256="s", content_type="text/markdown", source_key="src",
                   tenant_id=tenant, title="Annual Report 2024",
                   facets={"region": "north", "year": "2024", "kind": "report"})
    repo.upsert_document(doc)
    index_document(doc, b"The value is 9.6 percent.\n\nMore context here.",
                   parsers=default_registry(), embedder=emb, repo=repo)
    src = InMemoryRetrievalSource()
    materialize(repo, src)
    return src, emb


def _run(src, req):
    return asyncio.run(src.search(req))


def test_block_carries_document_facets_and_provenance() -> None:
    src, _ = _index()
    hits = _run(src, RetrievalRequest(query="value percent", tenant_id="t"))
    assert hits
    h = hits[0]
    assert h.facets == {"region": "north", "year": "2024", "kind": "report"}
    assert h.document_title == "Annual Report 2024"
    assert h.content_type == "text/markdown"
    assert h.source_key == "src"


def test_search_narrows_by_facets() -> None:
    src, _ = _index()
    # matching narrowing facets → returned
    assert _run(src, RetrievalRequest(query="value percent", tenant_id="t",
                                      facets={"region": "north", "kind": "report"}))
    # a non-matching facet → filtered out before ranking
    assert not _run(src, RetrievalRequest(query="value percent", tenant_id="t",
                                          facets={"year": "2023"}))
    # IN-set membership across years
    assert _run(src, RetrievalRequest(query="value percent", tenant_id="t",
                                      facets={"year": ("2023", "2024")}))


def test_per_block_facets_override_document() -> None:
    # a block tagged with an extra/overriding facet beats the document's value
    repo = InMemoryCorpusRepository()
    repo.upsert_document(Document(id="d", sha256="s", content_type="text/plain",
                                  source_key="src", tenant_id="t",
                                  facets={"region": "north", "topic": "returns"}))
    repo.add_blocks([Block(document_id="d", index=0, content_key="k", text="special",
                           facets={"topic": "structure"})])
    from roster_kernel.corpus.models import BlockContent
    repo.upsert_block_content(BlockContent(content_key="k", text="special"))
    src = InMemoryRetrievalSource()
    materialize(repo, src)
    hits = _run(src, RetrievalRequest(query="special", tenant_id="t",
                                      facets={"topic": "structure", "region": "north"}))
    assert hits and hits[0].facets["topic"] == "structure"  # block override won
