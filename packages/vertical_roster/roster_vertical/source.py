"""TechRetrievalSource — a corpus over the bundled sample filings + papers (offline).

Builds by running each fixture record through its doc-synthesizer -> the REAL kernel pipeline
(parse -> split -> embed) -> materialize, so every BlockHit has a verifiable block_span
locator. Production uses PostgresRetrievalSource fed by ingest_connector_to_postgres(
EdgarConnector/ArxivConnector, ...). covers() = {} (the corpus spans all sectors).
"""
from __future__ import annotations

from roster_kernel.contract.dto import BlockHit, Capability, FacetFilter, RetrievalRequest
from roster_kernel.corpus.models import Document
from roster_kernel.corpus.parsers import default_registry
from roster_kernel.corpus.repository import InMemoryCorpusRepository
from roster_kernel.ingestion.pipeline import index_document
from roster_kernel.ingestion.storage import InMemoryObjectStore
from roster_kernel.providers.embeddings import Embedder, FakeEmbedder
from roster_kernel.retrieval.materialize import materialize
from roster_kernel.retrieval.memory import InMemoryRetrievalSource

from . import filing_doc, paper_doc
from .fixtures import sample_filings, sample_papers


class TechRetrievalSource:
    key = "corpus"

    def __init__(self, *, tenant_id: str = "demo", embedder: Embedder | None = None,
                 filings: list[dict] | None = None, papers: list[dict] | None = None):
        self._inner = InMemoryRetrievalSource()
        self._build(tenant_id, embedder or FakeEmbedder(dim=16),
                    filings if filings is not None else sample_filings(),
                    papers if papers is not None else sample_papers())

    def _build(self, tenant_id, embedder, filings, papers):
        store, repo = InMemoryObjectStore(), InMemoryCorpusRepository()
        parsers = default_registry()

        def _add(source_key, native_id, title, md_bytes, facets):
            sha = store.put(md_bytes)
            doc = Document(id=f"{source_key}:{native_id}", sha256=sha, content_type="text/markdown",
                           source_key=source_key, tenant_id=tenant_id, title=title, facets=facets)
            repo.upsert_document(doc)
            index_document(doc, md_bytes, parsers=parsers, embedder=embedder, repo=repo)

        for rec in filings:
            _add("edgar", filing_doc.accession(rec), filing_doc.title(rec),
                 filing_doc.to_markdown(rec).encode("utf-8"), filing_doc.facets(rec))
        for rec in papers:
            _add("arxiv", paper_doc.arxiv_id(rec), paper_doc.title(rec),
                 paper_doc.to_markdown(rec).encode("utf-8"), paper_doc.facets(rec))
        materialize(repo, self._inner)

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.RETRIEVAL, Capability.PRECISION})

    def covers(self) -> FacetFilter:
        return {}

    def make_block_loader(self, tenant_id: str, workspace_id: str | None = None):
        return self._inner.make_block_loader(tenant_id, workspace_id)

    async def search(self, req: RetrievalRequest) -> list[BlockHit]:
        return await self._inner.search(req)
