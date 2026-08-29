"""Offline tests for the ingestion primitives + pipeline. Domain-neutral fakes."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import DocumentRef, EntityRef
from roster_kernel.corpus.models import Block, BlockContent, Document, ParsedDoc
from roster_kernel.corpus.repository import InMemoryCorpusRepository
from roster_kernel.ingestion.pipeline import ingest_source
from roster_kernel.ingestion.queue import InMemoryJobQueue, JobStatus
from roster_kernel.ingestion.storage import InMemoryObjectStore, content_key


# ---- queue ---------------------------------------------------------------

def test_queue_priority_and_completion() -> None:
    q = InMemoryJobQueue()
    q.enqueue("discover", {}, priority=0)
    hi = q.enqueue("fetch", {"u": 1}, priority=10)
    job = q.claim(now=0)
    assert job.id == hi                      # highest priority first
    q.complete(job.id)
    assert q.get(hi).status is JobStatus.DONE


def test_queue_backoff_then_fail() -> None:
    q = InMemoryJobQueue()
    jid = q.enqueue("fetch", {})
    j = q.claim(now=0)
    q.fail(j.id, "boom", max_attempts=2, backoff=5, now=0)
    assert q.get(jid).status is JobStatus.PENDING     # retry scheduled
    assert q.claim(now=0) is None                     # but not until run_after
    j2 = q.claim(now=5)                               # eligible after backoff
    assert j2.id == jid
    q.fail(j2.id, "boom again", max_attempts=2, backoff=5, now=5)
    assert q.get(jid).status is JobStatus.FAILED      # attempts exhausted


# ---- storage -------------------------------------------------------------

def test_object_store_content_addressed_dedup() -> None:
    s = InMemoryObjectStore()
    k1 = s.put(b"hello")
    k2 = s.put(b"hello")       # identical bytes
    k3 = s.put(b"world")
    assert k1 == k2 == content_key(b"hello")
    assert k3 != k1
    assert s.put_count == 2    # only two unique objects stored


# ---- corpus repo ---------------------------------------------------------

def test_document_dedup_by_sha_and_version_lineage() -> None:
    repo = InMemoryCorpusRepository()
    d1 = Document(id="s:1", sha256="abc", content_type="application/pdf", source_key="s")
    assert repo.upsert_document(d1) == "s:1"
    # same bytes, different id → dedup returns the original id
    d1b = Document(id="s:1-dup", sha256="abc", content_type="application/pdf", source_key="s")
    assert repo.upsert_document(d1b) == "s:1"
    assert repo.document_count() == 1
    # a new version (different bytes) supersedes the prior
    d2 = Document(id="s:1v2", sha256="def", content_type="application/pdf",
                  source_key="s", supersedes="s:1")
    repo.upsert_document(d2)
    assert repo.document_count() == 2
    assert repo.get_document("s:1v2").supersedes == "s:1"


def test_block_content_dedup() -> None:
    repo = InMemoryCorpusRepository()
    repo.add_blocks([Block(document_id="d", index=0, content_key="k", text="shared")])
    assert repo.upsert_block_content(BlockContent(content_key="k", text="shared")) is True
    assert repo.upsert_block_content(BlockContent(content_key="k", text="shared")) is False
    assert repo.block_content_count() == 1
    repo.add_parsed(ParsedDoc(document_id="d", text="shared", parser_version="v1"))


# ---- pipeline (discover → list → fetch → store → document) ---------------

class _FakeStrategy:
    egress_class = "datacenter"
    engine = "http"
    proxy_enabled = False
    async def fetch(self, url, **opts):  # noqa: ANN001
        return b""


class _FakeConnector:
    """Two entities; both reference the SAME document bytes (n:m + dedup)."""
    def __init__(self):
        self.key = "src"
        self.fetch_strategy = _FakeStrategy()
    async def discover_entities(self, window):  # noqa: ANN001
        return [EntityRef(source_key="src", native_id="e1"),
                EntityRef(source_key="src", native_id="e2")]
    async def list_documents(self, entity):  # noqa: ANN001
        # both entities list one doc each, but with identical bytes on fetch
        return [DocumentRef(source_key="src", native_id=f"doc-{entity.native_id}",
                            content_type="text/plain", facets={"region": "north"})]
    async def fetch_artifact(self, doc):  # noqa: ANN001
        return b"IDENTICAL BODY"       # same bytes for every doc


def test_pipeline_ingests_and_dedupes() -> None:
    store = InMemoryObjectStore()
    repo = InMemoryCorpusRepository()
    summary = asyncio.run(ingest_source(_FakeConnector(), store, repo))
    assert summary.entities == 2
    assert summary.documents == 2           # two DocumentRefs processed
    assert summary.objects_stored == 1      # identical bytes stored once (dedup)
    assert repo.document_count() == 1       # same sha → one Document
