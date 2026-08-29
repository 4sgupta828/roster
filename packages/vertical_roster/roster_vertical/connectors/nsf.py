"""NSF Awards connector — US National Science Foundation grants (KEYLESS public API).

discover_entities → GET /services/v1/awards.json?keyword=… (or injected fixtures); one EntityRef per
award. list_documents → one government-grant document per award. fetch_artifact → markdown bytes.
Every award grades `verified_structured` via `source_kind=funding` — an attested funding signal that
reveals WHO is funded to build WHAT (whitespace/opportunity spotting). Tests inject `awards` so they
run offline. Fail-safe: a live-fetch failure or empty response yields [] / a metadata-only doc, never
an exception into an ingest job.
"""
from __future__ import annotations

import json
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import nsf_doc
from ._http import HttpStrategy

API = "https://api.nsf.gov/services/v1/awards.json"
_PRINT_FIELDS = "id,title,abstractText,awardeeName,piFirstName,piLastName,startDate,fundsObligatedAmt,agency"


class NsfConnector:
    key = "nsf"

    def __init__(self, *, awards: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for a in (awards or []):
            if nsf_doc.award_id(a):
                self._by_id[nsf_doc.award_id(a)] = a

    async def _search(self, query: str, limit: int) -> list[dict]:
        n = min(25, max(1, limit))  # NSF rpp caps at 25 per page
        q = urllib.parse.quote(query)
        url = f"{API}?keyword={q}&rpp={n}&printFields={_PRINT_FIELDS}"
        try:
            data = json.loads(await self.fetch_strategy.fetch(url))
        except Exception:
            return []  # fail-safe: never raise into an ingest job
        awards = (((data or {}).get("response") or {}).get("award")) or []
        return [a for a in awards if isinstance(a, dict) and nsf_doc.title(a)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            awards = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "artificial intelligence"
            limit = int((window or {}).get("limit", self._page_size))
            awards = await self._search(q, limit)
            for a in awards:
                if nsf_doc.award_id(a):
                    self._by_id[nsf_doc.award_id(a)] = a
        return [EntityRef(source_key=self.key, native_id=nsf_doc.award_id(a),
                          title=nsf_doc.title(a), facets=nsf_doc.facets(a))
                for a in awards if nsf_doc.award_id(a)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        a = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=nsf_doc.facets(a) if a else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        a = self._by_id.get(doc.native_id) or {"id": doc.native_id, "title": doc.title}
        return nsf_doc.to_markdown(a).encode("utf-8")
