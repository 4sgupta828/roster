"""Shared fetch strategy for tech connectors: plain datacenter HTTP, no proxy, with backoff.

All Tier-1 tech sources (SEC EDGAR, arXiv, OpenAlex, GitHub, …) are public APIs over ordinary HTTPS
with a fair-use User-Agent requirement. This adds retry-with-backoff on rate-limit/transient errors so
a 429/503 becomes a paced retry, not a dead ingest job. Set a REAL contact via ROSTER_HTTP_CONTACT
(SEC's fair-access policy expects one) — default is a neutral placeholder, overridable in prod.
"""
from __future__ import annotations

import asyncio
import os

_CONTACT = os.environ.get("ROSTER_HTTP_CONTACT", "research@roster.dev")
USER_AGENT = f"roster-research/0.1 (+{_CONTACT})"

# Transient HTTP statuses worth retrying with backoff (rate-limit + upstream hiccups).
_RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpStrategy:
    egress_class = "datacenter"
    engine = "http"
    proxy_enabled = False

    def __init__(self, *, max_retries: int = 4, base_delay: float = 1.0):
        self._max_retries = max_retries
        self._base_delay = base_delay

    async def fetch(self, url: str, **opts) -> bytes:  # noqa: ANN003
        import httpx
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", **(opts.get("headers") or {})}
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=30.0, headers=headers, follow_redirects=True) as client:
            for attempt in range(self._max_retries + 1):
                try:
                    r = await client.get(url)
                    if r.status_code in _RETRY_STATUS and attempt < self._max_retries:
                        # honor Retry-After when present, else exponential backoff
                        ra = r.headers.get("Retry-After")
                        delay = float(ra) if (ra and ra.isdigit()) else self._base_delay * (2 ** attempt)
                        await asyncio.sleep(min(delay, 30.0))
                        continue
                    r.raise_for_status()
                    return r.content
                except httpx.HTTPStatusError as e:
                    last_exc = e
                    if e.response is not None and e.response.status_code in _RETRY_STATUS \
                            and attempt < self._max_retries:
                        await asyncio.sleep(min(self._base_delay * (2 ** attempt), 30.0))
                        continue
                    raise
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    last_exc = e
                    if attempt < self._max_retries:
                        await asyncio.sleep(min(self._base_delay * (2 ** attempt), 30.0))
                        continue
                    raise
        if last_exc:
            raise last_exc
        raise RuntimeError(f"fetch failed with no response: {url}")
