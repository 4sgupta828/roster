"""Turn an arXiv record into a citable markdown document + facets.

A paper is structured metadata, not a PDF — synthesize ONE markdown document per arXiv id from
its text-bearing fields (title, authors, categories, abstract). The kernel splits it into
section blocks, each span-verifiable. Narrowing facets are denormalized onto every block.
"""
from __future__ import annotations


def arxiv_id(rec: dict) -> str:
    return str(rec.get("id", "")).strip()


def title(rec: dict) -> str:
    return str(rec.get("title", "")).strip()


def facets(rec: dict) -> dict:
    published = str(rec.get("published", ""))
    f = {
        "source_kind": "preprint",
        "source_country": "global",
        "entity_type": "paper",
        "primary_category": str(rec.get("primary_category", "")).lower(),
        "sector": str(rec.get("sector", "")).lower(),          # fixture stamps the sub-vertical
        "is_peer_reviewed": "false",                            # arXiv is unreviewed → technical_signal tier
    }
    if published[:4].isdigit():
        f["year"] = published[:4]
    cats = rec.get("categories") or []
    if cats:
        f["category"] = str(cats[0]).lower()
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    authors = rec.get("authors") or []
    cats = rec.get("categories") or []
    parts: list[str] = [f"# {title(rec)}", ""]
    if rec.get("id"):
        parts += [f"arXiv ID: {rec['id']}", ""]
    if authors:
        parts += ["## Authors", ", ".join(str(a) for a in authors), ""]
    if cats:
        parts += ["## Categories", ", ".join(str(c) for c in cats), ""]
    if rec.get("summary"):
        parts += ["## Abstract", str(rec["summary"]).strip(), ""]
    return "\n".join(parts).strip() + "\n"
