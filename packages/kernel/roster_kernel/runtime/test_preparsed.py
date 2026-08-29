"""PreParsedConnector runs through the normal ingest path and lands blocks under the SAME
document_id an ordinary connector would (source_key:native_id) — the property that makes the
local→prod PDF bridge REPLACE an abstract stub rather than create a duplicate. Offline, no network."""
from __future__ import annotations

import asyncio

from roster_kernel.corpus.parsers import default_registry
from roster_kernel.corpus.repository import InMemoryCorpusRepository
from roster_kernel.ingestion.pipeline import index_document, ingest_source
from roster_kernel.ingestion.storage import InMemoryObjectStore
from roster_kernel.retrieval.materialize import materialize
from roster_kernel.retrieval.memory import InMemoryRetrievalSource
from roster_kernel.runtime.preparsed import PreParsedConnector


def _ingest(conn) -> InMemoryRetrievalSource:
    store, repo, parsers = InMemoryObjectStore(), InMemoryCorpusRepository(), default_registry()
    asyncio.run(ingest_source(conn, store, repo, tenant_id="demo"))
    for doc in repo.iter_documents():
        index_document(doc, store.get(doc.sha256), parsers=parsers, repo=repo,
                       min_chars=40, target_chars=1800)
    src = InMemoryRetrievalSource()
    materialize(repo, src)
    return src, repo


def test_lands_blocks_under_source_key_native_id():
    body = "## Full text\n\n" + ("The system processes the whole sequence in one structured pass. " * 200)
    conn = PreParsedConnector(source_key="src", native_id="123.456",
                              title="A Long Document", markdown="# A Long Document\n\n" + body,
                              facets={"source_kind": "paper", "year": "2017"})
    src, repo = _ingest(conn)
    docs = list(repo.iter_documents())
    assert len(docs) == 1
    # document_id derived as source_key:native_id → matches the same-keyed connector's id (the
    # clean-replace target that makes a re-ingest ENRICH an existing thin doc, not duplicate it)
    assert docs[0].id == "src:123.456"
    assert docs[0].content_type == "text/markdown"
    assert docs[0].facets.get("source_kind") == "paper"
    # the full body is ingested (not a short stub) — content present across the block(s)
    blocks = repo.blocks_for("src:123.456")
    assert len(blocks) >= 1
    assert sum(len(b.text) for b in blocks) > 5000            # full body, dwarfs a metadata stub
    assert any("structured pass" in b.text for b in blocks)
