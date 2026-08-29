"""Turn a normalized SEC/EDGAR filing record into a citable markdown document + facets.

A filing is long structured/HTML text — the connector normalizes it to a dict of named
sections. We synthesize ONE markdown document per filing (accession) so each section becomes a
span-verifiable block. Narrowing facets (form_type, cik, year, sector) ride every block.
"""
from __future__ import annotations

from . import sic_sector


def accession(rec: dict) -> str:
    return str(rec.get("accession", "")).strip()


def company(rec: dict) -> str:
    return str(rec.get("company", "")).strip()


def form_type(rec: dict) -> str:
    return str(rec.get("form_type", "")).strip()


def title(rec: dict) -> str:
    return f"{company(rec)} — {form_type(rec)}".strip(" —")


def facets(rec: dict) -> dict:
    filed = str(rec.get("filed", ""))
    f = {
        "source_kind": "filing",
        "source_country": "US",
        "entity_type": "company",
        "form_type": form_type(rec).lower(),
        "cik": str(rec.get("cik", "")).strip(),
        "ticker": str(rec.get("ticker", "")).upper(),
        "sic": str(rec.get("sic", "")).lower(),
        # sector-agnostic scope: an explicit override wins; else derive the company's real sector
        # from its SIC (so a broad, all-sector corpus carries each filing's true sector).
        "sector": str(rec.get("sector") or sic_sector.classify(rec.get("sic"))).lower(),
    }
    if filed[:4].isdigit():
        f["year"] = filed[:4]
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    if rec.get("cik"):
        parts += [f"SEC CIK: {rec['cik']}    Accession: {accession(rec)}    Filed: {rec.get('filed','')}", ""]
    for heading, body in (rec.get("sections") or {}).items():
        body = str(body).strip()
        if body:
            parts += [f"## {heading}", body, ""]
    return "\n".join(parts).strip() + "\n"
