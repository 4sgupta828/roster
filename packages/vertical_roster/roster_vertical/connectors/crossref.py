"""Crossref connector — Crossref REST API (public, no key; polite-pool `mailto`).

discover_entities → GET /works?query=… (or injected fixtures); one EntityRef per work (keyed by DOI).
list_documents → one synthesized markdown paper-document per work. fetch_artifact → markdown bytes.
DOI/venue authority + citation counts; complements OpenAlex + Semantic Scholar. Grades via the SAME
tier logic (`source_kind=paper` + `is_peer_reviewed`): journal/conference article →
`verified_structured`, `posted-content` preprint → `technical_signal`. Tests inject `works` (offline).
"""
from __future__ import annotations

import json
import os
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import crossref_doc as cr
from ._http import HttpStrategy

API = "https://api.crossref.org/works"
_MAILTO = os.environ.get("ROSTER_HTTP_CONTACT", "research@roster.dev")


class CrossrefConnector:
    key = "crossref"

    def __init__(self, *, works: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for w in (works or []):
            if cr.doi(w):
                self._by_id[cr.doi(w)] = w

    async def _search(self, query: str, limit: int, *, sort: str = "", from_year: str = "") -> list[dict]:
        n = min(100, max(1, limit))
        q = urllib.parse.quote(query)
        url = f"{API}?query={q}&rows={n}&select={urllib.parse.quote(cr.FIELDS)}&mailto={_MAILTO}"
        # freshness lane: a from-year floor and/or newest-first order (default = relevance, unchanged)
        if from_year and str(from_year)[:4].isdigit():
            url += f"&filter=from-pub-date:{str(from_year)[:4]}-01-01"
        if str(sort).lower() == "recent":
            url += "&sort=published&order=desc"
        data = json.loads(await self.fetch_strategy.fetch(url))
        items = ((data.get("message") or {}).get("items")) if isinstance(data, dict) else None
        return [w for w in (items or []) if isinstance(w, dict)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            works = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "large language models"
            limit = int((window or {}).get("limit", self._page_size))
            works = await self._search(q, limit, sort=str((window or {}).get("sort", "")),
                                       from_year=str((window or {}).get("from_year", "")))
            for w in works:
                if cr.doi(w):
                    self._by_id[cr.doi(w)] = w
        return [EntityRef(source_key=self.key, native_id=cr.doi(w),
                          title=cr.title(w), facets=cr.facets(w))
                for w in works if cr.doi(w)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        w = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=cr.facets(w) if w else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        w = self._by_id.get(doc.native_id)
        if w is None:
            url = f"{API}/{urllib.parse.quote(doc.native_id, safe='')}?mailto={_MAILTO}"
            data = json.loads(await self.fetch_strategy.fetch(url))
            w = (data.get("message") or {}) if isinstance(data, dict) else {}
            self._by_id[doc.native_id] = w
        return cr.to_markdown(w).encode("utf-8")
