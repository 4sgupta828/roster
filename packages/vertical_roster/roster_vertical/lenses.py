"""The analytical LENSES for tech diligence (the second orthogonal axis).

These are the aspects the claims-first extractor covers per atom (one checklist in ONE
extraction call — the kernel consumes them). They are ANGLES applied within whatever sector
is active, NOT sub-verticals. The same list also seeds the panel specialist roster (P3).
"""
from __future__ import annotations

EXTRACTION_LENSES: tuple[str, ...] = (
    "funding & traction (rounds, investors, revenue/loss, customers, hiring or repo momentum)",
    "technology & IP (architecture/approach, benchmark results, granted vs. pending patents)",
    "competitive landscape (named competitors, substitutes, moats, comparisons)",
    "market sentiment SIGNAL (news/forum/social tone — perception and momentum, never stated as fact)",
    "team & execution (founders, prior exits, key hires, org signals)",
)
