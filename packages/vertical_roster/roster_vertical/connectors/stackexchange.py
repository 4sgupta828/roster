"""Stack Overflow connector — Stack Exchange API 2.3 (KEYLESS, ~300 req/day; dev-adoption signal).

discover_entities → GET /search/advanced?q=…&site=stackoverflow (or injected fixtures); one EntityRef
per question. list_documents → one labeled developer-adoption document per question. fetch_artifact →
markdown bytes. Everything grades `sentiment_signal` (lowest tier, never controlling) via
source_kind=social + the `stackoverflow` source key. Tests inject `questions` so they run offline.

Optional ROSTER_STACKEXCHANGE_KEY raises the daily quota (keyless still works). The response is GZIP by
default — HttpStrategy sends Accept-Encoding: gzip and httpx auto-decompresses, so json.loads(fetch) works.
"""
from __future__ import annotations

import json
import os
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import stackoverflow_doc as so
from ._http import HttpStrategy

SEARCH = "https://api.stackexchange.com/2.3/search/advanced"

# The high-signal TECHNICAL Stack Exchange sites (beyond Stack Overflow) where practitioners reason
# through deep-tech questions. The tranche fans across these via params={"site": ...}; the connector
# default stays "stackoverflow" so existing behavior is byte-identical.
TECH_SITES = ("stackoverflow", "ai", "datascience", "stats", "cs", "security",
              "softwareengineering", "quantumcomputing", "electronics", "dsp", "robotics")


class StackExchangeConnector:
    key = "stackoverflow"

    def __init__(self, *, questions: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for q in (questions or []):
            if so.question_id(q):
                self._by_id[so.question_id(q)] = q

    async def _search(self, query: str, limit: int, site: str = "stackoverflow") -> list[dict]:
        n = min(50, max(1, limit))
        q = urllib.parse.quote(query)
        url = (f"{SEARCH}?order=desc&sort=votes&q={q}&site={urllib.parse.quote(site)}"
               f"&filter=withbody&pagesize={n}")
        key = os.environ.get("ROSTER_STACKEXCHANGE_KEY")
        if key:
            url += f"&key={urllib.parse.quote(key)}"
        try:
            data = json.loads(await self.fetch_strategy.fetch(url))
        except Exception:   # noqa: BLE001 — throttle/transient → skip this query, don't crash ingest
            return []
        items = [it for it in (data.get("items") or []) if isinstance(it, dict) and so.title(it)]
        return items[:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            questions = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "artificial intelligence"
            limit = int((window or {}).get("limit", self._page_size))
            site = str(((window or {}).get("params") or {}).get("site") or "stackoverflow").strip()
            questions = await self._search(q, limit, site=site or "stackoverflow")
            for it in questions:
                if so.question_id(it):
                    self._by_id[so.question_id(it)] = it
        return [EntityRef(source_key=self.key, native_id=so.question_id(it),
                          title=so.title(it), facets=so.facets(it))
                for it in questions if so.question_id(it)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        it = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=so.facets(it) if it else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        it = self._by_id.get(doc.native_id) or {"question_id": doc.native_id, "title": doc.title}
        return so.to_markdown(it).encode("utf-8")
