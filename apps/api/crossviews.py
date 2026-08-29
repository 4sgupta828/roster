"""CROSSVIEWS grid builder — Task CV1 backend data layer.

CROSSVIEWS is a grounded, dynamic TABLE that is a materialized projection of the
claim graph: ROWS are startups | founders | domains, COLUMNS are dimensions, and
each CELL is the grounded value(s) + citation(s), or `not_collected` when the graph
holds no grounded claim (NEVER a fabricated value). This module owns the SPEC and the
async grid BUILDER only — the visualizer agent, HTTP routes, and the FE are later
tasks; nothing here is wired to a route.

Grounding is the moat: every NON-EMPTY predicate/edge cell carries at least one
citation `{quote, document_id, block_id, authority_tier}` sourced from a real claim's
winning evidence (the underlying store reads INNER-join active evidence, so a cell can
only be non-empty when a real span backs it). AGGREGATE cells (counts) may be
`collected` without a single quote, but the grid records their derivation basis in
`meta.notes` so they stay verifiable. An empty cell is `{"collected": false}`.

The builder is VOCABULARY-NEUTRAL at the row/cell level: it pivots whatever predicate
columns the spec names over the store's grounded reads
(`population_claims`/`founder_rows`/`distinct_categories`+`category_member_companies`);
the tech-specific dimension→predicate grouping (`DIMENSIONS`) is re-exported here for
callers that group columns, but the builder itself never depends on it.

Fail-safe: a bad or empty spec, an unknown row_kind, or an empty result yields an
EMPTY grid with a `meta.notes` explanation — it never raises and never fabricates rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Re-export the tech dimension map for callers that GROUP columns by dimension. This is
# app-level wiring (app may depend on the vertical — the allowed direction); the builder
# below does NOT use it, so crossviews stays importable even if the vertical is absent.
try:  # pragma: no cover - import guard
    from roster_vertical.diligence_compose import (  # noqa: F401
        DIMENSIONS, PREDICATE_TO_DIMENSION,
    )
except Exception:  # noqa: BLE001  pragma: no cover
    DIMENSIONS = []          # type: ignore[assignment]
    PREDICATE_TO_DIMENSION = {}   # type: ignore[assignment]

VALID_ROW_KINDS = ("company", "person", "category")

# When a company grid is sorted by DENSITY ("most data first"), the store caps the
# population by NAME order, so we must fetch a larger pool, rank it by how many of the
# spec's columns are populated, and only THEN slice to the display cap — otherwise the
# cap would keep the alphabetically-first companies (often sparse shells) instead of the
# richest ones. This ceiling bounds that pool fetch.
_DENSITY_POOL_CAP = 1000

# The fixed, documented column sets for the non-pivot row kinds. A person/category spec
# may name a SUBSET of these ids; unknown ids are ignored (noted), and an empty/omitted
# column list falls back to the full set.
PERSON_COLUMNS: list[dict] = [
    {"id": "companies_founded", "kind": "edge", "label": "Companies founded"},
    {"id": "n_companies", "kind": "aggregate", "label": "# Companies"},
]
CATEGORY_COLUMNS: list[dict] = [
    {"id": "member_count", "kind": "aggregate", "label": "Members"},
    {"id": "representative_companies", "kind": "aggregate",
     "label": "Representative companies"},
]


@dataclass
class CrossviewSpec:
    """A CROSSVIEWS request. `row_kind` picks the row source; `columns` are the
    projected dimensions (`kind` ∈ {'predicate','edge','aggregate'}); `filters` narrows
    the population (`category_norm`, `source_key`, `has_predicate`); `sort` orders rows —
    either by a column (`{"column_id","direction"}`) or by DATA DENSITY, most-populated
    first (`{"by":"density"}`, company grids); `cap` bounds the row count."""

    row_kind: str = ""
    columns: list[dict] = field(default_factory=list)
    filters: dict = field(default_factory=dict)
    sort: dict | None = None
    cap: int | None = None

    @classmethod
    def coerce(cls, spec: "CrossviewSpec | dict | None") -> "CrossviewSpec":
        """Normalize a dataclass / dict / None into a CrossviewSpec, defensively (a
        malformed field degrades to its empty default rather than raising — the builder
        turns an empty/invalid spec into a fail-safe empty grid)."""
        if isinstance(spec, cls):
            return spec
        if not isinstance(spec, dict):
            return cls()
        cols = spec.get("columns")
        columns = [c for c in cols if isinstance(c, dict)] if isinstance(cols, list) else []
        filters = spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
        sort = spec.get("sort") if isinstance(spec.get("sort"), dict) else None
        cap = spec.get("cap")
        cap = cap if isinstance(cap, int) and cap > 0 else None
        rk = spec.get("row_kind")
        return cls(row_kind=rk if isinstance(rk, str) else "",
                   columns=columns, filters=filters, sort=sort, cap=cap)


# --------------------------------------------------------------------------- #
# Pure cell/citation helpers                                                  #
# --------------------------------------------------------------------------- #
def _citation(ev: dict | None) -> dict | None:
    """A grounded citation `{quote, document_id, block_id, authority_tier}` from a
    claim's winning-evidence dict, or None when there is no active span."""
    if not ev or not ev.get("document_id"):
        return None
    return {
        "quote": ev.get("quote"),
        "document_id": ev.get("document_id"),
        "block_id": ev.get("block_id"),
        "authority_tier": ev.get("authority_tier"),
    }


