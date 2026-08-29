"""OpenAlex connector — 250M+ scholarly works (public API, no key).

discover_entities → GET /works?search=… (or injected fixtures); one EntityRef per work.
list_documents → one synthesized markdown paper-document per work. fetch_artifact → markdown bytes.
Peer-reviewed journal articles grade as `verified_structured` (a tier above arXiv preprints).
Tests inject `works` so they run offline. Politely include a mailto contact (OpenAlex "polite pool").
"""
from __future__ import annotations

import os

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import openalex_doc
from ._http import HttpStrategy

API = "https://api.openalex.org/works"
_MAILTO = os.environ.get("ROSTER_HTTP_CONTACT", "research@roster.dev")


class OpenAlexConnector:
    key = "openalex"

    def __init__(self, *, works: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for w in (works or []):
            self._by_id[openalex_doc.openalex_id(w)] = w

    async def _search(self, query: str, limit: int, *, sort: str = "", from_year: str = "") -> list[dict]:
        import json
        import urllib.parse
        per = min(200, max(1, limit))
        # SEMINAL ingest lane (sort=cited): the FOUNDATIONAL works OF THE TOPIC. Broad full-text
        # search + citation sort fails — it returns universally-mega-cited papers (SciPy, etc.) that
        # loosely match. Restrict to TITLE matches, then sort by citations, so the top hits are the
        # seminal papers of that topic (e.g. the original "Retrieval-Augmented Generation…" paper).
        filters = []
        if str(sort).lower() == "cited":
            filters.append("title.search:" + urllib.parse.quote(query))
            url = f"{API}?per_page={per}&mailto={_MAILTO}&sort=cited_by_count:desc"
        else:
            url = f"{API}?search={query.replace(' ', '%20')}&per_page={per}&mailto={_MAILTO}"
            if str(sort).lower() == "recent":
                url += "&sort=publication_date:desc"
        # freshness floor (any lane): a from-year filter so a lane can capture recent works.
        if from_year and str(from_year)[:4].isdigit():
            filters.append(f"from_publication_date:{str(from_year)[:4]}-01-01")
        if filters:
            url += "&filter=" + ",".join(filters)
        data = json.loads(await self.fetch_strategy.fetch(url))
        return (data.get("results") or [])[:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            works = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "large language models"
            limit = int((window or {}).get("limit", self._page_size))
            works = await self._search(q, limit, sort=str((window or {}).get("sort", "")),
                                       from_year=str((window or {}).get("from_year", "")))
            for w in works:
                self._by_id[openalex_doc.openalex_id(w)] = w
        return [EntityRef(source_key=self.key, native_id=openalex_doc.openalex_id(w),
                          title=openalex_doc.title(w), facets=openalex_doc.facets(w))
                for w in works if openalex_doc.openalex_id(w)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        w = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=openalex_doc.facets(w) if w else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        w = self._by_id.get(doc.native_id)
        if w is None:
            import json
            w = json.loads(await self.fetch_strategy.fetch(f"{API}/{doc.native_id}?mailto={_MAILTO}"))
            self._by_id[doc.native_id] = w
        return openalex_doc.to_markdown(w).encode("utf-8")
