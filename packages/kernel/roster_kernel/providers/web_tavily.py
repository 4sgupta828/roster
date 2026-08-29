"""TavilyWebSearch — a real WebSearchClient over the Tavily API.

Lazy httpx call; optional dep. Returns WebResult with a body (raw content) so the
provenance gate can verify a cited quote exists in the fetched page. Wrap in a
cassette for free replay. A SerpApi/Exa client would implement the same port.
"""
from __future__ import annotations

import os

from .websearch import WebResult

TAVILY_URL = "https://api.tavily.com/search"


class TavilyWebSearch:
    def __init__(self, *, api_key: str | None = None, timeout: float = 18.0, time_range: str = ""):
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._timeout = timeout
        # freshness: bias to a recency window ("day"|"week"|"month"|"year"); "" = no filter (open
        # recency, byte-identical). Fixes "latest state" answers returning old, highly-relevant pages.
        self._time_range = time_range

    async def search(self, query: str, *, max_results: int = 8,
                     open_web: bool = False, recency_days: int | None = None,
                     max_chars: int | None = None) -> list[WebResult]:
        import httpx
        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "include_raw_content": True,
        }
        # per-request recency maps to Tavily's time_range buckets (it has no day-granular floor);
        # constructor default otherwise. open_web is a no-op here (Tavily is already open web).
        _tr = self._time_range
        if recency_days:
            _tr = "week" if recency_days <= 7 else "month" if recency_days <= 31 else "year"
        if _tr:
            payload["time_range"] = _tr
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(TAVILY_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
        out: list[WebResult] = []
        for r in data.get("results", []):
            body = r.get("raw_content") or r.get("content", "")
            if max_chars is not None:
                body = body[:int(max_chars)]
            out.append(WebResult(
                url=r.get("url", ""),
                title=r.get("title", ""),
                snippet=r.get("content", ""),
                body=body,
                published=r.get("published_date") or None,
                provider="tavily",
            ))
        # freshness: recency window active → re-sort newest-first so the latest pages lead the pool
        if _tr:
            out.sort(key=lambda r: r.published or "", reverse=True)
        return out
