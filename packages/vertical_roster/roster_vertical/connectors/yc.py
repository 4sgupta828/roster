"""Y Combinator company-directory connector — the startup POPULATION seed (public Algolia index).

YC's directory at ycombinator.com/companies is a static React app backed by a PUBLIC, search-only
Algolia index (`YCCompany_production`). The app id + search key are embedded client-side (that's
normal + intended for public search — the key is scoped to `ycdc_public` and read-only). This pulls
the roster of YC-backed companies (name, batch, one-liner, long description, industry/tags, location,
status, team size, website) as one corpus document each — giving Roster a grounded startup population
with founders, which it currently lacks.

Flow (companies_house pattern): discover_entities({"query": text, "batch"/"tag"/"industry": filter,
"limit": N}) → GET the Algolia search endpoint, paginate → one EntityRef per company. fetch_artifact
best-effort enriches each company with FOUNDERS + year_founded from its detail page (Inertia JSON in
`ycombinator.com/companies/{slug}`) — the team/execution signal — then renders yc_doc.to_markdown.
Companies grade `source_kind=reference` → `verified_structured` tier (the Wikidata precedent).

Tests inject `companies` (each may embed a `founders` list) so they run offline with no network.
STRUCTURAL only (Rule 18): fields are read verbatim off the Algolia/detail record; nothing is judged.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import yc_doc
from ._http import HttpStrategy

# Public, search-only Algolia credentials embedded in ycombinator.com/companies (scoped to ycdc_public).
_APP_ID = "45BWZJ1SGC"
_API_KEY = ("NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJiMWM4ZTU0ZDlhYTZjOTJiMjlhMWFu"
            "YWx5dGljc1RhZ3M9eWNkYyZyZXN0cmljdEluZGljZXM9WUNDb21wYW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlf"
            "QnlfTGF1bmNoX0RhdGVfcHJvZHVjdGlvbiZ0YWdGaWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIyJTVE")
_INDEX = "YCCompany_production"
_SEARCH = f"https://{_APP_ID}-dsn.algolia.net/1/indexes/{_INDEX}"
_DETAIL = "https://www.ycombinator.com/companies/{slug}"
_MAX_HITS_PER_PAGE = 1000
_log = logging.getLogger(__name__)


class YcConnector:
    key = "yc"

    def __init__(self, *, companies: list[dict] | None = None, page_size: int = 100):
        self.fetch_strategy = HttpStrategy(base_delay=1.0, max_retries=4)
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for c in (companies or []):
            cid = yc_doc.company_id(c)
            if cid:
                self._by_id[cid] = c

    def _headers(self) -> dict[str, str]:
        return {"X-Algolia-Application-Id": _APP_ID, "X-Algolia-API-Key": _API_KEY,
                "Accept": "application/json"}

    @staticmethod
    def _facet_filters(window: dict) -> list[str]:
        """Optional exact facet filters read verbatim off the window (batch/tag/industry)."""
        ff: list[str] = []
        for wkey, facet in (("batch", "batch"), ("tag", "tags"), ("industry", "industry")):
            val = str((window or {}).get(wkey) or "").strip()
            if val:
                ff.append(f"{facet}:{val}")
        return ff

    async def _search(self, window: dict) -> list[dict]:
        query = str((window or {}).get("query") or "").strip()
        limit = max(1, int((window or {}).get("limit", self._page_size)))
        per_page = min(_MAX_HITS_PER_PAGE, max(1, min(limit, self._page_size if self._page_size > 0 else limit)))
        ff = self._facet_filters(window)
        out: list[dict] = []
        page = 0
        while len(out) < limit:
            params = {"query": query, "hitsPerPage": str(per_page), "page": str(page)}
            if ff:
                params["facetFilters"] = json.dumps(ff)
            url = f"{_SEARCH}?{urllib.parse.urlencode(params)}"
            data = json.loads(await self.fetch_strategy.fetch(url, headers=self._headers()))
            hits = [h for h in (data.get("hits") or []) if isinstance(h, dict)]
            out += hits
            n_pages = int(data.get("nbPages") or 1)
            if not hits or page + 1 >= n_pages:
                break
            page += 1
        return out[:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        w = window or {}
        has_selector = bool(str(w.get("query") or "").strip()) or bool(self._facet_filters(w))
        if not has_selector:
            # No query and no filter → serve injected fixtures (offline), or nothing. Never a
            # network browse of the whole index (mirrors companies_house's "no query → []").
            companies = list(self._by_id.values())
        else:
            companies = await self._search(w)
            for c in companies:
                cid = yc_doc.company_id(c)
                if cid:
                    self._by_id[cid] = c
        return [EntityRef(source_key=self.key, native_id=yc_doc.company_id(c),
                          title=yc_doc.title(c), facets=yc_doc.facets(c))
                for c in companies if yc_doc.company_id(c)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        c = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=yc_doc.facets(c) if c else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def _detail(self, slug: str) -> dict:
        """Founders + year_founded from the YC detail page (Inertia JSON embedded in `data-page`)."""
        raw = (await self.fetch_strategy.fetch(_DETAIL.format(slug=urllib.parse.quote(slug)))).decode(
            "utf-8", "ignore")
        m = re.search(r'data-page="(.*?)"\s*>', raw, re.S)
        if not m:
            return {}
        page = json.loads(_html.unescape(m.group(1)))
        comp = ((page.get("props") or {}).get("company")) or {}
        return comp if isinstance(comp, dict) else {}

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        base = self._by_id.get(doc.native_id) or {"slug": doc.native_id, "name": doc.title}
        rec = dict(base)
        # LIVE enrichment: founders + year_founded from the detail page (best-effort; detail fills
        # gaps only, never overwrites present search fields). Offline/fixture path keeps the record.
        if not rec.get("founders"):
            slug = str(rec.get("slug") or doc.native_id or "").strip()
            if slug:
                try:
                    detail = await self._detail(slug)
                    for k, v in detail.items():
                        if v not in (None, "", []) and not rec.get(k):
                            rec[k] = v
                except Exception as e:   # noqa: BLE001 — detail enrichment is best-effort
                    _log.info("yc %s: detail enrichment failed (%s)", doc.native_id, str(e)[:80])
        return yc_doc.to_markdown(rec).encode("utf-8")
