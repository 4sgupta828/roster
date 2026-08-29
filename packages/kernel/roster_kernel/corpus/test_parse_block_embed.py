"""Offline tests for parse → block → embed. Domain-neutral."""
from __future__ import annotations

from roster_kernel.corpus import splitter
from roster_kernel.corpus.models import Document
from roster_kernel.corpus.parsers import default_registry
from roster_kernel.corpus.repository import InMemoryCorpusRepository
from roster_kernel.ingestion.pipeline import index_document
from roster_kernel.ingestion.storage import content_key
from roster_kernel.providers.embeddings import FakeEmbedder


# ---- parsers -------------------------------------------------------------

def test_parser_dispatch_by_content_type() -> None:
    reg = default_registry()
    assert reg.for_content_type("text/plain; charset=utf-8").parse(b"hi", content_type="text/plain") == "hi"
    html = b"<html><body><h1>Title</h1><script>ignore()</script><p>Body text.</p></body></html>"
    out = reg.for_content_type("text/html").parse(html, content_type="text/html")
    assert "Title" in out and "Body text." in out and "ignore" not in out


def test_parser_missing_type_raises() -> None:
    reg = default_registry()
    try:
        reg.for_content_type("application/pdf")
        assert False, "expected KeyError"
    except KeyError:
        pass


# ---- splitter ------------------------------------------------------------

def test_splitter_offsets_and_sections() -> None:
    text = "# Heading A\n\nFirst paragraph.\n\n## Sub B\n\nSecond paragraph here."
    blocks = splitter.split("doc1", text)
    assert [b.text for b in blocks] == ["First paragraph.", "Second paragraph here."]
    # section paths track the heading stack
    assert blocks[0].section_path == ("Heading A",)
    assert blocks[1].section_path == ("Heading A", "Sub B")
    # offsets point at the real substring
    b0 = blocks[0]
    assert text[b0.char_start:b0.char_end] == "First paragraph."
    # content_key is sha256 of the block text
    assert b0.content_key == content_key(b"First paragraph.")


def test_splitter_is_deterministic() -> None:
    text = "Alpha.\n\nBeta.\n\nGamma."
    assert [b.content_key for b in splitter.split("d", text)] == \
           [b.content_key for b in splitter.split("d", text)]


# ---- index stage (parse → block → embed → persist) -----------------------

def _doc(doc_id: str, ct: str = "text/plain") -> Document:
    return Document(id=doc_id, sha256=doc_id, content_type=ct, source_key="s")


def test_index_document_embeds_and_persists() -> None:
    repo = InMemoryCorpusRepository()
    emb = FakeEmbedder(dim=16)
    raw = b"Para one.\n\nPara two."
    s = index_document(_doc("d1"), raw, parsers=default_registry(), embedder=emb, repo=repo)
    assert s.blocks == 2
    assert s.block_contents_stored == 2
    assert repo.block_content_count() == 2
    # embeddings were set
    ck = content_key(b"Para one.")
    assert repo._block_content[ck].embedding is not None
    assert len(repo._block_content[ck].embedding) == 16


def test_identical_block_across_docs_embedded_once() -> None:
    repo = InMemoryCorpusRepository()
    emb = FakeEmbedder(dim=8)
    shared = b"Shared passage.\n\nUnique to one."
    index_document(_doc("d1"), shared, parsers=default_registry(), embedder=emb, repo=repo)
    # second doc repeats the shared passage
    s2 = index_document(_doc("d2"), b"Shared passage.\n\nUnique to two.",
                        parsers=default_registry(), embedder=emb, repo=repo)
    # only the NEW unique block is embedded the second time (shared deduped)
    assert s2.block_contents_stored == 1
    # total distinct block contents = shared + unique1 + unique2 = 3
    assert repo.block_content_count() == 3
