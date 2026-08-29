#!/usr/bin/env python3
"""Sector-AGNOSTIC company universe: the largest US public companies by revenue (data-driven, EDGAR).

Rank the whole XBRL universe by total revenue via the SEC `frames` API (no AI filter, no hand-picking)
→ the top-N biggest public companies across ALL sectors (retail, healthcare, tech, financials, energy…).
Each company's real sector is stamped at ingest from its SIC (sic_sector.py), so the corpus serves any
sector later. Merges two revenue tags + two fiscal years so off-cycle filers aren't missed.

Usage:  ROSTER_HTTP_CONTACT=you@example.com .venv/bin/python scripts/screen_companies.py [N] > universe.json
"""
from __future__ import annotations

import json
import os
import sys

import httpx

UA = {"User-Agent": f"roster-research/0.1 ({os.environ.get('ROSTER_HTTP_CONTACT', 'research@roster.dev')})"}
FRAMES = "https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/USD/{period}.json"
# Revenue is tag-fragmented; merge the modern + legacy tags across two recent fiscal years.
_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
_PERIODS = ["CY2024", "CY2023"]


def screen(top_n: int = 1000) -> list[str]:
    best: dict[str, tuple[float, str]] = {}
    with httpx.Client(timeout=40, headers=UA, follow_redirects=True) as c:
        for concept in _CONCEPTS:
            for period in _PERIODS:
                try:
                    rows = c.get(FRAMES.format(concept=concept, period=period)).json().get("data", [])
                except Exception:   # noqa: BLE001
                    continue
                for r in rows:
                    cik = str(r.get("cik") or "")
                    val = float(r.get("val") or 0)
                    if cik and val > best.get(cik, (0, ""))[0]:
                        best[cik] = (val, r.get("entityName") or "")
    ranked = sorted(best, key=lambda k: -best[k][0])[:top_n]
    return ranked   # CIKs (the EDGAR connector resolves a CIK directly)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
    print(json.dumps(screen(n)))