def _not_collected() -> dict:
    return {"collected": False}


def _cell(value: Any, citations: list[dict]) -> dict:
    return {"collected": True, "value": value, "citations": citations}


def _claim_display_value(claim: dict) -> str:
    """The grounded display value for a claim cell: the stated object_value, else the
    normalized object, else the object entity id (all sourced from the claim — never
    fabricated)."""
    return (claim.get("object_value") or claim.get("object_norm")
            or claim.get("object_entity_id") or "")


def _sort_rows(rows: list[dict], sort: dict | None, columns: list[dict]) -> None:
    """In-place stable row sort by a column's cell value; `not_collected` cells sort
    last. Unknown column / bad sort spec is a no-op (rows keep source order)."""
    if not sort:
        return
    col_id = sort.get("column_id")
    if not col_id or all(c["id"] != col_id for c in columns):
        return
    descending = str(sort.get("direction", "asc")).lower() == "desc"

    def key(row: dict):
        cell = row["cells"].get(col_id) or {}
        if not cell.get("collected"):
            return (1, "")            # empties always last, both directions
        v = cell.get("value")
        if isinstance(v, list):
            v = v[0] if v else ""
        return (0, str(v).lower())

    rows.sort(key=key, reverse=descending)
    if descending:
        # keep empties last even when reversed
        rows.sort(key=lambda r: 0 if (r["cells"].get(col_id) or {}).get("collected") else 1)


def _is_density_sort(sort: dict | None) -> bool:
    """True when the spec asks rows ordered by DATA DENSITY (most populated cells first)
    rather than by a column value. Signalled by `sort={"by":"density"}`."""
    return bool(sort) and str(sort.get("by", "")).lower() == "density"


def _row_density(row: dict, col_ids: list[str]) -> int:
    """How many of the given columns are grounded (collected) for this row — the rank key
    for density sort. Ties are broken by name for determinism at the call site."""
    cells = row.get("cells") or {}
    return sum(1 for cid in col_ids if (cells.get(cid) or {}).get("collected"))


def _empty_grid(row_kind: str, columns: list[dict], notes: list[str]) -> dict:
    return {
        "row_kind": row_kind,
        "columns": [{"id": c["id"], "label": c.get("label", c["id"]), "kind": c.get("kind", "")}
                    for c in columns],
        "rows": [],
        "meta": {"row_count": 0, "truncated": False, "notes": notes},
    }


def _resolve_columns(spec_columns: list[dict], allowed: list[dict],
                     notes: list[str]) -> list[dict]:
    """For fixed-column row kinds (person/category): keep the spec's requested columns
    that are in `allowed` (preserving spec order), else fall back to the full allowed
    set. Unknown requested ids are noted, not raised."""
    if not spec_columns:
        return list(allowed)
    by_id = {c["id"]: c for c in allowed}
    picked: list[dict] = []
    for c in spec_columns:
        cid = c.get("id")
        if cid in by_id and all(p["id"] != cid for p in picked):
            picked.append(by_id[cid])
        elif cid is not None:
            notes.append(f"ignored unknown column '{cid}' for row_kind")
    return picked or list(allowed)


