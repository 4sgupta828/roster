"""Turn a Semantic Scholar (S2 Graph API) paper record into a citable markdown document + facets.

S2 papers are scholarly works → `source_kind=paper`, so they slot into the SAME tier logic as
OpenAlex (`evidence_kind.classify`): a peer-reviewed venue → `verified_structured`, an arXiv-only
preprint → `technical_signal`. The `doi` + `arxiv_id` facets let a later pass de-dup the same paper
against its arXiv preprint / OpenAlex record (tracked in docs/downloads-blocked.md). STRUCTURAL only
(Rule 18): we read the tags S2 publishes about the work, we do not judge its meaning.
"""
from __future__ import annotations

# S2 fields we request; kept here so the connector and helper stay in sync.
FIELDS = ("paperId,title,abstract,year,venue,externalIds,citationCount,"
          "authors,publicationTypes,publicationVenue")


def s2_id(rec: dict) -> str:
    return str(rec.get("paperId") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("title") or "").strip()


def _ext(rec: dict) -> dict:
    e = rec.get("externalIds")
    return e if isinstance(e, dict) else {}


def _venue(rec: dict) -> str:
    v = rec.get("venue")
    if isinstance(v, str) and v.strip():
        return v.strip()
    pv = rec.get("publicationVenue")
    if isinstance(pv, dict):
        return str(pv.get("name") or "").strip()
    return ""


def _is_peer_reviewed(rec: dict) -> bool:
    """Peer-reviewed = published in a JOURNAL or CONFERENCE (top CS/AI venues are conferences).
    An arXiv-only preprint (no venue) or an explicit 'Preprint' type is NOT (→ technical_signal)."""
    types = {str(t).lower() for t in (rec.get("publicationTypes") or []) if t}
    if "preprint" in types:
        return False
    pv = rec.get("publicationVenue") if isinstance(rec.get("publicationVenue"), dict) else {}
    vtype = str(pv.get("type") or "").lower()
    if vtype in ("journal", "conference", "proceedings", "book series"):
        return True
    # No structured venue type → accept only if S2 tags it a journal/conference/review article AND a
    # real venue string exists; else treat as not-reviewed (fail-safe toward the lower tier).
    if _venue(rec) and types & {"journalarticle", "conference", "review"}:
        return True
    return False


def facets(rec: dict) -> dict:
    ext = _ext(rec)
    f = {
        "source_kind": "paper",
        "source_country": "global",
        "entity_type": "paper",
        "is_peer_reviewed": "true" if _is_peer_reviewed(rec) else "false",
        "venue": _venue(rec).lower(),
        "doi": str(ext.get("DOI") or "").replace("https://doi.org/", "").lower(),
        "arxiv_id": str(ext.get("ArXiv") or "").lower(),   # for arXiv↔S2 de-dup later
        "cited_by_count": str(rec.get("citationCount") if rec.get("citationCount") is not None else ""),
    }
    yr = rec.get("year")
    if yr and str(yr).isdigit():
        f["year"] = str(yr)
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    authors = [str(a.get("name")) for a in (rec.get("authors") or [])
               if isinstance(a, dict) and a.get("name")]
    ext = _ext(rec)
    parts: list[str] = [f"# {title(rec)}", ""]
    idline = f"Semantic Scholar ID: {s2_id(rec)}" if s2_id(rec) else ""
    if ext.get("DOI"):
        idline += f"    DOI: {str(ext['DOI']).replace('https://doi.org/', '')}"
    if ext.get("ArXiv"):
        idline += f"    arXiv: {ext['ArXiv']}"
    if idline:
        parts += [idline.strip(), ""]
    if _venue(rec):
        parts += ["## Venue", _venue(rec) + (f" ({rec['year']})" if rec.get("year") else ""), ""]
    if authors:
        parts += ["## Authors", ", ".join(authors), ""]
    ab = rec.get("abstract")
    if isinstance(ab, str) and ab.strip():
        parts += ["## Abstract", ab.strip(), ""]
    return "\n".join(parts).strip() + "\n"
