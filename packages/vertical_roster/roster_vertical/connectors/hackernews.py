"""Hacker News connector — Algolia HN Search API (KEYLESS, reliable; sentiment fallback for GDELT/HN).

discover_entities → GET /search?query=…&tags=story (or injected fixtures); one EntityRef per story.
list_documents → one labeled market-signal document per story. fetch_artifact → markdown bytes.
Everything grades `sentiment_signal` (lowest tier, never controlling) via source_kind=social + the
`hackernews` source key. Tests inject `stories` so they run offline.
"""
from __future__ import annotations

import json
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import hackernews_doc as hn
from ._http import HttpStrategy

SEARCH = "https://hn.algolia.com/api/v1/search"


class HackerNewsConnector:
    key = "hackernews"

    def __init__(self, *, stories: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for s in (stories or []):
            if hn.story_id(s):
                self._by_id[hn.story_id(s)] = s

    async def _search(self, query: str, limit: int) -> list[dict]:
        n = min(100, max(1, limit))
        q = urllib.parse.quote(query)
        url = f"{SEARCH}?query={q}&tags=story&hitsPerPage={n}"
        data = json.loads(await self.fetch_strategy.fetch(url))
        return [h for h in (data.get("hits") or []) if isinstance(h, dict) and hn.title(h)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            stories = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "artificial intelligence"
            limit = int((window or {}).get("limit", self._page_size))
            stories = await self._search(q, limit)
            for s in stories:
                if hn.story_id(s):
                    self._by_id[hn.story_id(s)] = s
        return [EntityRef(source_key=self.key, native_id=hn.story_id(s),
                          title=hn.title(s), facets=hn.facets(s))
                for s in stories if hn.story_id(s)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        s = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=hn.facets(s) if s else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        s = self._by_id.get(doc.native_id) or {"objectID": doc.native_id, "title": doc.title}
        return hn.to_markdown(s).encode("utf-8")
