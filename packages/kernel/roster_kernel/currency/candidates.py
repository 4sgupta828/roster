"""Edition-candidate generation — STRUCTURAL pairing for the supersession judge (spec 3.2).

Pure function over document metadata: same issuer + overlapping subject conditions + different
years → (older, newer) pairs. Deliberately over-inclusive within those bounds (the LLM judge is
the semantic gate, Rule 18); deliberately bounded (never pairs across issuers, never same-year,
never subject-disjoint — those aren't edition candidates in any vertical).
"""
from __future__ import annotations


def _year(d: dict) -> int:
    s = str(d.get("year") or "").strip()[:4]
    return int(s) if s.isdigit() else 0


def edition_candidates(docs: list[dict], *, exclude_pairs: set[tuple[str, str]] | None = None,
                       max_pairs: int = 50) -> list[tuple[dict, dict]]:
    """Return (older, newer) candidate pairs. `exclude_pairs` = {(old_id, new_id)} already in the
    ledger (any status) — never re-judge a decided pair."""
    exclude = exclude_pairs or set()
    out: list[tuple[dict, dict]] = []
    docs = [d for d in docs if d.get("issuer") and _year(d)]
    for i, a in enumerate(docs):
        for b in docs[i + 1:]:
            if a.get("issuer") != b.get("issuer"):
                continue
            ya, yb = _year(a), _year(b)
            if ya == yb:
                continue
            ca = {str(c).lower() for c in (a.get("conditions") or [])}
            cb = {str(c).lower() for c in (b.get("conditions") or [])}
            if not (ca and cb and ca & cb):
                continue
            old, new = (a, b) if ya < yb else (b, a)
            if (old["document_id"], new["document_id"]) in exclude:
                continue
            out.append((old, new))
            if len(out) >= max_pairs:
                return out
    return out