# --------------------------------------------------------------------------- #
# The builder                                                                 #
# --------------------------------------------------------------------------- #
async def build_grid(store, spec: "CrossviewSpec | dict | None", *,
                     tenant_id: str = "demo") -> dict:
    """Build a grounded CROSSVIEWS grid from the claim graph.

    Returns:
      `{"row_kind", "columns":[{id,label,kind}],
        "rows":[{"id","name","cells":{col_id:{"collected":bool,"value":str|list,
                 "citations":[{quote,document_id,block_id,authority_tier}]}}}],
        "meta":{row_count, truncated, notes}}`.

    Fail-safe: a bad/empty spec, an unknown row_kind, or an empty result yields an empty
    grid + `meta.notes` — never a raise, never a fabricated row/cell."""
    sp = CrossviewSpec.coerce(spec)
    notes: list[str] = []
    if sp.row_kind not in VALID_ROW_KINDS:
        notes.append(f"unknown row_kind '{sp.row_kind}'; expected one of {VALID_ROW_KINDS}")
        return _empty_grid(sp.row_kind or "", [], notes)
    try:
        if sp.row_kind == "company":
            return await _build_company_grid(store, sp, tenant_id, notes)
        if sp.row_kind == "person":
            return await _build_person_grid(store, sp, tenant_id, notes)
        return await _build_category_grid(store, sp, tenant_id, notes)
    except Exception as exc:  # noqa: BLE001 — fail-safe: never crash a grid build
        notes.append(f"grid build failed: {type(exc).__name__}: {exc}")
        return _empty_grid(sp.row_kind, [], notes)


async def _build_company_grid(store, sp: CrossviewSpec, tenant_id: str,
                              notes: list[str]) -> dict:
    """company rows = a pivot of `population_claims` restricted to the spec's PREDICATE
    columns. Each cell collects that predicate's claim(s) for the company → a single
    value (1 claim) or a LIST (multi), each contributing its winning-evidence citation;
    a predicate with no claim → not_collected."""
    pred_cols = [c for c in sp.columns if c.get("kind") == "predicate" and c.get("id")]
    if not pred_cols:
        notes.append("company grid needs >=1 column of kind 'predicate'")
        return _empty_grid("company", [], notes)
    col_ids = [c["id"] for c in pred_cols]
    category_norm = sp.filters.get("category_norm")
    display_cap = sp.cap or 400
    density_mode = _is_density_sort(sp.sort)
    # Density sort ranks the WHOLE population by how many columns each company fills, so
    # fetch a larger pool before capping (the store caps by name order — capping first
    # would keep the alphabetically-first, often-sparse companies). Column/name sort keeps
    # the store's name-ordered cap unchanged.
    fetch_cap = max(display_cap, _DENSITY_POOL_CAP) if density_mode else display_cap
    pop = await store.population_claims(
        tenant_id=tenant_id,
        category_norms=[category_norm] if category_norm else None,
        company_cap=fetch_cap,
        claims_per_company_cap=200,       # raise the per-company cap for a wide grid
        predicates=col_ids,
    )
    meta_src = pop.get("meta", {})
    rows: list[dict] = []
    for comp in pop.get("companies", []):
        by_pred: dict[str, list[dict]] = {}
        for cl in comp.get("claims", []):
            by_pred.setdefault(cl.get("predicate"), []).append(cl)
        cells: dict[str, dict] = {}
        for cid in col_ids:
            claims = by_pred.get(cid) or []
            if not claims:
                cells[cid] = _not_collected()
                continue
            values: list[str] = []
            citations: list[dict] = []
            for cl in claims:
                values.append(_claim_display_value(cl))
                cit = _citation(cl.get("evidence"))
                if cit:
                    citations.append(cit)
            value: Any = values[0] if len(values) == 1 else values
            cells[cid] = _cell(value, citations)
        rows.append({"id": comp.get("entity_id"), "name": comp.get("name"), "cells": cells})

    density_truncated = False
    if density_mode:
        # rank densest-first over the SELECTED columns; break ties by name for a stable,
        # deterministic order (asc), then slice the richest `display_cap` off the pool.
        rows.sort(key=lambda r: (-_row_density(r, col_ids), (r.get("name") or "").lower()))
        density_truncated = len(rows) > display_cap
        rows = rows[:display_cap]
        notes.append("rows ordered by data density (most populated columns first)")
        if density_truncated:
            notes.append(f"showing the {display_cap} densest of the matched population")
    else:
        _sort_rows(rows, sp.sort, pred_cols)

    pool_truncated = bool(meta_src.get("companies_truncated"))
    truncated = bool(pool_truncated or meta_src.get("claims_truncated") or density_truncated)
    if pool_truncated:
        # in density mode this is the _DENSITY_POOL_CAP ceiling; otherwise the display cap
        notes.append(f"company population truncated at cap {meta_src.get('company_cap')}")
    if meta_src.get("claims_truncated"):
        notes.append("some companies had claims clipped at the per-company cap")
    if not rows:
        notes.append("no grounded companies matched the spec")
    return {
        "row_kind": "company",
        "columns": [{"id": c["id"], "label": c.get("label", c["id"]), "kind": "predicate"}
                    for c in pred_cols],
        "rows": rows,
        "meta": {"row_count": len(rows), "truncated": truncated, "notes": notes},
    }


