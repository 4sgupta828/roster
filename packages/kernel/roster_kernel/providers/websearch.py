"""Web-search provider port + deterministic fake.

Mechanism only — which sites/providers to curate is a vertical concern and is
supplied through the vertical contract, never hardcoded here.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .base import ProviderMode, guard_live, resolve_mode
from .cassette import Cassette, hash_request


@dataclass
class WebResult:
    url: str
    title: str
    snippet: str
    body: str | None = None
    published: str | None = None       # ISO-ish publish date when the provider reports one
    highlights: tuple[str, ...] = ()   # query-aware extracts (Exa) — spans from ANYWHERE in the page
    provider: str = ""                 # search-source attribution: which engine surfaced this (exa/brave/…)
    providers: tuple[str, ...] = ()    # ALL engines that returned this URL (set by the composite on merge;
    #                                    a single-element tuple ⇒ only one engine found it → NOVEL to it)


@runtime_checkable
class WebSearchClient(Protocol):
    async def search(self, query: str, *, max_results: int = 10,
                     open_web: bool = False, recency_days: int | None = None,
                     max_chars: int | None = None) -> list[WebResult]: ...


def _norm_url(u: str) -> str:
    """Normalize a URL for cross-provider dedup: drop scheme, trailing slash, '#…', lowercase host."""
    u = (u or "").strip()
    u = re.sub(r"^https?://", "", u, flags=re.I).split("#", 1)[0].rstrip("/")
    return u.lower()


class CompositeWebSearch:
    """ADDITIVE web leg: fan out to several providers CONCURRENTLY and merge, deduping by URL. Each
    provider is best-effort (a failure/empty contributes nothing). Broadens coverage — e.g. Exa's
    whitelisted, credible results PLUS DuckDuckGo's open-web breadth — while the downstream tier
    classifier + span gate still grade + verify whatever is actually cited. Provider-major interleave
    so EVERY provider gets representation (breadth), not just the first one's list."""

    def __init__(self, clients: list, *, hydrator=None, thin_chars: int = 800,
                 prominence_sort: bool = True):
        self._clients = [c for c in clients if c is not None]
        # HYDRATOR (Brave-discovers → Exa-hydrates): async (urls, query) -> {url: (body, highlights)}.
        # Turns a SNIPPET-only provider's discoveries into groundable full text. Applied ONLY to thin,
        # already-deduped results, so it never adds a URL (no duplication) — it fills content in place.
        self._hydrator = hydrator
        self._thin_chars = int(thin_chars)
        # PROMINENCE: a URL returned by MULTIPLE independent engines (Exa AND Brave) is more authoritative/
        # widely-reported than a single-engine find — cross-engine agreement is a prominence prior the
        # composite already computes (hit.providers) but never used. When on, stable-sort the merged list
        # by agreement-count desc (interleave order preserved as tiebreak) so the most-agreed URLs survive
        # downstream top-k truncation. Grounding/currency untouched (ordering only over the same evidence).
        self._prominence_sort = bool(prominence_sort)

    async def search(self, query: str, *, max_results: int = 10,
                     open_web: bool = False, recency_days: int | None = None,
                     max_chars: int | None = None) -> list[WebResult]:
        import asyncio
        lists = await asyncio.gather(
            *(c.search(query, max_results=max_results, open_web=open_web,
                       recency_days=recency_days, max_chars=max_chars)
              for c in self._clients),
            return_exceptions=True)
        lists = [r for r in lists if isinstance(r, list)]
        # url -> every provider that returned it (search-source attribution + novelty: a single-provider
        # url is NOVEL to that engine). Built across ALL providers' lists before the dedup merge.
        prov_by_url: dict[str, set] = {}
        for lst in lists:
            for hit in lst:
                k = _norm_url(hit.url)
                if k and getattr(hit, "provider", ""):
                    prov_by_url.setdefault(k, set()).add(hit.provider)
        out: list[WebResult] = []
        seen: set[str] = set()
        for rank in range(max((len(r) for r in lists), default=0)):
            for r in lists:
                if rank < len(r):
                    hit = r[rank]
                    key = _norm_url(hit.url)
                    if key and key not in seen:
                        seen.add(key)
                        provs = prov_by_url.get(key) or ({hit.provider} if hit.provider else set())
                        hit.providers = tuple(sorted(provs))
                        out.append(hit)
        # HYDRATE thin discoveries to full text. `out` is ALREADY deduped by URL, so each thin hit is a
        # UNIQUE url a full-text provider didn't return (a snippet-provider's novel find) — filling its
        # body in place adds NO url (no duplication). Provider stays as-is so attribution still credits the
        # engine that DISCOVERED the url. Fail-safe: hydrator error → snippets kept unchanged.
        if self._hydrator:
            thin = [h for h in out if len((h.body or "")) < self._thin_chars and h.url]
            if thin:
                try:
                    hy = await self._hydrator([h.url for h in thin], query)
                except Exception:   # noqa: BLE001
                    hy = {}
                if hy:
                    by_norm = {_norm_url(u): v for u, v in hy.items()}
                    for h in thin:
                        got = hy.get(h.url) or by_norm.get(_norm_url(h.url))
                        if got:
                            h.body, h.highlights = got[0], tuple(got[1] or ())
        # PROMINENCE: bubble cross-engine-agreed URLs to the top (stable → interleave order kept within a
        # tier). Only bites when ≥2 engines are live (Exa+Brave); single-engine → all len 1 → no-op.
        if self._prominence_sort:
            out.sort(key=lambda h: len(getattr(h, "providers", ()) or ()), reverse=True)
        return out


class FakeWebSearch:
    """Offline web search returning canned results per query (tests)."""

    def __init__(self, canned: dict[str, list[WebResult]] | None = None):
        self._canned = canned or {}

    async def search(self, query: str, *, max_results: int = 10,
                     open_web: bool = False, recency_days: int | None = None,
                     max_chars: int | None = None) -> list[WebResult]:
        return self._canned.get(query, [])[:max_results]


class CassetteWebSearch:
    """Wrap an inner WebSearchClient with replay/record/live — free eval/CI."""

    def __init__(self, inner: WebSearchClient | None, *, cassette_root: Path,
                 namespace: str = "web", mode: ProviderMode | str | None = None):
        self._inner = inner
        self._mode = resolve_mode(mode)
        self._cassette = Cassette(root=cassette_root, namespace=namespace)

    async def search(self, query: str, *, max_results: int = 10,
                     open_web: bool = False, recency_days: int | None = None,
                     max_chars: int | None = None) -> list[WebResult]:
        # Default cassette KEY stays (query, max_results) so existing replay fixtures are untouched;
        # a raised text cap gets its own key because it changes the recorded page body.
        key = (hash_request("web", query, max_results) if max_chars is None
               else hash_request("web", query, max_results, max_chars))
        if self._mode is ProviderMode.REPLAY:
            return [WebResult(**r) for r in self._cassette.replay(key, hint=query)]
        guard_live(self._mode)
        if self._inner is None:
            raise RuntimeError("CassetteWebSearch in record/live mode requires an inner client")
        results = await self._inner.search(query, max_results=max_results,
                                           open_web=open_web, recency_days=recency_days,
                                           max_chars=max_chars)
        if self._mode is ProviderMode.RECORD:
            self._cassette.record(key, [asdict(r) for r in results])
        return results
