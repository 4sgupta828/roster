"""Tests for company hyperlinks (api.company_links.detect_and_resolve_companies).

The LLM detects company mentions + their official homepage. NAME links by precedence:
homepage → grounded canonical page (known) → web search (last resort). Offline: fake llm + store.

    PYTHONPATH=apps:packages/vertical_roster:packages/kernel .venv/bin/python -m pytest \
        apps/api/test_company_links.py -q
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api.company_links import detect_and_resolve_companies


def _co(name, website=""):
    return SimpleNamespace(name=name, website=website)


class _LLM:
    def __init__(self, companies):
        self._c = companies

    async def complete(self, **kw):
        return SimpleNamespace(parsed=SimpleNamespace(companies=list(self._c)))


class _Store:
    def __init__(self, reg):
        self._reg = reg

    async def company_norm_map(self, *, tenant_id="demo"):
        return dict(self._reg)


def _svc(companies):
    return SimpleNamespace(llm=_LLM(companies),
                           ui=SimpleNamespace(source_url=lambda did, q=None: "https://page/" + did))


def _run(c):
    return asyncio.run(c)


ANSWER = "OpenAI is under pressure, Abalone Bio is doing well, and Foobar Inc shut down."


def test_homepage_wins_for_known_and_unknown() -> None:
    svc = _svc([_co("OpenAI", "https://openai.com"),
                _co("Abalone Bio", "https://abalonebio.com"),
                _co("Foobar Inc", "")])          # no homepage, not in registry → search
    store = _Store({"abalone bio": {"entity_id": "wikidata:Q42", "name": "Abalone Bio"}})
    by = {c["name"]: c for c in _run(detect_and_resolve_companies(svc, store, answer=ANSWER, ui=svc.ui))}
    # unknown WITH a homepage → its own site (not a search link)
    assert by["OpenAI"]["url"] == "https://openai.com" and by["OpenAI"]["search"] is False
    assert "roster_url" not in by["OpenAI"]
    # known WITH a homepage → still prefers the company's own site, but keeps the ◉ entity page
    assert by["Abalone Bio"]["url"] == "https://abalonebio.com" and by["Abalone Bio"]["search"] is False
    assert by["Abalone Bio"]["roster_url"] == "/entity/wikidata%3AQ42"
    assert by["Abalone Bio"]["grounded"] is True
    # unknown WITHOUT a homepage → last-resort web search (flagged)
    assert by["Foobar Inc"]["search"] is True
    assert by["Foobar Inc"]["url"].startswith("https://www.google.com/search?q=")


def test_known_without_homepage_uses_grounded_page() -> None:
    svc = _svc([_co("Abalone Bio", "")])          # no homepage from the LLM
    store = _Store({"abalone bio": {"entity_id": "wikidata:Q42", "name": "Abalone Bio"}})
    out = _run(detect_and_resolve_companies(svc, store, answer="Abalone Bio is notable.", ui=svc.ui))
    assert out[0]["url"] == "https://page/wikidata:Q42" and out[0]["search"] is False


def test_bogus_website_is_rejected_falls_back() -> None:
    # a search/wikipedia URL is NOT a homepage → rejected; unknown company → web search
    svc = _svc([_co("OpenAI", "https://en.wikipedia.org/wiki/OpenAI")])
    out = _run(detect_and_resolve_companies(svc, _Store({}), answer="OpenAI here.", ui=svc.ui))
    assert out[0]["search"] is True and out[0]["url"].startswith("https://www.google.com/search")


def test_bare_domain_website_is_normalized() -> None:
    svc = _svc([_co("Stripe", "stripe.com")])     # no scheme → https:// added
    out = _run(detect_and_resolve_companies(svc, _Store({}), answer="Stripe is big.", ui=svc.ui))
    assert out[0]["url"] == "https://stripe.com" and out[0]["search"] is False


def test_only_verbatim_names_kept_and_deduped() -> None:
    svc = _svc([_co("OpenAI", "https://openai.com"), _co("Nonexistent Corp", "https://x.example"),
                _co("OpenAI", "https://openai.com")])
    out = _run(detect_and_resolve_companies(svc, _Store({}), answer="OpenAI is under pressure.", ui=svc.ui))
    assert [c["name"] for c in out] == ["OpenAI"]


def test_no_llm_or_empty_and_never_raises() -> None:
    assert _run(detect_and_resolve_companies(SimpleNamespace(llm=None), None, answer="x")) == []
    assert _run(detect_and_resolve_companies(_svc([]), None, answer="")) == []

    class _Boom:
        async def complete(self, **kw):
            raise RuntimeError("llm down")
    assert _run(detect_and_resolve_companies(SimpleNamespace(llm=_Boom(), ui=None),
                                             None, answer="OpenAI here")) == []
