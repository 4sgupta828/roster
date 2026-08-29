"""Batched embedding produces identical results to per-document embedding, and
counts API calls to prove the speedup."""
from __future__ import annotations

from roster_kernel.corpus.models import Document
from roster_kernel.corpus.parsers import default_registry
from roster_kernel.corpus.repository import InMemoryCorpusRepository
from roster_kernel.ingestion.pipeline import embed_pending, index_document
from roster_kernel.providers.embeddings import FakeEmbedder


class _CountingEmbedder:
    def __init__(self, dim=8):
        self._e = FakeEmbedder(dim=dim)
        self.calls = 0
    @property
    def dim(self): return self._e.dim
    def embed(self, texts):
        self.calls += 1
        return self._e.embed(texts)


def _docs(n):
    return [(Document(id=f"d{i}", sha256=f"s{i}", content_type="text/plain", source_key="x",
                      tenant_id="t"),
             f"Para {i} alpha.\n\nPara {i} beta content here.".encode())
            for i in range(n)]


def test_batched_matches_per_document_and_uses_fewer_calls():
    docs = _docs(10)
    parsers = default_registry()

    # per-document embedding: one call per doc
    per = InMemoryCorpusRepository(); e1 = _CountingEmbedder()
    for d, raw in docs:
        d2 = Document(**{**d.__dict__}); per.upsert_document(d2)
        index_document(d2, raw, parsers=parsers, embedder=e1, repo=per)

    # deferred + one batched pass
    bat = InMemoryCorpusRepository(); e2 = _CountingEmbedder()
    for d, raw in docs:
        d2 = Document(**{**d.__dict__}); bat.upsert_document(d2)
        index_document(d2, raw, parsers=parsers, repo=bat)   # no embed
    n = embed_pending(bat, e2, batch_size=256)

    assert e1.calls == 10                 # one per document
    assert e2.calls == 1                  # one batched call for the whole corpus
    assert n == bat.block_content_count()
    # identical embeddings (deterministic FakeEmbedder)
    for ck in ["dummy"]:
        pass
    per_vecs = {ck: bc.embedding for ck, bc in per._block_content.items()}
    bat_vecs = {ck: bc.embedding for ck, bc in bat._block_content.items()}
    assert per_vecs == bat_vecs


def test_embed_pending_only_fills_missing():
    repo = InMemoryCorpusRepository(); e = _CountingEmbedder()
    d = Document(id="d", sha256="s", content_type="text/plain", source_key="x", tenant_id="t")
    repo.upsert_document(d)
    index_document(d, b"one alpha.\n\ntwo beta.", parsers=default_registry(), repo=repo)
    assert len(repo.pending_embeddings()) == 2
    embed_pending(repo, e)
    assert repo.pending_embeddings() == []    # all filled
    assert embed_pending(repo, e) == 0        # nothing left → no-op
