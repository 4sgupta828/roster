"""Semantic Scholar connector — S2 Graph API (keyless; optional ROSTER_S2_API_KEY for higher rate).

discover_entities → GET /paper/search?query=… (or injected fixtures); one EntityRef per paper.
list_documents → one synthesized markdown paper-document per work. fetch_artifact → markdown bytes.
Strong CS/AI + citation coverage; complements OpenAlex. Peer-reviewed venues grade as
`verified_structured`, arXiv-only preprints as `technical_signal` (same tier logic as OpenAlex, via
`source_kind=paper` + `is_peer_reviewed`). Tests inject `papers` so they run offline.
"""
from __future__ import annotations

import json
import os
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import semantic_scholar_doc as s2doc
from ._http import HttpStrategy

SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
PAPER = "https://api.semanticscholar.org/graph/v1/paper"
_API_KEY = os.environ.get("ROSTER_S2_API_KEY", "")


class SemanticScholarConnector:
    key = "semantic_scholar"

    def __init__(self, *, papers: list[dict] | None = None, page_size: int = 25):
        # S2's KEYLESS shared pool 429s aggressively → be patient (long backoff). With a free
        # ROSTER_S2_API_KEY the limit is per-key and this rarely triggers.
        self.fetch_strategy = HttpStrategy(max_retries=6, base_delay=2.0)
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for p in (papers or []):
            if s2doc.s2_id(p):
                self._by_id[s2doc.s2_id(p)] = p

    def _headers(self) -> dict:
        return {"x-api-key": _API_KEY} if _API_KEY else {}

    async def _search(self, query: str, limit: int, *, from_year: str = "") -> list[dict]:
        n = min(100, max(1, limit))
        q = urllib.parse.quote(query)
        url = f"{SEARCH}?query={q}&limit={n}&fields={s2doc.FIELDS}"
        if from_year and str(from_year)[:4].isdigit():   # freshness lane: only works from this year on
            url += f"&year={str(from_year)[:4]}-"
        data = json.loads(await self.fetch_strategy.fetch(url, headers=self._headers()))
        return [r for r in (data.get("data") or []) if isinstance(r, dict)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            papers = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "large language models"
            limit = int((window or {}).get("limit", self._page_size))
            papers = await self._search(q, limit, from_year=str((window or {}).get("from_year", "")))
            for p in papers:
                if s2doc.s2_id(p):
                    self._by_id[s2doc.s2_id(p)] = p
        return [EntityRef(source_key=self.key, native_id=s2doc.s2_id(p),
                          title=s2doc.title(p), facets=s2doc.facets(p))
                for p in papers if s2doc.s2_id(p)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        p = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=s2doc.facets(p) if p else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        p = self._by_id.get(doc.native_id)
        if p is None:
            url = f"{PAPER}/{urllib.parse.quote(doc.native_id)}?fields={s2doc.FIELDS}"
            p = json.loads(await self.fetch_strategy.fetch(url, headers=self._headers()))
            self._by_id[doc.native_id] = p
        return s2doc.to_markdown(p).encode("utf-8")
