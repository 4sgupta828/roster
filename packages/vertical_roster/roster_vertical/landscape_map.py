"""Pure grounded market-map builder (VERTICAL-owned — it knows predicate→axis
semantics).

The claim-graph store aggregates the population (`ClaimGraphStore.population_claims`
returns companies × their grounded, active-evidence claims). This module SHAPES that
population into the compact, CITED structure Task 4's compose consumes — WITHOUT any
DB or LLM, so it is directly unit-testable and deterministic (snapshot-safe).

Axis semantics (from `claim_predicates.SLICE1_PREDICATES`):
  * `operates_in_category` is the CATEGORY axis — it PLACES a company on the map. A
    company is grouped under EACH category it holds a grounded membership claim for.
  * the remaining predicates (offers_product / targets_customer / uses_technology /
    claims_differentiator / compared_to) are the per-company CELLS.

Grounding invariant (mirrors the store's exclusion discipline): every non-empty cell
carries a `citation_id` that RESOLVES in `citations` to a `document_id + block_id +
quote`. A claim with no evidence is dropped — there is NO uncited cell. A company
with no claim for a predicate simply omits that cell (compose renders it
`not_collected`). Citation ids (`c1, c2, …`) are assigned in a stable traversal
order, so the whole structure is deterministic.
"""
from __future__ import annotations

from typing import Any, Sequence

CATEGORY_PREDICATE = "operates_in_category"


def _display_object(claim: dict) -> str:
    """The object as displayed: the referenced entity id for entity-claims, else the
    stated value. Falls back to the normalized object so an entity-claim that only
    carries `object_norm` still renders something stable."""
    if claim.get("object_kind") == "entity":
        return claim.get("object_entity_id") or claim.get("object_norm") or ""
    return claim.get("object_value") or claim.get("object_norm") or ""


def _has_evidence(claim: dict) -> bool:
    """A claim is groundable iff it carries an active evidence row with a document_id
    (the pure mirror of the store's `ev.document_id IS NOT NULL` exclusion)."""
    ev = claim.get("evidence")
    return bool(ev and ev.get("document_id"))


def build_market_map(companies: list[dict], *, predicates: Sequence[dict],
                     category_predicate: str = CATEGORY_PREDICATE) -> dict:
    """Shape grounded population rows (from `population_claims`) into a compact CITED
    market map. See module docstring for the invariants. Returns:

        {
          "categories": [ {"name","object_norm","companies":[
               {"entity_id","name","cells": {predicate: [{"value"|"entity_id",
                                                          "citation_id"}]}} ]} ],
          "citations":  [ {"citation_id","entity_id","predicate","object",
                           "document_id","block_id","quote","authority_tier"} ],
          "stats":      {"n_companies","n_categories","n_cited_claims"}
        }

    Deterministic: companies are traversed name-then-id sorted, cell predicates in the
    given `predicates` order, and cell values by their grounded object — so citation
    ids (`c1, c2, …`) and every ordering are stable across runs on the same input.
    """
    # Cell predicates = the slice predicates in declared order, minus the category axis.
    cell_predicates = [p["name"] for p in predicates if p.get("name") != category_predicate]

    companies_sorted = sorted(
        companies, key=lambda c: (c.get("name") or "", c.get("entity_id") or ""))

    citations: list[dict] = []
    # object_norm -> {"name", "object_norm", "companies":[]}
    categories: dict[str, dict] = {}
    placed_ids: set[str] = set()
    counter = 0

    def _next_citation_id() -> str:
        nonlocal counter
        counter += 1
        return f"c{counter}"

    for company in companies_sorted:
        entity_id = company.get("entity_id") or ""
        name = company.get("name") or ""
        claims = company.get("claims") or []

        # Grounded category memberships place the company. Require evidence so a
        # placement is never ungrounded; skip companies with no grounded membership.
        memberships: dict[str, str] = {}  # object_norm -> display name
        for cl in claims:
            if cl.get("predicate") != category_predicate or not _has_evidence(cl):
                continue
            onorm = cl.get("object_norm") or _display_object(cl)
            if not onorm:
                continue
            # Prefer a stated value as the category label; else the normalized object.
            label = cl.get("object_value") or onorm
            memberships.setdefault(onorm, label)
        if not memberships:
            continue

        # Build the company's cells + their citations ONCE (shared across categories).
        cells: dict[str, list[dict]] = {}
        for pred in cell_predicates:
            pred_claims = [cl for cl in claims
                           if cl.get("predicate") == pred and _has_evidence(cl)]
            # Deterministic order of a multi-valued cell.
            pred_claims.sort(key=lambda cl: (
                cl.get("object_norm") or "", _display_object(cl),
                (cl.get("evidence") or {}).get("document_id") or "",
                (cl.get("evidence") or {}).get("block_id") or ""))
            entries: list[dict] = []
            for cl in pred_claims:
                display = _display_object(cl)
                if not display:
                    continue  # nothing to render → not a cell
                cid = _next_citation_id()
                ev = cl.get("evidence") or {}
                citations.append({
                    "citation_id": cid,
                    "entity_id": entity_id,
                    "predicate": pred,
                    "object": display,
                    "document_id": ev.get("document_id"),
                    "block_id": ev.get("block_id"),
                    "quote": ev.get("quote"),
                    "authority_tier": ev.get("authority_tier"),
                })
                if cl.get("object_kind") == "entity":
                    entries.append({"entity_id": display, "citation_id": cid})
                else:
                    entries.append({"value": display, "citation_id": cid})
            if entries:
                cells[pred] = entries

        company_struct = {"entity_id": entity_id, "name": name, "cells": cells}
        placed_ids.add(entity_id)

        # Group under EACH category it is a member of (companies stay in sorted order).
        for onorm, label in memberships.items():
            cat = categories.get(onorm)
            if cat is None:
                cat = {"name": label, "object_norm": onorm, "companies": []}
                categories[onorm] = cat
            cat["companies"].append(company_struct)

    ordered_categories = sorted(
        categories.values(), key=lambda c: (c["name"], c["object_norm"]))

    return {
        "categories": ordered_categories,
        "citations": citations,
        "stats": {
            "n_companies": len(placed_ids),
            "n_categories": len(ordered_categories),
            "n_cited_claims": len(citations),
        },
    }
