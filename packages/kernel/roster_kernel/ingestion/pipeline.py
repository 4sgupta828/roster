"""The generic acquisition pipeline: discover → list → fetch → store → document.

Ties a vertical's Connector to the kernel's ObjectStore + CorpusRepository. Fully
domain-neutral: entities/documents carry only `facets`. Parse → block → embed is
a later stage (needs a Parser + Embedder); this stage lands raw artifacts in the
content-addressed store and the document spine.
"""
from __future__ import annotations

from dataclasses import dataclass

from roster_kernel.contract.protocols import Connector
from roster_kernel.corpus import splitter as _splitter
from roster_kernel.corpus.models import BlockContent, Document, ParsedDoc
from roster_kernel.corpus.parsers import ParserRegistry
from roster_kernel.corpus.repository import CorpusRepository
from roster_kernel.ingestion.storage import ObjectStore
from roster_kernel.providers.embeddings import Embedder


@dataclass
class IngestSummary:
    entities: int = 0
    documents: int = 0
    objects_stored: int = 0     # unique bytes actually stored (dedup misses)


@dataclass
class IndexSummary:
    blocks: int = 0
    block_contents_stored: int = 0   # unique block texts embedded (dedup misses)


async def ingest_source(
    connector: Connector,
    store: ObjectStore,
    repo: CorpusRepository,
    *,
    tenant_id: str = "",
    workspace_id: str | None = None,
    window: dict | None = None,
) -> IngestSummary:
    summary = IngestSummary()
    entities = await connector.discover_entities(window or {})
    summary.entities = len(entities)
    for entity in entities:
        docs = await connector.list_documents(entity)
        for ref in docs:
            raw = await connector.fetch_artifact(ref)
            key = store.put(raw)
            doc = Document(
                id=f"{ref.source_key}:{ref.native_id}",
                sha256=key,
                content_type=ref.content_type,
                source_key=ref.source_key,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                title=ref.title,
                facets=dict(ref.facets),
                dates=dict(ref.dates),
                entity_ids=tuple(ref.entity_ids) or (entity.native_id,),
            )
            repo.upsert_document(doc)
            summary.documents += 1
    summary.objects_stored = getattr(store, "put_count", 0)
    return summary


def index_document(
    document: Document,
    raw: bytes,
    *,
    parsers: ParserRegistry,
    repo: CorpusRepository,
    embedder: Embedder | None = None,
    min_chars: int = 1,
    target_chars: int = 0,
) -> IndexSummary:
    """parse → block → (optionally embed) → persist for one Document.

    Separable from ingest_source so fetch and index can run on different workers.
    Only NEW block contents (by content_key) are registered/embedded — identical
    passages across documents are stored once. Pass embedder=None to DEFER
    embedding and run it as one batched pass later via `embed_pending` (far fewer
    API calls when ingesting many documents).
    """
    summary = IndexSummary()

    parser = parsers.for_content_type(document.content_type)
    text = parser.parse(raw, content_type=document.content_type)
    repo.add_parsed(ParsedDoc(document_id=document.id, text=text,
                              parser_version=getattr(parser, "version", "parser.v1")))

    blocks = _splitter.split(document.id, text, min_chars=min_chars, target_chars=target_chars)
    repo.add_blocks(blocks)
    summary.blocks = len(blocks)

    fresh = {b.content_key: b.text for b in blocks if repo.upsert_block_content(
        BlockContent(content_key=b.content_key, text=b.text))}
    summary.block_contents_stored = len(fresh)

    if embedder is not None and fresh:
        keys = list(fresh)
        vecs = embedder.embed([fresh[k] for k in keys])
        for k, vec in zip(keys, vecs):
            repo.set_block_embedding(k, tuple(vec))
    return summary


def embed_pending(repo: CorpusRepository, embedder: Embedder, *, batch_size: int = 256) -> int:
    """Embed all block contents lacking an embedding, in batches. Returns count.

    One batched pass over the whole corpus → O(n/batch) API calls instead of one
    per document. Deterministic order so runs are reproducible.
    """
    pending = repo.pending_embeddings()          # [(content_key, text)]
    for i in range(0, len(pending), batch_size):
        chunk = pending[i:i + batch_size]
        vecs = embedder.embed([t for _, t in chunk])
        for (ck, _), vec in zip(chunk, vecs):
            repo.set_block_embedding(ck, tuple(vec))
    return len(pending)
