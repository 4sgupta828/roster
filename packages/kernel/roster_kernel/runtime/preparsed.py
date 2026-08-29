"""A one-shot connector that feeds ALREADY-PARSED markdown through the normal ingest pipeline.

Domain-free (kernel): it names no source. Use it to ingest content that was fetched/parsed OUTSIDE
the usual connector fetch — e.g. a PDF downloaded + parsed on a machine with a good IP, then shipped
to a prod endpoint (the local→prod full-text bridge). Because `ingest_source` derives the document id
as `f"{source_key}:{native_id}"`, giving the SAME (source_key, native_id) an existing connector uses
makes this REPLACE that document's blocks via the corpus clean-replace — i.e. it recovers/enriches an
existing thin doc rather than creating a duplicate.
"""
from __future__ import annotations

from roster_kernel.contract.dto import DocumentRef, EntityRef


class PreParsedConnector:
    """Yields exactly one text/markdown document from supplied text. `key` is the source_key the
    ingest stamps and the document id is built from (match an existing connector's key to replace)."""

    def __init__(self, *, source_key: str, native_id: str, title: str, markdown: str,
                 facets: dict | None = None):
        self.key = source_key
        self._native_id = native_id
        self._title = title
        self._markdown = markdown
        self._facets = {str(k): str(v) for k, v in (facets or {}).items() if v}

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        return [EntityRef(source_key=self.key, native_id=self._native_id,
                          title=self._title, facets=self._facets)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        return [DocumentRef(source_key=self.key, native_id=self._native_id, title=self._title,
                            content_type="text/markdown", facets=self._facets,
                            entity_ids=(self._native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        return self._markdown.encode("utf-8")
