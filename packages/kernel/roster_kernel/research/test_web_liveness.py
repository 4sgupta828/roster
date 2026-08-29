"""URL-liveness gate: drop only definitively-dead (404/410) open-web citations; fail-open on
everything else (bot-walls, timeouts, 5xx, probe errors). Structural, conservative.

Uses pytest-asyncio's managed loop (async def + @pytest.mark.asyncio) — matches the sibling
async tests (test_web_quality) and never hand-rolls an event loop (which perturbs suite isolation)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from roster_kernel.research.web_liveness import drop_dead_urls


@dataclass
class _Hit:
    document_id: str = ""      # the URL for web hits
    text: str = "body"
    facets: dict = field(default_factory=dict)


def _prober(dead_urls):
    async def probe(url, timeout):
        return url in dead_urls
    return probe


@pytest.mark.asyncio
async def test_drops_only_dead_urls():
    hits = [_Hit("https://live.example/a"), _Hit("https://dead.example/x"), _Hit("https://live.example/b")]
    out = await drop_dead_urls(hits, prober=_prober({"https://dead.example/x"}))
    assert [h.document_id for h in out] == ["https://live.example/a", "https://live.example/b"]


@pytest.mark.asyncio
async def test_keeps_all_when_none_dead():
    hits = [_Hit("https://a.example"), _Hit("https://b.example")]
    out = await drop_dead_urls(hits, prober=_prober(set()))
    assert out is hits  # unchanged object when nothing is dead


@pytest.mark.asyncio
async def test_empty_input():
    assert await drop_dead_urls([], prober=_prober({"x"})) == []


@pytest.mark.asyncio
async def test_probe_exception_fails_open_keeps_hit():
    async def boom(url, timeout):
        raise RuntimeError("network blip")
    hits = [_Hit("https://a.example")]
    out = await drop_dead_urls(hits, prober=boom)
    assert [h.document_id for h in out] == ["https://a.example"]  # kept on probe error


@pytest.mark.asyncio
async def test_dedup_probes_shared_url_once():
    calls = []
    async def counting(url, timeout):
        calls.append(url)
        return url in {"https://dead.example/x"}
    hits = [_Hit("https://dead.example/x"), _Hit("https://dead.example/x"), _Hit("https://live.example")]
    out = await drop_dead_urls(hits, prober=counting)
    assert calls.count("https://dead.example/x") == 1   # deduped
    assert [h.document_id for h in out] == ["https://live.example"]


@pytest.mark.asyncio
async def test_hits_without_url_are_kept():
    hits = [_Hit(""), _Hit("https://dead.example/x")]
    out = await drop_dead_urls(hits, prober=_prober({"https://dead.example/x"}))
    assert [h.document_id for h in out] == [""]   # blank-URL hit kept; dead one dropped
