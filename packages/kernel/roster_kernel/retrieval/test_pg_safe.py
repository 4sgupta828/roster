"""Postgres-safety sanitizer for materialized block text/facets — strips NUL bytes and invalid
UTF-8 so one bad-byte doc can't fail the whole ingest batch ('invalid byte sequence for encoding
UTF8'), which is how an arXiv job lost all 25 of its docs."""
from roster_kernel.retrieval.materialize import _pg_safe, _safe_facets


def test_strips_null_bytes():
    assert _pg_safe("a\x00b\x00c") == "abc"


def test_drops_invalid_utf8_surrogates():
    assert _pg_safe("x\udc80y") == "xy"


def test_clean_text_unchanged():
    assert _pg_safe("Quantum error correction — 99.9% fidelity") == "Quantum error correction — 99.9% fidelity"


def test_non_strings_pass_through():
    assert _pg_safe(None) is None
    assert _pg_safe(42) == 42


def test_facets_keys_and_values_sanitized():
    assert _safe_facets({"sector": "quantum", "bad\x00key": "bad\x00val"}) == {
        "sector": "quantum", "badkey": "badval"}
