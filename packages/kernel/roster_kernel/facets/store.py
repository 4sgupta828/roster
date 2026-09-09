"""The facet STORE protocol + the shared must / count semantics (kernel — spec §2.1, §5).

A store hands the evaluator ROWS: `{"id", "kind", "sim", "facets": {key: [values]}, "numeric": {key: number}}`.
`matches_must` and `count_rows` are the single definition of filter and count semantics; the SQL adapter
in the app must agree with them (its conformance test runs both over the same data). An in-memory
reference store ships here for tests and for that parity check."""
from __future__ import annotations

from collections import Counter
from typing import Protocol

from .schema import UNKNOWN, FacetSchema, FacetType


def _vals(row: dict, key: str) -> list[str]:
    v = (row.get("facets") or {}).get(key)
    if v is None:
        return []
    return [str(x) for x in (v if isinstance(v, (list, tuple, set)) else [v]) if str(x) and str(x) != UNKNOWN]


def matches_must(row: dict, must: dict | None, schema: FacetSchema) -> bool:
    """Within a key OR; across keys AND; a row with no value for a must key never matches."""
    for key, want in (must or {}).items():
        k = schema.key(key)
        if k is None:
            return False
        have = _vals(row, key)
        if isinstance(want, dict):                       # numeric range over the raw number
            x = (row.get("numeric") or {}).get(key)
            if x is None:
                return False
            lo, hi = want.get("min"), want.get("max")
            if (lo is not None and float(x) < float(lo)) or (hi is not None and float(x) > float(hi)):
                return False
            continue
        want_l = [str(w) for w in (want or [])]
        if not want_l:
            continue
        if not have:
            return False
        if k.type is FacetType.hierarchical:
            if not any(schema.contains(key, h, w) for h in have for w in want_l):
                return False
        else:
            if not (set(have) & set(want_l)):
                return False
    return True


def count_rows(rows: list[dict], schema: FacetSchema, kind: str, depth: dict | None = None) -> dict:
    """Per navigable key of `kind`: value → number of rows. Multi-valued rows count once per value; a row
    lacking a categorical / ordinal / numeric key counts under `unknown` (never for sets); hierarchical
    values are truncated to `depth[key]` segments when asked. Sets keep the top_n most common values."""
    out: dict = {}
    for k in schema.for_kind(kind):
        if not k.navigable:
            continue
        c: Counter = Counter()
        unknown = 0
        for r in rows:
            vals = _vals(r, k.key)
            if not vals:
                if k.type is not FacetType.set:
                    unknown += 1
                continue
            if k.type is FacetType.hierarchical and depth and depth.get(k.key):
                vals = ["/".join(v.split("/")[: int(depth[k.key])]) for v in vals]
            for v in dict.fromkeys(vals):
                c[v] += 1
        if k.type is FacetType.set:
            counted = dict(c.most_common(k.top_n))
        else:
            counted = dict(c)
        if unknown:
            counted[UNKNOWN] = unknown
        out[k.key] = counted
    return out


def coverage_rows(rows: list[dict], schema: FacetSchema, kind: str) -> dict[str, float]:
    """Per navigable key: the share of rows holding ANY value for it.

    COVERAGE IS NOT COUNTS. `count_rows` deliberately never files a set key under `unknown` (a row with
    no tags is not a row whose tags are unknown, and an unknown chip on a tag rail is noise), so any
    coverage read OFF those counts measures 1.0 for every set key — and the guard that decides whether a
    compiled `must` can be a promise could never fire for one. Set keys are exactly the ones a query
    decoder turns into musts (place, company, skill), so coverage gets its own reading here: it counts
    presence, which is right for sets and non-sets alike, and it changes no displayed count."""
    total = len(rows)
    out: dict[str, float] = {}
    for k in schema.for_kind(kind):
        if not k.navigable:
            continue
        if not total:
            out[k.key] = 0.0
            continue
        have = sum(1 for r in rows if _vals(r, k.key))
        out[k.key] = have / total
    return out


class FacetStore(Protocol):
    async def enumerate(self, kind: str, must: dict, *, cap: int = 400) -> list[dict]: ...
    async def semantic(self, kind: str, text: str, must: dict, *, cap: int = 400) -> list[dict]: ...
    async def counts(self, kind: str, must: dict, schema: FacetSchema, *, depth: dict | None = None) -> dict: ...
    async def coverage(self, kind: str, must: dict, schema: FacetSchema) -> dict[str, float]: ...
    async def noise_floor(self, kind: str, text: str) -> float | None: ...


class InMemoryFacetStore:
    """Reference store over a list of rows (tests, parity checks). `sim` on a row stands in for the
    semantic similarity a vector index would compute."""

    def __init__(self, rows: list[dict], schema: FacetSchema | None = None):
        self._rows = [dict(r) for r in rows]
        self._schema = schema

    def _filtered(self, kind: str, must: dict) -> list[dict]:
        rows = [dict(r) for r in self._rows if r.get("kind") == kind]
        if self._schema is not None:
            rows = [r for r in rows if matches_must(r, must, self._schema)]
        return rows

    async def enumerate(self, kind: str, must: dict, *, cap: int = 400) -> list[dict]:
        return self._filtered(kind, must)[:cap]

    async def semantic(self, kind: str, text: str, must: dict, *, cap: int = 400) -> list[dict]:
        rows = self._filtered(kind, must)
        rows.sort(key=lambda r: -float(r.get("sim") or 0.0))
        return rows[:cap]

    async def counts(self, kind: str, must: dict, schema: FacetSchema, *, depth: dict | None = None) -> dict:
        rows = [r for r in self._rows if r.get("kind") == kind and matches_must(r, must, schema)]
        return count_rows(rows, schema, kind, depth=depth)

    async def coverage(self, kind: str, must: dict, schema: FacetSchema) -> dict[str, float]:
        rows = [r for r in self._rows if r.get("kind") == kind and matches_must(r, must, schema)]
        return coverage_rows(rows, schema, kind)

    async def noise_floor(self, kind: str, text: str) -> float | None:
        return None
