"""Turn Wikidata structured facts about a COMPANY into a citable markdown profile + facets.

Wikidata is a KEYLESS fallback for the commercial-DB gap (Crunchbase/PitchBook): community-curated
structured facts — founders, CEO, inception, industry, parent/subsidiary, ownership, ticker — that
also feed M&A relationship mapping. It is community data, NOT an audited record, so it grades as
`source_kind=reference` → neutral tier (rank 0 in `evidence_kind.classify`: retrievable + grounded by
the span gate, but never authority-boosting and never controlling). STRUCTURAL only (Rule 18): we read
the property values Wikidata publishes, we don't judge meaning; the kernel's verbatim-span gate still
requires a real quote for every emitted sentence.
"""
from __future__ import annotations

# property -> readable role (order = display order in the profile). Entity values resolve to labels;
# literal values (inception date, ticker) come back as-is via the label service.
ROLE_PROPS: list[tuple[str, str]] = [
    ("P112", "Founder"), ("P169", "CEO"), ("P1037", "Director/Manager"),
    ("P571", "Inception"), ("P452", "Industry"), ("P159", "Headquarters"),
    ("P17", "Country"), ("P1128", "Employees"), ("P749", "Parent organization"),
    ("P355", "Subsidiary"), ("P127", "Owned by"), ("P1830", "Owner of"),
    ("P1056", "Product/Material"), ("P414", "Stock exchange"), ("P249", "Ticker"),
    ("P856", "Official website"),
]
_ROLE_ORDER = [r for _, r in ROLE_PROPS]


def sparql_for(qid: str) -> str:
    """One query returning (role, valueLabel) rows for the profile properties of a QID."""
    values = " ".join(f'(wdt:{p} "{role}")' for p, role in ROLE_PROPS)
    return (
        "SELECT ?role ?vLabel WHERE { "
        f"VALUES (?p ?role) {{ {values} }} "
        f"wd:{qid} ?p ?v. "
        'SERVICE wikibase:label { bd:serviceParam wikibase:language "en". } } LIMIT 200'
    )


def rows_from_sparql(payload: dict) -> list[tuple[str, str]]:
    """Extract [(role, value)] from a SPARQL JSON result, preserving property order + de-duping."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for b in (payload.get("results", {}) or {}).get("bindings", []) or []:
        role = str((b.get("role") or {}).get("value") or "").strip()
        val = str((b.get("vLabel") or {}).get("value") or "").strip()
        if role and val and (role, val.lower()) not in seen:
            seen.add((role, val.lower()))
            out.append((role, val))
    out.sort(key=lambda rv: _ROLE_ORDER.index(rv[0]) if rv[0] in _ROLE_ORDER else 999)
    return out


def _grouped(rows: list[tuple[str, str]]) -> dict[str, list[str]]:
    g: dict[str, list[str]] = {}
    for role, val in rows:
        g.setdefault(role, []).append(val)
    return g


def facets(qid: str, rows: list[tuple[str, str]]) -> dict:
    g = _grouped(rows)
    country = " ".join(g.get("Country", [])).lower()
    f = {
        "source_kind": "reference",
        "entity_type": "company",
        "source_country": "US" if ("united states" in country) else "global",
        "wikidata_qid": qid,
        "industry": (g.get("Industry", [""])[0]).lower(),
    }
    # a 4-digit inception year, if present (structural extract)
    for v in g.get("Inception", []):
        y = v[:4]
        if y.isdigit():
            f["year"] = y
            break
    return {k: v for k, v in f.items() if v}


def to_markdown(title: str, qid: str, rows: list[tuple[str, str]], description: str = "") -> str:
    parts: list[str] = [f"# {title} — company profile", ""]
    parts += [f"Wikidata: {qid}", ""]
    if description:
        parts += [description.strip(), ""]
    for role in _ROLE_ORDER:
        vals = [v for r, v in rows if r == role]
        if vals:
            parts.append(f"- **{role}:** " + ", ".join(vals))
    return "\n".join(parts).strip() + "\n"
