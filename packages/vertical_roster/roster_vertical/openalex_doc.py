"""Turn an OpenAlex work record into a citable markdown document + facets.

OpenAlex works are scholarly papers → `source_kind=paper`. Peer-reviewed journal articles are the
`verified_structured` tier (a step above arXiv preprints). The `doi` facet lets a later pass de-dup
the same paper against its arXiv preprint. Abstracts come as an inverted index we reconstruct.
"""
from __future__ import annotations


def openalex_id(rec: dict) -> str:
    """The short OpenAlex work id (e.g. W2741809807) from the full IRI."""
    raw = str(rec.get("id", "")).rstrip("/")
    return raw.rsplit("/", 1)[-1]


def title(rec: dict) -> str:
    return str(rec.get("title") or rec.get("display_name") or "").strip()


def _abstract(rec: dict) -> str:
    """Reconstruct the abstract from OpenAlex's abstract_inverted_index (word -> [positions])."""
    inv = rec.get("abstract_inverted_index")
    if not isinstance(inv, dict) or not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs or []:
            positions.append((int(i), word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _venue(rec: dict) -> str:
    loc = rec.get("primary_location") or {}
    src = (loc.get("source") or {}) if isinstance(loc, dict) else {}
    return str(src.get("display_name") or "").strip()


def _is_peer_reviewed(rec: dict) -> bool:
    loc = rec.get("primary_location") or {}
    src = (loc.get("source") or {}) if isinstance(loc, dict) else {}
    stype = str(src.get("type") or "").lower()
    wtype = str(rec.get("type") or "").lower()
    # Peer-reviewed = published in a JOURNAL or a CONFERENCE/proceedings (in CS/AI the top venues are
    # conferences). Preprint servers report source type 'repository' (arXiv/bioRxiv) → NOT peer-reviewed
    # (those grade as technical_signal). A missing source or a 'preprint' work type is also NOT.
    if wtype in ("preprint", "") or stype in ("repository", ""):
        return False
    return stype in ("journal", "conference", "proceedings", "book series")


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "paper",
        "source_country": "global",
        "entity_type": "paper",
        "is_peer_reviewed": "true" if _is_peer_reviewed(rec) else "false",
        "venue": _venue(rec).lower(),
        "doi": str(rec.get("doi") or "").replace("https://doi.org/", "").lower(),
        "cited_by_count": str(rec.get("cited_by_count") or ""),
    }
    yr = rec.get("publication_year")
    if yr and str(yr).isdigit():
        f["year"] = str(yr)
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    authors = []
    for a in (rec.get("authorships") or []):
        nm = ((a.get("author") or {}).get("display_name")) if isinstance(a, dict) else None
        if nm:
            authors.append(str(nm))
    parts: list[str] = [f"# {title(rec)}", ""]
    if openalex_id(rec):
        line = f"OpenAlex ID: {openalex_id(rec)}"
        if rec.get("doi"):
            line += f"    DOI: {str(rec['doi']).replace('https://doi.org/', '')}"
        parts += [line, ""]
    if _venue(rec):
        parts += ["## Venue", _venue(rec) + (f" ({rec['publication_year']})" if rec.get("publication_year") else ""), ""]
    if authors:
        parts += ["## Authors", ", ".join(authors), ""]
    ab = _abstract(rec)
    if ab:
        parts += ["## Abstract", ab, ""]
    return "\n".join(parts).strip() + "\n"
