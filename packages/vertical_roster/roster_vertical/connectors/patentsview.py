"""PatentsView connector — granted US patents (search.patentsview.org v1 API).

The v1 API requires a FREE API key (X-Api-Key). Set ROSTER_PATENTSVIEW_KEY; without it the connector
fails safe (discover → [], logged) rather than erroring an ingest job. Granted patents grade as
`primary_filing`. discover_entities({"query": topic | assignee, "limit": N}). Tests inject `patents`.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import patent_doc
from ._http import HttpStrategy

API = "https://search.patentsview.org/api/v1/patent/"
_FIELDS = ["patent_id", "patent_title", "patent_abstract", "patent_date",
           "assignees.assignee_organization", "cpc_current.cpc_group_id"]
_log = logging.getLogger(__name__)


class PatentsViewConnector:
    key = "patentsview"

    def __init__(self, *, patents: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy(base_delay=2.0, max_retries=4)
        self._page_size = page_size
        self._key = os.environ.get("ROSTER_PATENTSVIEW_KEY", "")
        self._by_id: dict[str, dict] = {}
        for p in (patents or []):
            self._by_id[patent_doc.patent_id(p)] = p

    async def _search(self, query: str, limit: int) -> list[dict]:
        if not self._key:
            _log.warning("patentsview: ROSTER_PATENTSVIEW_KEY not set — skipping (no patents ingested)")
            return []
        q = {"_text_any": {"patent_title": query, "patent_abstract": query}}
        params = {"q": json.dumps(q), "f": json.dumps(_FIELDS),
                  "o": json.dumps({"size": min(100, max(1, limit))})}
        url = API + "?" + urllib.parse.urlencode(params)
        raw = await self.fetch_strategy.fetch(url, headers={"X-Api-Key": self._key})
        return (json.loads(raw).get("patents") or [])[:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            patents = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip()
            limit = int((window or {}).get("limit", self._page_size))
            patents = await self._search(q, limit) if q else []
            for p in patents:
                self._by_id[patent_doc.patent_id(p)] = p
        return [EntityRef(source_key=self.key, native_id=patent_doc.patent_id(p),
                          title=patent_doc.title(p), facets=patent_doc.facets(p))
                for p in patents if patent_doc.patent_id(p)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        p = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=patent_doc.facets(p) if p else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        p = self._by_id.get(doc.native_id) or {"patent_id": doc.native_id}
        return patent_doc.to_markdown(p).encode("utf-8")
