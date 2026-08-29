"""Tech scope model — SUBJECT scope, kept orthogonal to the analytical lenses.

`sector` (AI / fintech / biotech / semiconductors / climate…) is the primary narrowing
dimension; secondary dims are entity_type, stage (seed→public), and geography. All four
are stored as facets — the kernel never learns these words. `sector` is resolved per
request against the `sector_profiles` map (see sectors.py), so ONE deployment answers
across sectors. A closed sector ontology would replace the permissive normalize later.
"""
from __future__ import annotations

SCOPE_DIMENSION = "sector"
SCOPE_DIMENSIONS: tuple[str, ...] = ("sector", "entity_type", "stage", "geography")


def normalize_sector(value: str) -> str:
    return value.strip().lower().replace(" ", "_")


def validate_scope(scope: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in SCOPE_DIMENSIONS:
        v = scope.get(key)
        if v and v.strip():
            out[key] = normalize_sector(v) if key == "sector" else v.strip().lower()
    return out
