"""Wikidata connector — KEYLESS structured company profiles (Crunchbase/PitchBook fallback).

discover_entities → resolve a company NAME to a QID (wbsearchentities), then pull its profile
properties (founders, CEO, inception, industry, parent/subsidiary, ownership, ticker…) via one SPARQL
query; cache the rows. list_documents → one company-profile document. fetch_artifact → markdown built
from the cached rows. Community reference data → `source_kind=reference` (neutral tier). Tests inject
`companies` (each {qid,label,description,rows}) so they run offline.
"""
from __future__ import annotations

import json
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import wikidata_doc as wd
from ._http import HttpStrategy

WBSEARCH = "https://www.wikidata.org/w/api.php"
SPARQL = "https://query.wikidata.org/sparql"


class WikidataConnector:
    key = "wikidata"

    def __init__(self, *, companies: list[dict] | None = None, limit_entities: int = 1):
        self.fetch_strategy = HttpStrategy(max_retries=5, base_delay=1.5)
        self._limit_entities = limit_entities
        # cache: qid -> {"label","description","rows"}
        self._cache: dict[str, dict] = {}
        for c in (companies or []):
            qid = str(c.get("qid") or "").strip()
            if qid:
                self._cache[qid] = {"label": c.get("label", ""), "description": c.get("description", ""),
                                    "rows": [tuple(r) for r in (c.get("rows") or [])]}

    async def _resolve_qids(self, name: str, limit: int) -> list[tuple[str, str, str]]:
        params = {"action": "wbsearchentities", "search": name, "language": "en",
                  "type": "item", "limit": str(max(1, limit)), "format": "json"}
        url = f"{WBSEARCH}?{urllib.parse.urlencode(params)}"
        data = json.loads(await self.fetch_strategy.fetch(url))
        return [(str(s.get("id")), str(s.get("label") or name), str(s.get("description") or ""))
                for s in (data.get("search") or []) if s.get("id")]

    async def _profile_rows(self, qid: str) -> list[tuple[str, str]]:
        url = f"{SPARQL}?format=json&query={urllib.parse.quote(wd.sparql_for(qid))}"
        data = json.loads(await self.fetch_strategy.fetch(url, headers={"Accept": "application/sparql-results+json"}))
        return wd.rows_from_sparql(data)

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        q = (window or {}).get("query", "").strip()
        if not q and self._cache:
            qids = list(self._cache.keys())
        else:
            limit = int((window or {}).get("limit", self._limit_entities))
            resolved = await self._resolve_qids(q or "OpenAI", limit)
            qids = []
            for qid, label, desc in resolved:
                if qid not in self._cache:
                    self._cache[qid] = {"label": label, "description": desc,
                                        "rows": await self._profile_rows(qid)}
                qids.append(qid)
        out: list[EntityRef] = []
        for qid in qids:
            c = self._cache.get(qid) or {}
            rows = c.get("rows") or []
            if not rows:
                continue   # no structured facts → skip (fail-safe, no empty profile)
            out.append(EntityRef(source_key=self.key, native_id=qid,
                                 title=c.get("label") or qid, facets=wd.facets(qid, rows)))
        return out

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        c = self._cache.get(entity.native_id) or {}
        rows = c.get("rows") or []
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=wd.facets(entity.native_id, rows) if rows else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        c = self._cache.get(doc.native_id)
        if c is None:
            c = {"label": doc.title, "description": "", "rows": await self._profile_rows(doc.native_id)}
            self._cache[doc.native_id] = c
        return wd.to_markdown(c.get("label") or doc.title, doc.native_id,
                              c.get("rows") or [], c.get("description", "")).encode("utf-8")
