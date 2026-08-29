"""Tech freshness policy — how strongly recency re-orders the verified-claim pool (flag-gated).

Tech is FAST-MOVING: a 2026 paper/repo/model-card/news item supersedes a 2024 one regardless of tier,
so recency must apply across ALL evidence tiers (min_rank=0) over a SHORT horizon (~2y). This is the
opposite of the medical default (recency only for the controlling tier, 12y — a landmark RCT must not
lose to a newer small trial). Consumed by the kernel ONLY under ROSTER_FRESHNESS_RANKING; OFF →
byte-identical to the kernel default. Kept a bounded BOOST (never a filter, never demotes below the
relevance baseline), so a highly-relevant seminal paper still wins its own question.
"""
from __future__ import annotations

TECH_FRESHNESS_POLICY = {
    "min_rank": 0,        # apply recency to EVERY tier (papers/code/news/web), not just filings
    "weight": 0.22,       # bounded additive boost — enough to break 2024-vs-2026 ties, below cosine
    "horizon_years": 2,   # linear decay to zero over 2 years (tech "recent" is months, not a decade)
}
