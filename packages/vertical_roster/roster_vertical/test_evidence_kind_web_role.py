"""Tests for the open-web `web_role` provenance fallback in `evidence_kind.classify`
(flag ROSTER_AUTHORITY_BASIS, Task T3).

A screened open-web hit carries NO connector `source_kind`, so without a signal it grades to ""
(rank 0) and the authority-basis partition would sink even a good non-whitelisted source. The
open-web quality screen stamps a coarse `web_role` facet on kept hits; classify maps that role to the
SAME tier an equivalent source_kind would earn. STRUCTURAL (Rule 18): it reads a stamped role, judges
nothing. The fallback fires ONLY when source_kind is EMPTY and web_role is present — so an unstamped
(flag-off) hit is byte-identical to before.
"""
from __future__ import annotations

from . import evidence_kind
from .authority import TechAuthorityPolicy


def test_web_role_maps_to_expected_tiers():
    c = evidence_kind.classify
    # official (company/official page) → technical_signal (tier 2)
    assert c("web", {"web_role": "official"}) == "technical_signal"
    # independent_analysis (reputable independent coverage) → analysis (tier 4)
    assert c("web", {"web_role": "independent_analysis"}) == "analysis"
    # expert_opinion (named expert essay/newsletter) → expert_analysis (tier 3)
    assert c("web", {"web_role": "expert_opinion"}) == "expert_analysis"
    # social post → sentiment_signal (tier 1)
    assert c("web", {"web_role": "social"}) == "sentiment_signal"


def test_web_role_tier_ordering():
    pol = TechAuthorityPolicy()
    c = evidence_kind.classify
    assert pol.rank(c("web", {"web_role": "social"})) == 1
    assert pol.rank(c("web", {"web_role": "official"})) == 2
    assert pol.rank(c("web", {"web_role": "expert_opinion"})) == 3
    assert pol.rank(c("web", {"web_role": "independent_analysis"})) == 4
    # official (self-reported) must sit below independent press — same discipline as corp_eng.
    assert pol.rank(c("web", {"web_role": "official"})) < pol.rank(c("web", {"web_role": "independent_analysis"}))


def test_aggregator_and_unknown_role_stay_rank_zero():
    c = evidence_kind.classify
    assert c("web", {"web_role": "aggregator"}) == ""   # thin aggregator → rank 0
    assert c("web", {"web_role": "mystery"}) == ""      # unknown role → rank 0
    assert c("web", {"web_role": ""}) == ""             # blank → rank 0


def test_no_web_role_is_unchanged_byte_identical():
    # The whole point: an open-web hit with NO web_role classifies exactly as before (rank 0).
    assert evidence_kind.classify("web", {}) == ""
    assert evidence_kind.classify("web", None) == ""


def test_source_kind_wins_over_web_role():
    # web_role is a FALLBACK — a real connector source_kind still takes precedence (fallback only
    # fires when source_kind is empty), so a stray web_role never overrides a stamped kind.
    c = evidence_kind.classify
    assert c("web", {"source_kind": "news", "web_role": "social"}) == "analysis"
    assert c("edgar", {"web_role": "social"}) == "primary_filing"
