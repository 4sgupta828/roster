"""GitHub connector — open-source repository traction (public API).

discover_entities({"query": search terms | "org:NAME", "limit": N}) → GitHub repo search sorted by
stars → one EntityRef per repo. fetch_artifact → repo metadata + README markdown. Unauthenticated
GitHub search is heavily rate-limited (~10 req/min); set ROSTER_GITHUB_TOKEN for 5000 req/hr. Tests
inject `repos` so they run offline.
"""
from __future__ import annotations

import json
import os

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import repo_doc
from ._http import HttpStrategy

SEARCH = "https://api.github.com/search/repositories"
README = "https://raw.githubusercontent.com/{full_name}/HEAD/README.md"
_TOKEN = os.environ.get("ROSTER_GITHUB_TOKEN", "")


class GithubConnector:
    key = "github"

    def __init__(self, *, repos: list[dict] | None = None, page_size: int = 15):
        self.fetch_strategy = HttpStrategy(base_delay=2.0, max_retries=4)
        self._page_size = page_size
        self._by_name: dict[str, dict] = {}
        for r in (repos or []):
            self._by_name[repo_doc.repo_id(r)] = r

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json"}
        if _TOKEN:
            h["Authorization"] = f"Bearer {_TOKEN}"
        return h

    async def discover_entities(self, window: dict) -> list[EntityRef]:
        if not (window or {}).get("query") and self._by_name:
            repos = list(self._by_name.values())
        else:
            q = (window or {}).get("query", "").strip() or "large language model"
            limit = min(50, int((window or {}).get("limit", self._page_size)))
            url = f"{SEARCH}?q={q.replace(' ', '+')}&sort=stars&order=desc&per_page={limit}"
            data = json.loads(await self.fetch_strategy.fetch(url, headers=self._headers()))
            repos = (data.get("items") or [])[:limit]
            for r in repos:
                self._by_name[repo_doc.repo_id(r)] = r
        return [EntityRef(source_key=self.key, native_id=repo_doc.repo_id(r),
                          title=repo_doc.title(r), facets=repo_doc.facets(r))
                for r in repos if repo_doc.repo_id(r)]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        r = self._by_name.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=repo_doc.facets(r) if r else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        r = dict(self._by_name.get(doc.native_id) or {"full_name": doc.native_id})
        if not r.get("readme"):
            try:
                r["readme"] = (await self.fetch_strategy.fetch(
                    README.format(full_name=doc.native_id))).decode("utf-8", "ignore")
            except Exception:   # noqa: BLE001 — no README / private → metadata-only doc
                pass
        return repo_doc.to_markdown(r).encode("utf-8")