async def _build_person_grid(store, sp: CrossviewSpec, tenant_id: str,
                             notes: list[str]) -> dict:
    """person rows = founders (the REVERSE has_founder edge). Supported columns:
    `companies_founded` (edge — list of company names, each cited by its founder-edge
    span) and `n_companies` (aggregate — the count, basis noted)."""
    columns = _resolve_columns(sp.columns, PERSON_COLUMNS, notes)
    founder_predicate = sp.filters.get("founder_predicate", "has_founder")
    person_cap = sp.cap or 400
    founders = await store.founder_rows(
        tenant_id=tenant_id, founder_predicate=founder_predicate, cap=person_cap + 1)
    truncated = len(founders) > person_cap
    founders = founders[:person_cap]

    has_aggregate = any(c["id"] == "n_companies" for c in columns)
    if has_aggregate:
        notes.append("aggregate 'n_companies' = count of grounded founder edges per person")

    rows: list[dict] = []
    for f in founders:
        companies = f.get("companies", [])
        cells: dict[str, dict] = {}
        for col in columns:
            cid = col["id"]
            if cid == "companies_founded":
                if not companies:
                    cells[cid] = _not_collected()
                    continue
                names = [c.get("company_name") for c in companies]
                citations = [c for c in (_citation(co) for co in companies) if c]
                cells[cid] = _cell(names, citations)
            elif cid == "n_companies":
                cells[cid] = _cell(str(len(companies)), [])
        rows.append({"id": f.get("person_id"), "name": f.get("name"), "cells": cells})

    if truncated:
        notes.append(f"founder rows truncated at cap {person_cap}")
    if not rows:
        notes.append("no grounded founders found")
    return {
        "row_kind": "person",
        "columns": [{"id": c["id"], "label": c.get("label", c["id"]), "kind": c["kind"]}
                    for c in columns],
        "rows": rows,
        "meta": {"row_count": len(rows), "truncated": truncated, "notes": notes},
    }


async def _build_category_grid(store, sp: CrossviewSpec, tenant_id: str,
                               notes: list[str]) -> dict:
    """category rows = distinct grounded market categories. Columns: `member_count`
    (aggregate — the distinct grounded member count) and `representative_companies`
    (aggregate/edge — exemplar member companies, each cited by its placement span)."""
    columns = _resolve_columns(sp.columns, CATEGORY_COLUMNS, notes)
    cats = await store.distinct_categories(tenant_id=tenant_id, min_members=1)
    cap = sp.cap or len(cats)
    truncated = len(cats) > cap
    cats = cats[:cap]

    if any(c["id"] == "member_count" for c in columns):
        notes.append("aggregate 'member_count' = distinct grounded members from "
                     "distinct_categories")
    want_reps = any(c["id"] == "representative_companies" for c in columns)

    rows: list[dict] = []
    for cat in cats:
        norm = cat.get("object_norm")
        cells: dict[str, dict] = {}
        reps = None
        if want_reps:
            reps = await store.category_member_companies(
                tenant_id=tenant_id, category_norm=norm, cap=8)
        for col in columns:
            cid = col["id"]
            if cid == "member_count":
                cells[cid] = _cell(str(cat.get("members", 0)), [])
            elif cid == "representative_companies":
                members = reps or []
                if not members:
                    cells[cid] = _not_collected()
                    continue
                # dedup company names (a company may hold >1 placement claim to the norm)
                seen: set = set()
                names: list[str] = []
                citations: list[dict] = []
                for m in members:
                    nm = m.get("name")
                    if nm in seen:
                        continue
                    seen.add(nm)
                    names.append(nm)
                    cit = _citation(m)
                    if cit:
                        citations.append(cit)
                cells[cid] = _cell(names, citations)
        rows.append({"id": norm, "name": cat.get("name"), "cells": cells})

    if truncated:
        notes.append(f"categories truncated at cap {cap}")
    if not rows:
        notes.append("no grounded categories found")
    return {
        "row_kind": "category",
        "columns": [{"id": c["id"], "label": c.get("label", c["id"]), "kind": c["kind"]}
                    for c in columns],
        "rows": rows,
        "meta": {"row_count": len(rows), "truncated": truncated, "notes": notes},
    }
