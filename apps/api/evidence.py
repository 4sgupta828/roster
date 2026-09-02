"""EVIDENCE TYPING for the talent-intelligence redesign (docs/specs/talent-intelligence-redesign.md).

The spec's core discipline: evidence is TYPED, not flattened. Every person row carries an evidence
packet saying what KIND of support each claim has — self-stated, employer-stated, artifact-backed,
structured, corroborated — plus gaps. Classification is CODE-OWNED, derived from each facet's
source provenance (never model-asserted), so a self-stated bio can never silently render as
verified. App-level module (recruiting vocabulary allowed); the kernel stays domain-free.

Source-family → evidence type, grounded in what each source actually IS:
- github      → self_stated      (facets are extracted from the person's own profile/bio)
- openalex    → structured       (scholarly registry; affiliations/concepts derive from
                                  published works — noted as publication-derived)
- yc/npi/sec  → structured       (constrained public registries)
- theorg      → employer_stated  (organization chart pages)
- aifund/ef/pear/sosv → employer_stated (portfolio/program pages)
CORROBORATED = the same claim value supported by ≥2 INDEPENDENT families (rare today: entities are
not yet cross-source deduped — honesty over inflation).
"""
from __future__ import annotations

# family detection from a facet's source_document_id (fallback: the entity id prefix)
_DOC_FAMILY_PATTERNS = (
    ("openalex.org", "openalex"), ("github", "github"), ("ycombinator.com", "yc"),
    ("npiregistry", "npi"), ("theorg.com", "theorg"), ("edgar", "sec"), ("sec.gov", "sec"),
    ("aifund.ai", "aifund"), ("joinef.com", "ef"), ("pear.vc", "pear"), ("sosv", "sosv"),
)

FAMILY_EVIDENCE_TYPE = {
    "github": "self_stated",
    "openalex": "structured",
    "yc": "structured",
    "npi": "structured",
    "sec": "structured",
    "theorg": "employer_stated",
    "aifund": "employer_stated",
    "ef": "employer_stated",
    "pear": "employer_stated",
    "sosv": "employer_stated",
}

# ladder position (higher = stronger support for what the source constrains)
_STRENGTH_RANK = {"corroborated": 4, "structured": 3, "artifact_backed": 3,
                  "employer_stated": 2, "self_stated": 1}

EVIDENCE_TYPE_LABELS = {
    "self_stated": "self-stated",
    "employer_stated": "employer-stated",
    "artifact_backed": "artifact-backed",
    "structured": "structured record",
    "corroborated": "corroborated",
}

# human framing per family for the Evidence Rail ("where this comes from")
FAMILY_LABELS = {
    "github": "the person's own GitHub profile",
    "openalex": "OpenAlex scholarly registry (derived from published works)",
    "yc": "Y Combinator company directory",
    "npi": "NPI clinician registry",
    "sec": "SEC EDGAR filings",
    "theorg": "TheOrg company org-chart page",
    "aifund": "AI Fund portfolio page",
    "ef": "Entrepreneur First company page",
    "pear": "Pear VC portfolio page",
    "sosv": "SOSV portfolio page",
}


def facet_family(source_document_id: str, entity_id: str = "") -> str:
    """The source FAMILY a facet came from — per-facet provenance first, entity prefix fallback."""
    doc = (source_document_id or "").lower()
    for pat, fam in _DOC_FAMILY_PATTERNS:
        if pat in doc:
            return fam
    return (entity_id or "").split(":", 1)[0] if ":" in (entity_id or "") else ""


def evidence_packet(facet_rows: list[dict], entity_id: str = "") -> dict:
    """CODE-OWNED evidence packet for one person row.

    Returns {types, strength, families, per_key, corroborated_keys, gaps}:
    - types: evidence types present, strongest first;
    - strength: the row's headline — the strongest type present, or 'mixed' when families of
      different rungs coexist;
    - per_key: facet_key → {type, family} for the Evidence Rail;
    - corroborated_keys: claim keys where ≥2 independent families agree on a value;
    - gaps: useful dimensions with no evidence at all (honest missing-field accounting).
    """
    fams_by_keyval: dict[tuple, set] = {}
    per_key: dict[str, dict] = {}
    families: set[str] = set()
    for f in facet_rows or []:
        key = f.get("facet_key") or ""
        fam = facet_family(f.get("document_id") or f.get("source_document_id") or "", entity_id)
        etype = FAMILY_EVIDENCE_TYPE.get(fam, "self_stated")
        if fam:
            families.add(fam)
        val = (f.get("value_norm") or f.get("facet_value_norm")
               or (f.get("display_value") or "").lower())
        fams_by_keyval.setdefault((key, val), set()).add(fam)
        cur = per_key.get(key)
        if cur is None or _STRENGTH_RANK.get(etype, 0) > _STRENGTH_RANK.get(cur["type"], 0):
            per_key[key] = {"type": etype, "family": fam}
    corroborated_keys = sorted({k for (k, v), fams in fams_by_keyval.items()
                                if len({f for f in fams if f}) >= 2})
    for k in corroborated_keys:
        per_key[k] = {**per_key.get(k, {}), "type": "corroborated"}
    types_present = sorted({d["type"] for d in per_key.values()},
                           key=lambda t: (-_STRENGTH_RANK.get(t, 0), t))   # rank, then a STABLE tie-break
    if not types_present:
        strength = "gap"
    elif len({_STRENGTH_RANK.get(t, 0) for t in types_present}) > 1:
        strength = "mixed"
    else:
        strength = types_present[0]
    _USEFUL = ("role", "seniority", "function", "company", "metro", "country")
    gaps = [k for k in _USEFUL if k not in per_key]
    return {"types": types_present, "strength": strength,
            "families": sorted(f for f in families if f), "per_key": per_key,
            "corroborated_keys": corroborated_keys, "gaps": gaps}


def evidence_groups(rows: list[dict]) -> list[dict]:
    """Map-level distribution for the coverage panel: how many rows per headline evidence state —
    the spec's candidate groups, counted by code from the packets."""
    order = ("corroborated", "structured", "artifact_backed", "employer_stated",
             "self_stated", "mixed", "gap")
    counts: dict[str, int] = {}
    for r in rows or []:
        s = ((r.get("evidence") or {}).get("strength")) or "gap"
        counts[s] = counts.get(s, 0) + 1
    return [{"state": s, "label": EVIDENCE_TYPE_LABELS.get(s, s), "count": counts[s]}
            for s in order if counts.get(s)]
