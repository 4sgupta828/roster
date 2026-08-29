"""DuckDuckGo web search — KEYLESS, free (no API key, no extra dependency).

A free fallback web leg: query the DDG HTML endpoint for result URLs, then fetch each result page
and strip it to text so the span-verification gate has REAL body to quote (DDG snippets alone are too
short to cite). Thinner than Exa's neural search + query-aware highlights, but free — used when no
EXA/TAVILY key is set, or forced via ROSTER_WEB_PROVIDER=ddg. Best-effort throughout: DDG's anti-bot
page, a blocked fetch, or a slow page each degrade to a snippet/empty result, never an error that
breaks the answer path. `open_web` is inherent (DDG is open web); domain-whitelisting isn't supported.
"""
from __future__ import annotations

import asyncio
import re
import urllib.parse

from roster_kernel.providers.websearch import WebResult

DDG_HTML = "https://html.duckduckgo.com/html/"
# DDG rejects non-browser User-Agents — present a normal desktop Chrome UA.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
_LINK = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_SNIP = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.S)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", s or "")).strip()


def _page_text(html: str, cap: int) -> str:
    return _clean(_SCRIPT.sub(" ", html or ""))[:cap]


def _real_url(href: str) -> str:
    """DDG wraps result links as //duckduckgo.com/l/?uddg=<url-encoded>&…; unwrap to the real URL."""
    if "uddg=" in href:
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            return urllib.parse.unquote(m.group(1))
    if href.startswith("//"):
        return "https:" + href
    return href


class DuckDuckGoWebSearch:
    def __init__(self, *, timeout: float = 8.0, fetch_bodies: bool = True, max_body_chars: int = 6000):
        self._timeout = timeout
        self._fetch = fetch_bodies
        self._cap = max_body_chars

    @staticmethod
    def _df(recency_days: int | None) -> str:
        if not recency_days:
            return ""
        d = int(recency_days)
        return "d" if d <= 1 else "w" if d <= 7 else "m" if d <= 31 else "y"

    async def search(self, query: str, *, max_results: int = 8,
                     open_web: bool = False, recency_days: int | None = None,
                     max_chars: int | None = None) -> list[WebResult]:
        import httpx
        data = {"q": query}
        cap = int(max_chars) if max_chars is not None else self._cap
        df = self._df(recency_days)
        if df:
            data["df"] = df
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers={"User-Agent": _UA},
                                         follow_redirects=True) as client:
                r = await client.post(DDG_HTML, data=data)
                r.raise_for_status()
                links = _LINK.findall(r.text)[:max_results]
                snips = [_clean(s) for s in _SNIP.findall(r.text)][:max_results]
                items = []
                for i, (href, title) in enumerate(links):
                    url = _real_url(href)
                    if not url.startswith("http"):
                        continue
                    sn = snips[i] if i < len(snips) else ""
                    items.append({"url": url, "title": _clean(title) or url, "snippet": sn, "body": sn})
                # enrich: fetch each result page for real body text (best-effort, concurrent)
                if self._fetch and items:
                    async def _enrich(it: dict) -> None:
                        try:
                            p = await client.get(it["url"])
                            txt = _page_text(p.text, cap)
                            if len(txt) > len(it["body"]):
                                it["body"] = txt
                        except Exception:   # noqa: BLE001 — a blocked/slow page degrades to snippet
                            pass
                    await asyncio.gather(*(_enrich(it) for it in items), return_exceptions=True)
                return [WebResult(url=it["url"], title=it["title"], snippet=it["snippet"],
                                  body=it["body"], published=None, provider="ddg") for it in items]
        except Exception:   # noqa: BLE001 — DDG anti-bot / network → empty leg, never break the answer
            return []
