"""Corpus repository port + in-memory impl — domain-free.

Dedup rules: a Document is unique by (sha256) — re-ingesting identical bytes
reuses the row; BlockContent is unique by (content_key) — identical block text
across documents stores its embedding once.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Block, BlockContent, Document, ParsedDoc


@runtime_checkable
class CorpusRepository(Protocol):
    def upsert_document(self, doc: Document) -> str: ...
    def get_document(self, doc_id: str) -> Document | None: ...
    def add_parsed(self, parsed: ParsedDoc) -> None: ...
    def add_blocks(self, blocks: list[Block]) -> None: ...
    def upsert_block_content(self, bc: BlockContent) -> bool: ...  # True if newly stored
    def set_block_embedding(self, content_key: str, embedding: tuple[float, ...]) -> None: ...
    def pending_embeddings(self) -> list[tuple[str, str]]: ...  # (content_key, text) w/o embedding


class InMemoryCorpusRepository:
    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._by_sha: dict[str, str] = {}       # sha256 → doc_id (dedup)
        self._parsed: dict[str, ParsedDoc] = {}
        self._blocks: dict[str, list[Block]] = {}
        self._block_content: dict[str, BlockContent] = {}

    def upsert_document(self, doc: Document) -> str:
        existing = self._by_sha.get(doc.sha256)
        if existing is not None:
            return existing                      # identical bytes already ingested
        self._docs[doc.id] = doc
        self._by_sha[doc.sha256] = doc.id
        return doc.id

    def get_document(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    def add_parsed(self, parsed: ParsedDoc) -> None:
        self._parsed[parsed.document_id] = parsed

    def add_blocks(self, blocks: list[Block]) -> None:
        for b in blocks:
            self._blocks.setdefault(b.document_id, []).append(b)

    def upsert_block_content(self, bc: BlockContent) -> bool:
        if bc.content_key in self._block_content:
            return False
        self._block_content[bc.content_key] = bc
        return True

    def set_block_embedding(self, content_key: str, embedding: tuple[float, ...]) -> None:
        self._block_content[content_key].embedding = embedding

    def pending_embeddings(self) -> list[tuple[str, str]]:
        return [(ck, bc.text) for ck, bc in self._block_content.items() if bc.embedding is None]

    # accessors
    def iter_documents(self) -> list[Document]:
        return list(self._docs.values())

    def block_content(self, content_key: str) -> BlockContent | None:
        return self._block_content.get(content_key)

    # inspection helpers
    def document_count(self) -> int:
        return len(self._docs)

    def blocks_for(self, doc_id: str) -> list[Block]:
        return self._blocks.get(doc_id, [])

    def block_content_count(self) -> int:
        return len(self._block_content)
