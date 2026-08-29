"""Reddit connector — broader-community SENTIMENT signal (r/MachineLearning, r/LocalLLaMA, startups…).

Reddit's keyless `.json` endpoints now return the HTML app shell to datacenter IPs (anti-scraping), so
this uses the OFFICIAL OAuth API: app-only (`grant_type=client_credentials`) token from
`ROSTER_REDDIT_CLIENT_ID`/`ROSTER_REDDIT_CLIENT_SECRET` (register a free "script" app at
reddit.com/prefs/apps), then authenticated search on `oauth.reddit.com`. Without creds the connector is
INERT (returns fixtures only / empty) — a keyed source, like patents, not a hard dependency.

Everything grades `sentiment_signal` (lowest tier, never controlling) via `source_kind=social` + the
`reddit` source key. Tests inject `posts` so they run fully offline with no network and no creds.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.parse

from roster_kernel.contract.dto import DocumentRef, EntityRef

from .. import reddit_doc as rd
from ._http import USER_AGENT, HttpStrategy

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_API = "https://oauth.reddit.com"


class RedditConnector:
    key = "reddit"

    def __init__(self, *, posts: list[dict] | None = None, page_size: int = 25):
        self.fetch_strategy = HttpStrategy()
        self._page_size = page_size
        self._token: str | None = None
        self._by_id: dict[str, dict] = {}
        for p in (posts or []):
            if rd.post_id(p):
                self._by_id[rd.post_id(p)] = rd._data(p)

    # ---- OAuth (app-only, read-only) -------------------------------------------------
    @staticmethod
    def _creds() -> tuple[str, str]:
        return (os.environ.get("ROSTER_REDDIT_CLIENT_ID", "").strip(),
                os.environ.get("ROSTER_REDDIT_CLIENT_SECRET", "").strip())

    async def _bearer(self) -> str | None:
        """Fetch (and cache) an app-only bearer token. None when creds are absent → connector inert."""
        if self._token:
            return self._token
        cid, secret = self._creds()
        if not (cid and secret):
            return None
        import httpx
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                _TOKEN_URL, data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}", "User-Agent": USER_AGENT})
            r.raise_for_status()
            self._token = (r.json() or {}).get("access_token") or None
        return self._token

    async def _search(self, query: str, limit: int, subreddit: str = "") -> list[dict]:
        token = await self._bearer()
        if not token:
            return []
        n = min(100, max(1, limit))
        q = urllib.parse.quote(query)
        if subreddit:
            url = (f"{_API}/r/{urllib.parse.quote(subreddit)}/search?q={q}&restrict_sr=1"
                   f"&sort=top&t=year&limit={n}&raw_json=1&type=link")
        else:
            url = f"{_API}/search?q={q}&sort=relevance&limit={n}&raw_json=1&type=link"
        raw = await self.fetch_strategy.fetch(
            url, headers={"Authorization": f"bearer {token}", "User-Agent": USER_AGENT})
        data = json.loads(raw)
        children = ((data.get("data") or {}).get("children") or [])
        posts = [c.get("data") for c in children if isinstance(c, dict) and isinstance(c.get("data"), dict)]
        return [p for p in posts if rd.title({"data": p})][:limit]

    # ---- Connector protocol ----------------------------------------------------------
    async def discover_entities(self, window: dict) -> list[EntityRef]:
        window = window or {}
        if not window.get("query") and self._by_id:          # offline fixtures path
            posts = list(self._by_id.values())
        else:
            q = (window.get("query") or "").strip() or "artificial intelligence"
            limit = int(window.get("limit", self._page_size))
            sub = str((window.get("params") or {}).get("subreddit") or "").strip()
            posts = await self._search(q, limit, sub)
            for p in posts:
                if rd.post_id({"data": p}):
                    self._by_id[rd.post_id({"data": p})] = p
        return [EntityRef(source_key=self.key, native_id=rd.post_id({"data": p}),
                          title=rd.title({"data": p}), facets=rd.facets({"data": p}))
                for p in posts if rd.post_id({"data": p})]

    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]:
        p = self._by_id.get(entity.native_id)
        return [DocumentRef(source_key=self.key, native_id=entity.native_id, title=entity.title,
                            content_type="text/markdown",
                            facets=rd.facets({"data": p}) if p else dict(entity.facets),
                            entity_ids=(entity.native_id,))]

    async def fetch_artifact(self, doc: DocumentRef) -> bytes:
        p = self._by_id.get(doc.native_id) or {"id": doc.native_id, "title": doc.title}
        return rd.to_markdown({"data": p}).encode("utf-8")
