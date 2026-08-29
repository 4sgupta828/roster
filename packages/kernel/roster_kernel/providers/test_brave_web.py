"""Tests for BraveWebSearch — keyless fail-safe + recency mapping (no network)."""
import asyncio

from roster_kernel.providers.brave_web import BraveWebSearch, _freshness


def test_keyless_returns_empty_no_network():
    # no key → contribute nothing (never raises, never calls out) → harmless in the composite
    out = asyncio.run(BraveWebSearch(api_key="").search("anything", max_results=5))
    assert out == []


def test_freshness_mapping():
    assert _freshness(None) == ""
    assert _freshness(0) == ""
    assert _freshness(1) == "pd"
    assert _freshness(5) == "pw"
    assert _freshness(20) == "pm"
    assert _freshness(400) == "py"


def test_search_signature_matches_port():
    # must accept the composite's kwargs so it isn't silently dropped by CompositeWebSearch
    b = BraveWebSearch(api_key="")
    out = asyncio.run(b.search("q", max_results=3, open_web=True, recency_days=30, max_chars=2000))
    assert out == []
