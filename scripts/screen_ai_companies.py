#!/usr/bin/env python3
"""Systematic AI-company universe screen (replaces ad-hoc hand-picking).

Method (reproducible, criteria-driven — companies SELF-IDENTIFY via their filings, we don't guess):
  1. EDGAR full-text search for 10-Ks that use AI-NATIVE terminology ("large language model",
     "foundation model", "generative artificial intelligence") — companies BUILDING/using AI write
     these, not merely risk-disclosing "AI".
  2. Structural filter to TECH SIC codes (357x computer hardware, 367x semiconductors/electronics,
     737x computer services/software) — removes banks/REITs/pharma/media that merely mention AI.
  3. Rank by weighted mention frequency across the queries; emit the top-N tickers.

Known limitation (honest): mention-frequency is a proxy, not market-cap/importance — it surfaces
verbose small-caps alongside leaders. The principled final cut would add an LLM semantic pass over
the candidates (Rule 18: the model owns the "is this genuinely AI-exposed" judgment). This screen is
the reproducible CANDIDATE GENERATOR; it is a large improvement over a hand-picked list.

Usage:  .venv/bin/python scripts/screen_ai_companies.py [N]   # prints N tickers (default 50) as JSON
Requires a real SEC User-Agent contact via ROSTER_HTTP_CONTACT (SEC fair-access).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
from collections import defaultdict

import httpx

UA = {"User-Agent": f"roster-research/0.1 ({os.environ.get('ROSTER_HTTP_CONTACT', 'research@roster.dev')})"}
QUERIES = [('"large language model"', 3.0), ('"foundation model"', 2.5),
           ('"generative artificial intelligence"', 1.5)]
EFTS = "https://efts.sec.gov/LATEST/search-index"
_TICK = re.compile(r"\(([A-Z][A-Z.]{0,5})(?:,[^)]*)?\)\s*\(CIK")


def _is_tech(sic: str) -> bool:
    try:
        n = int(sic)
    except (TypeError, ValueError):
        return False
    return (3570 <= n <= 3579) or (3670 <= n <= 3679) or (7370 <= n <= 7379) or n in (3576, 3661, 3663, 3827)


def screen(top_n: int = 50) -> list[str]:
    score: dict[str, float] = defaultdict(float)
    meta: dict[str, tuple[str, str, str]] = {}
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        for q, w in QUERIES:
            for frm in range(0, 150, 10):   # top ~150 relevance-ranked hits per query
                url = f"{EFTS}?q={urllib.parse.quote(q)}&forms=10-K&from={frm}"
                try:
                    hits = c.get(url).json().get("hits", {}).get("hits", [])
                except Exception:   # noqa: BLE001
                    break
                if not hits:
                    break
                for h in hits:
                    s = h.get("_source", {})
                    if not any(_is_tech(x) for x in (s.get("sics") or [])):
                        continue
                    ciks = s.get("ciks") or []
                    if not ciks:
                        continue
                    dn = (s.get("display_names") or [""])[0]
                    tm = _TICK.search(dn)
                    score[ciks[0]] += w
                    meta[ciks[0]] = (dn.split("(")[0].strip(), tm.group(1) if tm else "", (s.get("sics") or [""])[0])
                time.sleep(0.12)   # SEC fair-access pacing
    ranked = sorted(score, key=lambda k: -score[k])
    return [meta[c][1] for c in ranked if meta[c][1]][:top_n]


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    print(json.dumps(screen(n)))
