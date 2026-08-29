"""BraveWebSearch — a real WebSearchClient over the Brave Search API (api.search.brave.com).

Replaces the DuckDuckGo HTML-scrape leg, which datacenter IPs are now blocked from (returns 0). Brave is
a genuine REST API with a generous free tier (a key from https://brave.com/search/api/), unblocked from
servers. Snippet-tier like DDG/Tavily (Brave returns descriptions, not full page text), so it adds
open-web BREADTH; Exa still supplies depth (full text + query-aware highlights). Same port as the other
providers: returns WebResult so the provenance gate + tier classifier grade whatever is actually cited.
Keyless → no-op (empty results), so it's harmless in the composite when BRAVE_API_KEY is unset.
"""
from __future__ import annotations

import os

from .websearch import WebResult

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


def _freshness(recency_days: int | None) -> str:
    """Map a recency floor to Brave's freshness code (pd=day, pw=week, pm=month, py=year)."""
    if not recency_days:
        return ""
    d = int(recency_days)
    return "pd" if d <= 1 else "pw" if d <= 7 else "pm" if d <= 31 else "py"


class BraveWebSearch:
    def __init__(self, *, api_key: str | None = None, timeout: float = 10.0):
        self._api_key = api_key or os.environ.get("BRAVE_API_KEY", "")
        self._timeout = timeout

    async def search(self, query: str, *, max_results: int = 8,
                     open_web: bool = False, recency_days: int | None = None,
                     max_chars: int | None = None) -> list[WebResult]:
        if not self._api_key:
            return []                       # keyless → contribute nothing (fail-safe in the composite)
        import httpx
        params = {"q": query, "count": max(1, min(int(max_results), 20)), "result_filter": "web"}
        fresh = _freshness(recency_days)
        if fresh:
            params["freshness"] = fresh
        headers = {"X-Subscription-Token": self._api_key, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(BRAVE_URL, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception:                   # noqa: BLE001 — best-effort leg; a failure contributes nothing
            return []
        cap = int(max_chars) if max_chars is not None else 4000
        out: list[WebResult] = []
        for r in ((data.get("web") or {}).get("results") or []):
            desc = (r.get("description") or "").strip()
            out.append(WebResult(
                url=r.get("url", ""),
                title=r.get("title") or r.get("url", ""),
                snippet=desc[:400],
                body=desc[:cap] or None,
                published=(r.get("age") or r.get("page_age") or None),
                provider="brave",
            ))
        return out
