"""Turn a Crossref REST work record into a citable markdown document + facets.

Crossref registers DOIs → rich bibliographic metadata (title, venue, authors, year, citation count,
work type). Like OpenAlex/S2 it's `source_kind=paper`, so it grades via the SAME tier logic
(`evidence_kind.classify`): a journal/conference article → `verified_structured`; a `posted-content`
preprint → `technical_signal`. Abstracts (when present) come as JATS XML we strip to text. STRUCTURAL
only (Rule 18) — we read the work TYPE Crossref assigns, we don't judge meaning. The `doi` facet lets a
later pass de-dup against arXiv/OpenAlex/S2.
"""
from __future__ import annotations

import re

FIELDS = ("DOI,title,abstract,container-title,issued,published,author,"
          "is-referenced-by-count,type,publisher")

# work types Crossref assigns to formally-reviewed publications (vs preprints/datasets/etc.)
_REVIEWED_TYPES = {"journal-article", "proceedings-article", "book-chapter"}
_TAG = re.compile(r"<[^>]+>")


def doi(rec: dict) -> str:
    return str(rec.get("DOI") or "").strip().lower()


def title(rec: dict) -> str:
    t = rec.get("title")
    if isinstance(t, list) and t:
        return str(t[0]).strip()
    return str(t or "").strip()


def _first(rec: dict, key: str) -> str:
    v = rec.get(key)
    if isinstance(v, list) and v:
        return str(v[0]).strip()
    return str(v or "").strip()


def _year(rec: dict) -> str:
    for key in ("issued", "published", "published-online", "published-print"):
        dp = (rec.get(key) or {}).get("date-parts") if isinstance(rec.get(key), dict) else None
        if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
            y = dp[0][0]
            if str(y).isdigit():
                return str(y)
    return ""


def _abstract(rec: dict) -> str:
    ab = rec.get("abstract")
    if not isinstance(ab, str) or not ab.strip():
        return ""
    return re.sub(r"\s+", " ", _TAG.sub(" ", ab)).strip()   # strip JATS XML tags → plain text


def _authors(rec: dict) -> list[str]:
    out = []
    for a in (rec.get("author") or []):
        if not isinstance(a, dict):
            continue
        nm = " ".join(x for x in [str(a.get("given") or "").strip(), str(a.get("family") or "").strip()] if x)
        if nm:
            out.append(nm)
    return out


def _is_peer_reviewed(rec: dict) -> bool:
    return str(rec.get("type") or "").lower() in _REVIEWED_TYPES


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "paper",
        "source_country": "global",
        "entity_type": "paper",
        "is_peer_reviewed": "true" if _is_peer_reviewed(rec) else "false",
        "venue": _first(rec, "container-title").lower(),
        "doi": doi(rec),
        "crossref_type": str(rec.get("type") or "").lower(),
        "cited_by_count": str(rec.get("is-referenced-by-count")
                              if rec.get("is-referenced-by-count") is not None else ""),
    }
    yr = _year(rec)
    if yr:
        f["year"] = yr
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    parts: list[str] = [f"# {title(rec)}", ""]
    if doi(rec):
        parts += [f"DOI: {doi(rec)}", ""]
    venue = _first(rec, "container-title")
    if venue:
        parts += ["## Venue", venue + (f" ({_year(rec)})" if _year(rec) else ""), ""]
    authors = _authors(rec)
    if authors:
        parts += ["## Authors", ", ".join(authors), ""]
    ab = _abstract(rec)
    if ab:
        parts += ["## Abstract", ab, ""]
    return "\n".join(parts).strip() + "\n"
