"""NIH RePORTER connector — US National Institutes of Health grants (KEYLESS public API, POST).

discover_entities → POST /v2/projects/search with an advanced_text_search body (or injected
fixtures); one EntityRef per project. list_documents → one government-grant document per project.
fetch_artifact → markdown bytes. Every project grades `verified_structured` via `source_kind=funding`
— an attested funding signal revealing WHO is funded to build WHAT. Uses httpx POST directly (like
reddit.py's token POST) since RePORTER's search is a JSON-body endpoint. Tests inject `projects` so
they run offline. Fail-safe: a live-fetch failure or empty response yields [] / a metadata-only doc,
never an exception into an ingest job.
"""
from __future__ import annotations

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import nih_reporter_doc as nih
from ._http import USER_AGENT, HttpStrategy

API = "https://api.reporter.nih.gov/v2/projects/search"


class NihReporterConnector:
    key = "nih_reporter"

    def __init__(self, *, projects: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for p in (projects or []):
            if nih.project_id(p):
                self._by_id[nih.project_id(p)] = p

    async def _search(self, query: str, limit: int) -> list[dict]:
        n = min(500, max(1, limit))  # RePORTER limit caps at 500
        body = {
            "criteria": {
                "advanced_text_search": {
                    "operator": "and",
                    "search_field": "projecttitle,abstracttext,terms",
                    "search_text": query,
                }
            },
            "limit": n,
            "offset": 0,
        }
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.post(API, json=body, headers={"User-Agent": USER_AGENT})
                r.raise_for_status()
                data = r.json() or {}
        except Exception:
            return []  # fail-safe: never raise into an ingest job
        results = (data.get("results") or [])
        return [p for p in results if isinstance(p, dict) and nih.title(p)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            projects = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "artificial intelligence"
            limit = int((window or {}).get("limit", self._page_size))
            projects = await self._search(q, limit)
            for p in projects:
                if nih.project_id(p):
                    self._by_id[nih.project_id(p)] = p
        return [EntityRef(source_key=self.key, native_id=nih.project_id(p),
                          title=nih.title(p), facets=nih.facets(p))
                for p in projects if nih.project_id(p)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        p = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=nih.facets(p) if p else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        p = self._by_id.get(doc.native_id) or {"project_num": doc.native_id, "project_title": doc.title}
        return nih.to_markdown(p).encode("utf-8")
