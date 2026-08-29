"""Turn an OpenReview note (API v2) into a citable markdown document + facets.

OpenReview carries PEER-REVIEW outcome, which is exactly what separates reviewed/accepted work
from an unreviewed arXiv preprint. A note whose venue/decision indicates ACCEPTANCE stamps
`is_peer_reviewed=true` → `evidence_kind.classify` returns `verified_structured` (the paper
branch); an un-accepted submission ("Submitted"/"Withdrawn"/"Desk Reject…") stays `false`
→ `technical_signal`, the same tier as a raw preprint.

STRUCTURAL only (Rule 18): we read the venue/decision string the venue itself published — we do
NOT judge the paper's meaning. API v2 nests every content field as {"value": x}; `_val` unwraps
both that envelope and a bare value.
"""
from __future__ import annotations

# Venue/decision phrases that mean "not (yet) accepted" — presence of any of these in the venue
# string means the note is NOT peer-review-accepted, even though a venue field exists.
_NOT_ACCEPTED = ("submitted", "withdrawn", "desk reject", "desk-reject", "rejected", "reject")


def _val(content: dict, key: str):
    """Read an API-v2 content field: unwrap the {"value": x} envelope OR return a bare value."""
    raw = (content or {}).get(key)
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"]
    return raw


def _content(rec: dict) -> dict:
    c = (rec or {}).get("content")
    return c if isinstance(c, dict) else {}


def note_id(rec: dict) -> str:
    return str((rec or {}).get("id") or (rec or {}).get("forum") or "").strip()


def title(rec: dict) -> str:
    return str(_val(_content(rec), "title") or "").strip()


def _venue(rec: dict) -> str:
    return str(_val(_content(rec), "venue") or "").strip()


def _is_peer_reviewed(rec: dict) -> bool:
    """True iff a venue/decision string is present AND does not merely say the work is un-accepted.

    An acceptance venue like "NeurIPS 2024 poster" or "ICLR 2024" → reviewed; a bare
    "Submitted to …"/"Withdrawn"/"Desk Reject" → not reviewed. Structural, not semantic.
    """
    venue = _venue(rec).lower()
    if not venue:
        return False
    return not any(phrase in venue for phrase in _NOT_ACCEPTED)


def _year(rec: dict) -> str:
    """First 4-digit year found in the venue string, else ''."""
    import re
    m = re.search(r"(19|20)\d{2}", _venue(rec))
    return m.group(0) if m else ""


def facets(rec: dict) -> dict:
    venue = _venue(rec)
    f = {
        "source_kind": "paper",
        "source_country": "global",
        "entity_type": "paper",
        "venue": venue,
        "is_peer_reviewed": "true" if _is_peer_reviewed(rec) else "false",
    }
    year = _year(rec)
    if year:
        f["year"] = year
    return {k: v for k, v in f.items() if v or k == "is_peer_reviewed"}


def to_markdown(rec: dict) -> str:
    content = _content(rec)
    reviewed = _is_peer_reviewed(rec)
    venue = _venue(rec)
    parts: list[str] = [f"# {title(rec)}", ""]
    meta = ["OpenReview submission"]
    if venue:
        meta.append(f"venue: {venue}")
    meta.append(f"peer-reviewed: {'yes' if reviewed else 'no'}")
    parts += [" · ".join(meta), ""]
    abstract = _val(content, "abstract")
    if isinstance(abstract, str) and abstract.strip():
        parts += ["## Abstract", abstract.strip(), ""]
    keywords = _val(content, "keywords")
    if isinstance(keywords, (list, tuple)) and keywords:
        parts += ["## Keywords", ", ".join(str(k) for k in keywords), ""]
    elif isinstance(keywords, str) and keywords.strip():
        parts += ["## Keywords", keywords.strip(), ""]
    return "\n".join(parts).strip() + "\n"
