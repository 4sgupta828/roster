"""UK Companies House connector — statutory company register (api.company-information.service.gov.uk).

KEYED source (patentsview pattern): needs a FREE API key. Set ROSTER_COMPANIES_HOUSE_KEY; without it
the connector is INERT — discover_entities logs a warning and returns [] rather than erroring an
ingest job. Auth is HTTP Basic with the key as the USERNAME and an EMPTY password, i.e.
`Authorization: Basic base64("{key}:")`.

Flow: discover_entities({"query": company name, "limit": N}) → GET /search/companies → one EntityRef
per company; fetch_artifact best-effort enriches each company with GET /company/{num} (profile) +
GET /company/{num}/officers (the team/execution signal) → companies_house_doc.to_markdown. Fills the
non-US-filings geography gap; companies grade `primary_filing`. Tests inject `companies` (each may
embed an `officers` list) so they run offline with no key.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import companies_house_doc as ch
from ._http import HttpStrategy

API = "https://api.company-information.service.gov.uk"
_MAX_ITEMS = 50
_log = logging.getLogger(__name__)


class CompaniesHouseConnector:
    key = "companies_house"

    def __init__(self, *, companies: list[dict] | None = None, page_size: int = 20):
        self.fetch_strategy = HttpStrategy(base_delay=1.0, max_retries=4)
        self._page_size = page_size
        self._key = os.environ.get("ROSTER_COMPANIES_HOUSE_KEY", "")
        self._by_id: dict[str, dict] = {}
        for c in (companies or []):
            cid = ch.company_number(c)
            if cid:
                self._by_id[cid] = c

    def _auth_header(self) -> dict[str, str]:
        # HTTP Basic: API key as username, empty password → base64("{key}:")
        token = base64.b64encode(f"{self._key}:".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    async def _search(self, query: str, limit: int) -> list[dict]:
        if not self._key:
            _log.warning(
                "companies_house: ROSTER_COMPANIES_HOUSE_KEY not set — skipping (no companies ingested)")
            return []
        n = min(_MAX_ITEMS, max(1, limit))
        q = urllib.parse.quote(query)
        url = f"{API}/search/companies?q={q}&items_per_page={n}"
        raw = await self.fetch_strategy.fetch(url, headers=self._auth_header())
        items = json.loads(raw).get("items") or []
        return [c for c in items if isinstance(c, dict) and c.get("company_number")][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            companies = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip()
            limit = min(_MAX_ITEMS, int((window or {}).get("limit", self._page_size)))
            companies = await self._search(q, limit) if q else []
            for c in companies:
                cid = ch.company_number(c)
                if cid:
                    self._by_id[cid] = c
        return [EntityRef(source_key=self.key, native_id=ch.company_number(c),
                          title=ch.title(c), facets=ch.facets(c))
                for c in companies if ch.company_number(c)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        c = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=ch.facets(c) if c else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def _profile(self, number: str) -> dict:
        raw = await self.fetch_strategy.fetch(
            f"{API}/company/{urllib.parse.quote(number)}", headers=self._auth_header())
        return json.loads(raw)

    async def _officers(self, number: str) -> list[dict]:
        raw = await self.fetch_strategy.fetch(
            f"{API}/company/{urllib.parse.quote(number)}/officers", headers=self._auth_header())
        items = json.loads(raw).get("items") or []
        return [o for o in items if isinstance(o, dict)]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        base = self._by_id.get(doc.native_id) or {"company_number": doc.native_id, "title": doc.title}
        rec = dict(base)
        # LIVE enrichment: profile + officers (best-effort). Offline/no-key path falls back to the
        # search record (which may already embed an injected `officers` list the doc renders).
        if self._key and ch.company_number(rec):
            num = ch.company_number(rec)
            try:
                rec.update(await self._profile(num))
            except Exception:   # noqa: BLE001 — profile is best-effort; keep the search record
                pass
            if not rec.get("officers"):
                try:
                    rec["officers"] = await self._officers(num)
                except Exception:   # noqa: BLE001 — officers list is best-effort
                    pass
        return ch.to_markdown(rec).encode("utf-8")
