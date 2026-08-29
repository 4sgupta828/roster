"""Turn a Wikipedia page into a citable markdown reference + facets.

Wikipedia is a KEYLESS encyclopedic reference (MediaWiki API): the tech-genesis/evolution +
ecosystem-history layer. It is a SECONDARY source — it grounds history/genesis/ecosystem claims but
never overrides a primary filing, patent, or peer-reviewed paper. It grades `source_kind=reference`
→ `verified_structured` in `evidence_kind.classify` (a real boost above press, but NON-controlling:
only a filing is controlling). STRUCTURAL only (Rule 18): we render the plaintext extract and the
page's own category tags — we make NO judgment about meaning; the kernel's verbatim-span gate still
requires a real quote for every emitted sentence.
"""
from __future__ import annotations

_MAX_EXTRACT = 24000


def page_id(rec: dict) -> str:
    pid = rec.get("pageid")
    return str(pid) if pid is not None else str(rec.get("title") or "").strip()


def title(rec: dict) -> str:
    return str(rec.get("title") or "").strip()


def _categories(rec: dict) -> list[str]:
    """Category display names (strip the 'Category:' namespace prefix), structural extract."""
    out: list[str] = []
    for c in (rec.get("categories") or []):
        name = str((c.get("title") if isinstance(c, dict) else c) or "").strip()
        if name.startswith("Category:"):
            name = name[len("Category:"):]
        if name and not name.lower().startswith("hidden categories"):
            out.append(name)
    return out


def facets(rec: dict) -> dict:
    f = {
        "source_kind": "reference",
        "source_country": "global",
        "entity_type": "article",
        "wikipedia_pageid": page_id(rec),
    }
    cats = _categories(rec)
    if cats:
        f["category"] = cats[0].lower()
        if len(cats) > 1:
            f["category_2"] = cats[1].lower()
    # a clean 4-digit year only if explicitly present (no inference)
    y = str(rec.get("year") or "").strip()[:4]
    if y.isdigit() and len(y) == 4:
        f["year"] = y
    return {k: v for k, v in f.items() if v}


def to_markdown(rec: dict) -> str:
    t = title(rec) or page_id(rec)
    parts: list[str] = [f"# {t} — Wikipedia", ""]
    parts += [
        "_Encyclopedic reference (secondary source): grounds history/genesis/ecosystem "
        "context; does not override filings, patents, or peer-reviewed papers._",
        "",
    ]
    extract = str(rec.get("extract") or "").strip()
    if extract:
        if len(extract) > _MAX_EXTRACT:
            extract = extract[:_MAX_EXTRACT].rstrip() + " …"
        parts += [extract, ""]
    cats = _categories(rec)
    if cats:
        parts += ["## Categories", ", ".join(cats)]
    return "\n".join(parts).strip() + "\n"
