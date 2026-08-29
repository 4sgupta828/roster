"""ROSTER_WEB_OPEN_DENOISE — web.py structural + cosine-floor + authority-boost gates.

OFF (`web_denoise=False`) must be byte-identical to today; ON applies the three cheap,
computable pre-filters (length floor / cosine floor / authority boost) — never a semantic
quality guess (Rule 18: the LLM screen, not these, is the junk defense).
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import BlockHit, RetrievalRequest
from roster_kernel.providers.websearch import FakeWebSearch, WebResult
from roster_kernel.retrieval.web import _DENOISE_SIM_FLOOR, WebRetrievalSource

# Match PROD: WEB_DOMAIN_FACETS stamps `source_kind` (never `pub_type`). Gate 3's boost keys on
# facet-PRESENCE (`_facets_for` non-empty), so a source_kind stamp is what makes a domain authoritative.
_AUTH_FACETS = {"auth.example": {"source_kind": "news"}}


class _CtrlEmbedder:
    """Deterministic embedder returning a caller-chosen vector per text (controls cosine exactly)."""
    dim = 2

    def __init__(self, vecs: dict[str, list[float]]):
        self._vecs = vecs

    def embed(self, texts):
        return [self._vecs[t] for t in texts]


def _hit(text: str, url: str, score: float, facets=None) -> BlockHit:
    return BlockHit(document_id=url, block_id=text, text=text, score=score, facets=dict(facets or {}))


def _source(*, embedder=None) -> WebRetrievalSource:
    return WebRetrievalSource(FakeWebSearch({}), domain_facets=_AUTH_FACETS, embedder=embedder)


# cosine of [c, sqrt(1-c^2)] against q=[1,0] is exactly c (both unit vectors).
def _unit(c: float) -> list[float]:
    return [c, (1.0 - c * c) ** 0.5]


# ── _rerank: OFF byte-identical ordering ──────────────────────────────────────
def test_rerank_off_ordering_is_by_cosine_then_score():
    emb = _CtrlEmbedder({"q": [1.0, 0.0], "aaa": _unit(0.9), "bbb": _unit(0.5), "ccc": _unit(0.1)})
    src = _source(embedder=emb)
    hits = [
        _hit("aaa", "https://x.example/a", 100.0),
        _hit("bbb", "https://auth.example/b", 200.0),   # authoritative — must NOT matter when OFF
        _hit("ccc", "https://y.example/c", 300.0),
    ]
    req = RetrievalRequest(query="q", tenant_id="t")   # web_denoise defaults False
    out = src._rerank(req, hits)
    # pure (-sim, -score): 0.9 > 0.5 > 0.1 → aaa, bbb, ccc (authority + floor inert when OFF)
    assert [h.text for h in out] == ["aaa", "bbb", "ccc"]
    assert [h.score for h in out] == [1000.0, 999.0, 998.0]   # scores rewritten to final rank


# ── _rerank ON: cosine floor drops off-topic; authority boost bubbles up ───────
def test_rerank_on_floor_drops_offtopic_and_authority_leads():
    emb = _CtrlEmbedder({"q": [1.0, 0.0], "aaa": _unit(0.9), "bbb": _unit(0.5), "ccc": _unit(0.1)})
    src = _source(embedder=emb)
    hits = [
        _hit("aaa", "https://x.example/a", 100.0),      # unknown domain, HIGH cosine 0.9
        _hit("bbb", "https://auth.example/b", 200.0),   # authoritative, LOWER cosine 0.5
        _hit("ccc", "https://y.example/c", 300.0),      # unknown, off-topic cosine 0.1 < floor
    ]
    req = RetrievalRequest(query="q", tenant_id="t", web_denoise=True)
    out = src._rerank(req, hits)
    texts = [h.text for h in out]
    assert "ccc" not in texts                           # Gate 2: below _DENOISE_SIM_FLOOR → dropped
    assert texts == ["bbb", "aaa"]                       # Gate 3: authoritative bbb above higher-cosine aaa
    assert _DENOISE_SIM_FLOOR == 0.18


def test_rerank_on_floor_never_empties_leg():
    # every chunk below the floor → keep exactly the single top-cosine one
    emb = _CtrlEmbedder({"q": [1.0, 0.0], "aaa": _unit(0.15), "bbb": _unit(0.05)})
    src = _source(embedder=emb)
    hits = [_hit("aaa", "https://x.example/a", 100.0), _hit("bbb", "https://y.example/b", 200.0)]
    req = RetrievalRequest(query="q", tenant_id="t", web_denoise=True)
    out = src._rerank(req, hits)
    assert [h.text for h in out] == ["aaa"]              # top-cosine survivor, leg never empty


# ── search: Gate 1 structural length floor (highlights are never dropped) ──────
_LONG_HL = "This highlight extract is comfortably longer than the eighty-character structural floor."


def _search(canned_result: WebResult, *, denoise: bool):
    src = _source(embedder=None)                        # None → isolate Gate 1 (no cosine rerank)
    src._client = FakeWebSearch({"q": [canned_result]})
    req = RetrievalRequest(query="q", tenant_id="t", k=5, web_denoise=denoise)
    return asyncio.run(src.search(req))


def test_search_off_keeps_short_body_chunk():
    r = WebResult(url="https://x.example/p", title="P", snippet="", body="tiny", highlights=(_LONG_HL,))
    out = _search(r, denoise=False)
    assert {h.text for h in out} == {_LONG_HL, "tiny"}   # OFF: sub-floor body survives (byte-identical)


def test_search_on_drops_short_body_keeps_highlight():
    r = WebResult(url="https://x.example/p", title="P", snippet="", body="tiny", highlights=(_LONG_HL,))
    out = _search(r, denoise=True)
    texts = {h.text for h in out}
    assert _LONG_HL in texts                             # highlight never dropped
    assert "tiny" not in texts                           # Gate 1: sub-floor body chunk dropped


def test_search_on_never_empties_a_result():
    # a result whose ONLY chunk is sub-floor and has no highlight → kept (never empty a result)
    r = WebResult(url="https://x.example/p", title="P", snippet="", body="tiny", highlights=())
    out = _search(r, denoise=True)
    assert [h.text for h in out] == ["tiny"]
