"""Lobsters (lobste.rs) connector — keyless JSON tag feeds (technical-community discussion sentiment).

Lobsters has NO reliable keyless keyword-search JSON, so this is a TAG-FEED connector (like the
expert_feed model): it pulls recent stories from a curated set of deep-tech tags and optionally
keyword-filters by `window["query"]` against title/tags. Every story grades `sentiment_signal`
(lowest tier, never controlling) via `source_kind=social` + the `lobsters` source key.

discover_entities → GET /t/{tag}.json per curated tag (or injected fixtures); one EntityRef per story.
list_documents → one labeled sentiment-signal document per story. fetch_artifact → markdown bytes.
Tests inject `stories` so they run offline.
"""
from __future__ import annotations

import json

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import lobsters_doc as lob
from ._http import HttpStrategy

# Curated set of deep-tech Lobsters tags. Curatable — add/remove tags to tune the corpus.
TAGS = [
    "ai", "ml", "compsci", "programming", "hardware", "security", "cryptography",
    "distributed", "databases", "performance", "rust", "python", "systems",
    "networking", "science",
]

FEED = "https://lobste.rs/t/{tag}.json"


def _matches_query(rec: dict, words: list[str]) -> bool:
    """Loose case-insensitive match: any query word appears in the title or a tag (structural)."""
    if not words:
        return True
    hay = (lob.title(rec) + " " + " ".join(str(t) for t in (rec.get("tags") or []))).lower()
    return any(w in hay for w in words)


class LobstersConnector:
    key = "lobsters"

    def __init__(self, *, stories: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for s in (stories or []):
            if lob.story_id(s):
                self._by_id[lob.story_id(s)] = s

    async def _fetch_tag(self, tag: str) -> list[dict]:
        data = json.loads(await self.fetch_strategy.fetch(FEED.format(tag=tag)))
        if not isinstance(data, list):
            return []
        return [s for s in data if isinstance(s, dict) and lob.story_id(s) and lob.title(s)]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        w = window or {}
        # Offline / fixture path: injected stories and no live query → serve what we have.
        if not w.get("query") and self._by_id:
            stories = list(self._by_id.values())
        else:
            words = [x for x in str(w.get("query", "")).lower().split() if x]
            cap = int(w.get("limit", 50))
            seen: dict[str, dict] = {}
            for tag in TAGS:
                try:
                    for s in await self._fetch_tag(tag):
                        sid = lob.story_id(s)
                        if sid not in seen and _matches_query(s, words):
                            seen[sid] = s
                except Exception:   # one bad tag feed must not kill the whole batch
                    continue
            stories = list(seen.values())[:cap]
            for s in stories:
                self._by_id[lob.story_id(s)] = s
        return [EntityRef(source_key=self.key, native_id=lob.story_id(s),
                          title=lob.title(s), facets=lob.facets(s))
                for s in stories if lob.story_id(s)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        s = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=lob.facets(s) if s else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        s = self._by_id.get(doc.native_id) or {"short_id": doc.native_id, "title": doc.title}
        return lob.to_markdown(s).encode("utf-8")
