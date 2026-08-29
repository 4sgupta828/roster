"""Hugging Face Hub connector — model-adoption signal (public API, token optional).

discover_entities({"query": search terms, "limit": N}) → Hub model search sorted by downloads → one
EntityRef per model. fetch_artifact → model metadata + card README (best-effort). Unauthenticated Hub
search works but is rate-limited; set ROSTER_HF_TOKEN for higher limits. Tests inject `models` so they
run offline. Everything grades `technical_signal` (a SELF-REPORTED adoption signal, never audited
fact) via source_kind=model.
"""
from __future__ import annotations

import json
import os
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import hf_doc
from ._http import HttpStrategy

SEARCH = "https://huggingface.co/api/models"
README = "https://huggingface.co/{id}/raw/main/README.md"
_TOKEN = os.environ.get("ROSTER_HF_TOKEN", "")


class HuggingFaceConnector:
    key = "huggingface"

    def __init__(self, *, models: list[dict] | None = None, page_size: int = 20):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._by_id: dict[str, dict] = {}
        for m in (models or []):
            if hf_doc.model_id(m):
                self._by_id[hf_doc.model_id(m)] = m

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if _TOKEN:
            h["Authorization"] = f"Bearer {_TOKEN}"
        return h

    async def _search(self, query: str, limit: int) -> list[dict]:
        n = min(50, max(1, limit))
        q = urllib.parse.quote(query)
        url = f"{SEARCH}?search={q}&sort=downloads&direction=-1&limit={n}"
        data = json.loads(await self.fetch_strategy.fetch(url, headers=self._headers()))
        models = data if isinstance(data, list) else []
        return [m for m in models if isinstance(m, dict) and hf_doc.model_id(m)][:limit]

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_id:
            models = list(self._by_id.values())
        else:
            q = (window or {}).get("query", "").strip() or "large language model"
            limit = int((window or {}).get("limit", self._page_size))
            models = await self._search(q, limit)
            for m in models:
                if hf_doc.model_id(m):
                    self._by_id[hf_doc.model_id(m)] = m
        return [EntityRef(source_key=self.key, native_id=hf_doc.model_id(m),
                          title=hf_doc.title(m), facets=hf_doc.facets(m))
                for m in models if hf_doc.model_id(m)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        m = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=hf_doc.facets(m) if m else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        m = dict(self._by_id.get(doc.native_id) or {"id": doc.native_id})
        if not m.get("readme"):
            try:
                m["readme"] = (await self.fetch_strategy.fetch(
                    README.format(id=doc.native_id), headers=self._headers())).decode("utf-8", "ignore")
            except Exception:   # noqa: BLE001 — no card / gated → metadata-only doc
                pass
        return hf_doc.to_markdown(m).encode("utf-8")
