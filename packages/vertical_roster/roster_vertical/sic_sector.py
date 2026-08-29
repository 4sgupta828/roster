"""Map a SIC code → a broad industry SECTOR (structural, Rule 18 — reads the filer's declared SIC).

The `sector` facet is sector-AGNOSTIC scoping: every ingested company carries its real sector so the
corpus serves ALL sectors (a fintech or biotech question filters to its sector later), not just AI.
Coarse GICS-like buckets; unknown SIC → "" (no sector stamped, fail-safe).
"""
from __future__ import annotations


def classify(sic: str | int | None) -> str:
    try:
        n = int(sic)
    except (TypeError, ValueError):
        return ""
    # technology (computer hw/sw/services, semiconductors, electronics, comms equipment)
    if (3570 <= n <= 3579) or (3670 <= n <= 3679) or (7370 <= n <= 7379) or n in (3661, 3663, 3669, 3827, 3559, 3576):
        return "technology"
    # financials (banks, insurance, funds, real-estate finance, holding)
    if 6000 <= n <= 6799:
        return "financials"
    # healthcare (pharma, biotech, devices, providers)
    if (2833 <= n <= 2836) or n == 2834 or (3826 <= n <= 3851 and n not in (3827,)) or (8000 <= n <= 8099) or n in (2830, 2835, 2836):
        return "healthcare"
    # energy (oil/gas, coal, mining energy)
    if (1300 <= n <= 1399) or (2900 <= n <= 2999) or n in (1220, 1221, 1222, 1311, 1381, 1389):
        return "energy"
    # utilities
    if 4900 <= n <= 4999:
        return "utilities"
    # communications / media / telecom
    if (4800 <= n <= 4899) or (2700 <= n <= 2799) or (7800 <= n <= 7849) or n in (4832, 4833):
        return "communications"
    # consumer (retail, wholesale, food, apparel, autos-consumer, leisure)
    if (5000 <= n <= 5999) or (2000 <= n <= 2199) or (2300 <= n <= 2399) or (3700 <= n <= 3716) or (7000 <= n <= 7299):
        return "consumer"
    # materials (chemicals ex-pharma, metals, mining ex-energy, paper)
    if (2800 <= n <= 2899) or (1000 <= n <= 1099) or (3300 <= n <= 3399) or (2600 <= n <= 2699) or (1400 <= n <= 1499):
        return "materials"
    # industrials (machinery, aerospace, transport, construction, engineering services)
    if (3400 <= n <= 3569) or (3580 <= n <= 3669) or (3717 <= n <= 3799) or (1500 <= n <= 1799) or (4000 <= n <= 4799) or (8700 <= n <= 8744):
        return "industrials"
    # real estate (operators, REITs land)
    if 6500 <= n <= 6599:
        return "real_estate"
    return ""
