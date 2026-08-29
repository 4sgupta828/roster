"""OpenReview connector — peer-REVIEW-scored research (public API v2, KEYLESS).

Separates reviewed/accepted work from unreviewed arXiv preprints for AI diligence: an accepted
note grades `verified_structured` (via `openreview_doc.facets` stamping `is_peer_reviewed=true`
+ `source_kind=paper`), an un-accepted submission grades `technical_signal` — the same tier as a
raw preprint.

discover_entities → GET /notes/search?term=…&limit=n (or injected fixture notes); one EntityRef
per note. list_documents → one markdown paper-document per note id. fetch_artifact → the assembled
markdown bytes. Tests inject `notes` so they run offline.
"""
from __future__ import annotations

import json
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import openreview_doc as ordoc
from ._http import HttpStrategy

SEARCH = "https://api2.openreview.net/notes/search"


class OpenReviewConnector:
    key = "openreview"

    def __init__(self, *, notes: list[dict] | None = None, page_size: int = 20):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for n in (notes or []):
            if ordoc.note_id(n) and ordoc.title(n):
                self._by_id[ordoc.note_id(n)] = n

    async def _search(self, query: str, limit: int) -> list[dict]:
        n = min(50, max(1, limit))
        q = urllib.parse.quote(query)
        url = f"{SEARCH}?term={q}&limit={n}"
        data = json.loads(await self.fetch_strategy.fetch(url))
        raw = data.get("notes")
        if not isinstance(raw, list):
            raw = data.get("results") or []
        # defensive: only keep notes that actually carry a title value
        return [x for x in raw if isinstance(x, dict) and ordoc.title(x)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            notes = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "large language models"
            limit = int((window or {}).get("limit", self._page_size))
            notes = await self._search(q, limit)
            for n in notes:
                if ordoc.note_id(n) and ordoc.title(n):
                    self._by_id[ordoc.note_id(n)] = n
        return [EntityRef(source_key=self.key, native_id=ordoc.note_id(n),
                          title=ordoc.title(n), facets=ordoc.facets(n))
                for n in notes if ordoc.note_id(n)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        n = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=ordoc.facets(n) if n else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        n = self._by_id.get(doc.native_id) or {"id": doc.native_id,
                                               "content": {"title": {"value": doc.title}}}
        return ordoc.to_markdown(n).encode("utf-8")
