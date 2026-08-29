"""Tests for the Exa-backed web fetchers: highlights are USED, news is recency+category-biased,
results dedupe by url, and a provider without the recency/category kwargs degrades gracefully.
"""
import asyncio
from types import SimpleNamespace

from api.enrich_sources import news_fetcher, site_fetcher, _grounding_text


def _res(url, body, highlights=(), published=""):
    return SimpleNamespace(url=url, title=url, snippet=body[:80], body=body,
                           published=published, highlights=tuple(highlights))


class _FakeWeb:
    """Records the kwargs of each search call and returns canned results."""
    def __init__(self, results, accept_kwargs=True):
        self._results = results
        self._accept = accept_kwargs
        self.calls = []

    async def search(self, query, *, max_results=8, **kw):
        if not self._accept and kw:
            raise TypeError("unexpected kwargs")   # a provider without recency/category
        self.calls.append({"query": query, "max_results": max_results, **kw})
        return list(self._results)


def test_grounding_text_puts_highlights_first():
    r = _res("u", "BODY TEXT " * 10, highlights=["KEY SPAN one.", "KEY SPAN two."])
    t = _grounding_text(r)
    assert t.startswith("KEY SPAN one.\nKEY SPAN two.")   # highlights lead
    assert "BODY TEXT" in t                                # body still included


def test_grounding_text_caps_length():
    r = _res("u", "x" * 9000, highlights=["h" * 200])
    assert len(_grounding_text(r, cap=4500)) == 4500


def test_news_uses_highlights_and_recency_and_category():
    web = _FakeWeb([_res("http://n1", "body one", highlights=["Raised $20M Series A led by a16z."])])
    docs = asyncio.run(news_fetcher("Acme", web=web))
    assert docs and "Raised $20M Series A" in docs[0]["text"]         # highlight is in the doc
    assert docs[0]["facets"]["source_kind"] == "news"
    call = web.calls[0]
    assert call["recency_days"] == 540 and call["category"] == "news"  # recency + category passed
    assert call["max_results"] == 6                                    # widened count


def test_news_degrades_when_provider_lacks_kwargs():
    web = _FakeWeb([_res("http://n1", "body one", highlights=["h"])], accept_kwargs=False)
    docs = asyncio.run(news_fetcher("Acme", web=web))
    assert docs and docs[0]["text"]                     # still returns docs via the plain-call fallback
    assert web.calls[0].get("recency_days") is None     # fell back to the kwarg-free call


def test_news_dedupes_by_url():
    web = _FakeWeb([_res("http://n1", "a", highlights=["x"]),
                    _res("http://n1", "b", highlights=["y"]),
                    _res("http://n2", "c", highlights=["z"])])
    docs = asyncio.run(news_fetcher("Acme", web=web))
    assert len(docs) == 2


def test_site_fetcher_uses_highlights():
    web = _FakeWeb([_res("http://acme.com/product", "product body",
                         highlights=["Acme builds a real-time speech API."])])
    docs = asyncio.run(site_fetcher("Acme", web=web, domain="acme.com"))
    assert docs and any("real-time speech API" in d["text"] for d in docs)
    assert all(d["facets"]["source_kind"] == "corp_eng" for d in docs)
