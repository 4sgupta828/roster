"""Open-web URL-liveness gate — drop citations whose page does not exist.

Opening the web (ROSTER_WEB_OPEN_DENOISE / ROSTER_WEB_ENTITY_OPEN) pulls in niche pages. The
span-gate proves a cited quote existed in the body Exa FETCHED at search time — it does NOT
prove the live URL still resolves in a user's browser. Some open-web pages 404 later, moved, or
Exa served a cached/rendered copy the live URL no longer returns. Citing a dead link destroys
trust ("evidence links point to pages that don't exist").

This gate probes each open-web hit's URL and drops ONLY the ones that DEFINITIVELY do not exist
(HTTP 404 / 410). It is deliberately conservative + FAIL-OPEN: a bot-wall (403/429), a 5xx, a
timeout, or any probe error KEEPS the hit — we never drop a real page because a datacenter-IP
HEAD got walled or the checker hiccuped. Only an explicit "not found" removes a citation.

Structural (Rule 18): this is a computable liveness check on a URL, not a semantic judgment.
Runs only on the open-web legs (whitelisted/corpus hits are stable and skip it).
"""
from __future__ import annotations

import asyncio
import logging

_log = logging.getLogger(__name__)

_LIVENESS_TIMEOUT = 6.0
# A definitively-dead page. 404 Not Found / 410 Gone only — NOT 403/429 (bot-wall), NOT 5xx
# (transient), NOT connection errors (could be a network blip or datacenter-IP block).
_DEAD_STATUS = frozenset({404, 410})
# A realistic browser UA — many sites 404/403 a bot UA even for a live page.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


async def _default_prober(url: str, timeout: float) -> bool:
    """Return True IFF the URL is definitively dead (404/410). Fail-open (False) on ANY other
    status or error — never assert dead on ambiguity."""
    import httpx
    headers = {"user-agent": _UA}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            try:
                resp = await client.head(url, headers=headers)
            except httpx.HTTPError:
                resp = None
            # Some servers reject/!support HEAD (405/501) — confirm with a GET before judging dead.
            if resp is None or resp.status_code in (405, 501):
                resp = await client.get(url, headers=headers)
            return resp.status_code in _DEAD_STATUS
    except Exception as e:  # noqa: BLE001 — fail-open: a probe error must never drop a real page
        _log.debug("liveness probe kept (inconclusive) for %s: %r", url, e)
        return False


async def drop_dead_urls(hits, *, timeout: float = _LIVENESS_TIMEOUT, prober=None) -> list:
    """Drop `hits` whose URL (document_id) is definitively dead (404/410). Probes each UNIQUE URL
    once, concurrently. Empty input, no URLs, or all-live → returns the input unchanged. `prober`
    is injectable for tests (async (url, timeout) -> bool where True = dead)."""
    if not hits:
        return hits
    probe = prober or _default_prober
    urls: list[str] = []
    seen: set[str] = set()
    for h in hits:
        u = (getattr(h, "document_id", "") or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    if not urls:
        return hits
    verdicts = await asyncio.gather(*[probe(u, timeout) for u in urls], return_exceptions=True)
    dead = {u for u, v in zip(urls, verdicts) if v is True}   # exceptions (not True) → keep
    if not dead:
        return hits
    _log.info("liveness gate dropped %d dead-URL hit-source(s): %s", len(dead), sorted(dead))
    return [h for h in hits if (getattr(h, "document_id", "") or "").strip() not in dead]
