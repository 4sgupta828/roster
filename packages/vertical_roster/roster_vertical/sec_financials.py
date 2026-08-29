"""Extract clean financial highlights from SEC XBRL companyfacts (structured, not HTML-scraped).

The us-gaap facts are the diligence gold — audited numbers with fiscal periods. We read the most
recent ANNUAL (10-K) values for a handful of headline concepts. Structural only (Rule 18): we read
the tags the filer reported, we do not infer meaning.
"""
from __future__ import annotations

# Headline concepts → (label, [candidate us-gaap tags, first present wins]).
_CONCEPTS: list[tuple[str, list[str]]] = [
    ("Revenue", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                 "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet"]),
    ("Net income (loss)", ["NetIncomeLoss", "ProfitLoss"]),
    ("Gross profit", ["GrossProfit"]),
    ("R&D expense", ["ResearchAndDevelopmentExpense"]),
    ("Total assets", ["Assets"]),
    ("Cash & equivalents", ["CashAndCashEquivalentsAtCarryingValue"]),
    ("Stockholders' equity", ["StockholdersEquity"]),
]


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.1f}M"
    return f"${v:,.0f}"


def _latest_annual(fact: dict, n: int = 2) -> list[tuple[str, float]]:
    """Return up to n most-recent ANNUAL (fy full-year, from a 10-K) datapoints as (fy, val)."""
    usd = (fact.get("units") or {}).get("USD") or []
    annual = [d for d in usd if d.get("form") in ("10-K", "10-K/A") and d.get("fp") == "FY" and "val" in d]
    # dedup by fiscal year, keep the latest 'end'
    by_fy: dict[int, dict] = {}
    for d in annual:
        fy = d.get("fy")
        if fy is None:
            continue
        if fy not in by_fy or (d.get("end", "") > by_fy[fy].get("end", "")):
            by_fy[fy] = d
    rows = sorted(by_fy.values(), key=lambda d: d.get("end", ""), reverse=True)[:n]
    return [(str(d.get("fy")), float(d["val"])) for d in rows]


def highlights_markdown(companyfacts: dict) -> str:
    """Build a Financial Highlights markdown section (most recent 2 annual periods), or '' if none."""
    usg = (companyfacts.get("facts") or {}).get("us-gaap") or {}
    lines: list[str] = []
    for label, tags in _CONCEPTS:
        # Among candidate tags, pick the one whose latest annual datapoint is MOST RECENT (companies
        # switch XBRL tags over time; the first-present tag can be a legacy one that stopped updating).
        best_pts: list[tuple[str, float]] = []
        best_end = ""
        for t in tags:
            if t not in usg:
                continue
            pts = _latest_annual(usg[t])
            if pts and pts[0][0] > best_end:   # compare latest fiscal-year label
                best_pts, best_end = pts, pts[0][0]
        if not best_pts:
            continue
        pts = best_pts
        rendered = "; ".join(f"FY{fy}: {_fmt(val)}" for fy, val in pts)
        lines.append(f"- {label}: {rendered}")
    if not lines:
        return ""
    return "## Financial Highlights (from XBRL)\n" + "\n".join(lines) + "\n"
