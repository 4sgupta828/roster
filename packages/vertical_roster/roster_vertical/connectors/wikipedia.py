"""Wikipedia connector — KEYLESS MediaWiki API (en.wikipedia.org): tech-genesis/evolution + ecosystem history.

discover_entities → search by window["query"] (list=search) → one EntityRef per page. list_documents →
one encyclopedic-reference document per page. fetch_artifact → best-effort GET the plaintext extract
(prop=extracts|info|categories) for the page, then render markdown; on failure it falls back to the
search record. Everything grades `source_kind=reference` → `verified_structured` (a secondary source:
grounds history/genesis/ecosystem claims, never overrides a filing/patent/paper). Tests inject `pages`
(each may embed an "extract") so they run offline.
"""
from __future__ import annotations

import json
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import wikipedia_doc as wp
from ._http import HttpStrategy

API = "https://en.wikipedia.org/w/api.php"


class WikipediaConnector:
    key = "wikipedia"

    def __init__(self, *, pages: list[dict] | None = None, page_size: int = 15):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        # cache: str(pageid) -> page record (may embed "extract"/"categories")
        self._by_id: dict[str, dict] = {}
        for p in (pages or []):
            pid = wp.page_id(p)
            if pid:
                self._by_id[pid] = p

    async def _search(self, query: str, limit: int) -> list[dict]:
        n = min(50, max(1, limit))
        params = {"action": "query", "list": "search", "srsearch": query,
                  "srlimit": str(n), "format": "json", "formatversion": "2"}
        url = f"{API}?{urllib.parse.urlencode(params)}"
        data = json.loads(await self.fetch_strategy.fetch(url))
        hits = ((data.get("query") or {}).get("search")) or []
        return [h for h in hits if isinstance(h, dict) and str(h.get("title") or "").strip()][:limit]

    async def _fetch_page(self, rec: dict) -> dict:
        """Best-effort pull of the plaintext extract + categories for a page; merge onto rec."""
        pid = str(rec.get("pageid") or "").strip()
        params = {"action": "query", "prop": "extracts|info|categories",
                  "explaintext": "1", "exsectionformat": "plain",
                  "format": "json", "formatversion": "2", "redirects": "1"}
        if pid.isdigit():
            params["pageids"] = pid
        else:
            params["titles"] = wp.title(rec)
        url = f"{API}?{urllib.parse.urlencode(params)}"
        data = json.loads(await self.fetch_strategy.fetch(url))
        pages = ((data.get("query") or {}).get("pages")) or []
        page = next((p for p in pages if isinstance(p, dict)), None)
        merged = dict(rec)
        if page:
            merged.update({k: v for k, v in page.items() if v is not None})
        return merged

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        q = (window or {}).get("query", "").strip()
        if not q and self._by_id:
            pages = list(self._by_id.values())
        else:
            limit = int((window or {}).get("limit", self._page_size))
            pages = await self._search(q or "artificial intelligence", limit)
            for p in pages:
                pid = wp.page_id(p)
                if pid:
                    self._by_id[pid] = p
        return [EntityRef(source_key=self.key, native_id=wp.page_id(p),
                          title=wp.title(p), facets=wp.facets(p))
                for p in pages if wp.page_id(p)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        p = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=wp.facets(p) if p else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        rec = self._by_id.get(doc.native_id) or {"pageid": doc.native_id, "title": doc.title}
        if not str(rec.get("extract") or "").strip():
            try:
                rec = await self._fetch_page(rec)
                self._by_id[doc.native_id] = rec
            except Exception:
                pass   # best-effort: fall back to whatever record we have
        return wp.to_markdown(rec).encode("utf-8")
