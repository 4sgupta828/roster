"""CROSSVIEWS visualizer agent — Task CV2.

A cheap, GOAL-FIRST, server-validated conversation that helps a user design a grounded
CROSSVIEWS table (CV1 built the data layer). ONE turn = `crossview_turn(...)`:

  1. COVERAGE (server, grounded): compute the ALLOWED columns for `row_kind` from the
     REAL claim graph — `predicate_coverage` (rows_covered>0) INTERSECTED with the
     active predicate registry (`active_predicate_names`) and grouped by `DIMENSIONS`
     for a company; the fixed CV1 edge/aggregate column set for person/category. A
     column is "allowed" only when it is a real active predicate/edge with coverage>0.
  2. LLM (structured, ONE cheap call — mirrors `app.py::_map_question_topics`): a
     data-dictionary consultant proposes `chosen_column_ids` ONLY from the allowed
     catalog, routes any requested-but-missing column into `gaps` (never invents a
     column), and — GOAL-FIRST — returns `ready=false` + an asking `reply` when the
     goal is empty/vague.
  3. SERVER RE-VALIDATION GATE (code, the grounding guarantee — Rule 18): intersect the
     LLM's `chosen_column_ids` with the allowed set, DROP anything not present or with
     coverage 0 (never trust the LLM here), clamp the count, and map each survivor to a
     TYPED column for `build_grid`. The LLM proposes meaning; code owns the coverage
     gate, so the agent can NEVER emit a fabricated / empty / uncovered column.

Fail-safe: no llm / any error → a DETERMINISTIC proposal of the top-N best-covered
allowed columns (`ready=false`, an explaining reply) — never a fabricated column.
"""
from __future__ import annotations

import re
from typing import Any

from api.crossviews import (
    CATEGORY_COLUMNS,
    PERSON_COLUMNS,
    VALID_ROW_KINDS,
    PREDICATE_TO_DIMENSION,
)

# Bound the proposed table so a runaway LLM (or a huge registry) can't blow up the grid.
MAX_COLUMNS = 8
# Fail-safe / cold-start proposal size (top-covered allowed columns).
FAILSAFE_TOP_N = 5


def _humanize(pred: str) -> str:
    """A short human label for a predicate id (structural string op, Rule 18: not a
    semantic decision) — `operates_in_category` → `Operates in category`."""
    return (pred or "").replace("_", " ").strip().capitalize() or (pred or "")


def _public_columns(cols: list[dict]) -> list[dict]:
    """The FE-facing catalog view — exactly `{id,label,dimension,coverage,object_kind}`
    (drops the internal `col_kind` build_grid discriminator)."""
    return [{"id": c["id"], "label": c["label"], "dimension": c["dimension"],
             "coverage": c["coverage"], "object_kind": c["object_kind"]} for c in cols]


async def build_column_catalog(store, *, row_kind: str, tenant_id: str = "demo",
                               category_norm: str | None = None) -> list[dict]:
    """The GROUNDED allowed-column catalog for `row_kind`. Every entry is a real column
    with coverage>0; each carries the internal `col_kind` (`predicate`|`edge`|`aggregate`)
    the grid builder needs plus the public catalog fields. Fail-safe: any store error →
    `[]` (no columns) rather than a raise or a fabricated column."""
    rk = row_kind if row_kind in VALID_ROW_KINDS else "company"
    try:
        if rk == "company":
            return await _company_catalog(store, tenant_id, category_norm)
        if rk == "person":
            return await _fixed_catalog(store, "person", tenant_id)
        return await _fixed_catalog(store, "category", tenant_id)
    except Exception:  # noqa: BLE001 — fail-safe: an empty catalog, never a crash
        return []


async def _company_catalog(store, tenant_id: str, category_norm: str | None) -> list[dict]:
    """company columns = active predicates that HAVE data. Coverage from
    `predicate_coverage` (rows_covered>0), INTERSECTED with the active predicate
    registry (`active_predicate_names`) so a retired/unknown predicate can never be
    offered; grouped by `DIMENSIONS`. Each maps to a `kind='predicate'` grid column."""
    cov = await store.predicate_coverage(
        subject_kind="company", category_norm=category_norm, tenant_id=tenant_id)
    active = {m["name"]: m for m in await store.active_predicate_names()}
    out: list[dict] = []
    for c in cov:
        pred = c.get("predicate")
        n = int(c.get("rows_covered") or 0)
        if n <= 0 or pred not in active:
            continue
        out.append({
            "id": pred,
            "label": _humanize(pred),
            "dimension": PREDICATE_TO_DIMENSION.get(pred, "Other"),
            "coverage": n,
            "object_kind": active[pred].get("object_kind", "value"),
            "col_kind": "predicate",     # build_grid company column discriminator
        })
    return out


async def _fixed_catalog(store, row_kind: str, tenant_id: str) -> list[dict]:
    """person/category columns = CV1's FIXED edge/aggregate set. Coverage is the count of
    grounded rows the grid would produce (founders / distinct categories); a set with
    zero grounded rows yields NO allowed columns (coverage 0 is never offered)."""
    if row_kind == "person":
        rows = await store.founder_rows(tenant_id=tenant_id)
        fixed, dimension = PERSON_COLUMNS, "Founders"
    else:
        rows = await store.distinct_categories(tenant_id=tenant_id, min_members=1)
        fixed, dimension = CATEGORY_COLUMNS, "Market"
    n = len(rows)
    if n <= 0:
        return []
    out: list[dict] = []
    for col in fixed:
        out.append({
            "id": col["id"],
            "label": col.get("label", col["id"]),
            "dimension": dimension,
            "coverage": n,
            "object_kind": "entity" if col["kind"] == "edge" else "count",
            "col_kind": col["kind"],     # 'edge' | 'aggregate'
        })
    return out


def _closest_ids(requested: str, allowed: list[dict]) -> list[str]:
    """Deterministic (code, not LLM) best-guess columns for a requested-but-missing
    dimension: allowed ids whose id/label shares a token with `requested`, ranked by
    token overlap then coverage; falls back to the top-covered ids. Top 3."""
    toks = [t for t in re.split(r"\W+", (requested or "").lower()) if t]
    scored: list[tuple[int, int, str]] = []
    for a in allowed:
        hay = f"{a['id']} {a['label']}".lower()
        score = sum(1 for t in toks if t in hay)
        if score:
            scored.append((score, int(a["coverage"]), a["id"]))
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    ids = [s[2] for s in scored[:3]]
    if not ids:
        ids = [a["id"] for a in sorted(
            allowed, key=lambda a: (-int(a["coverage"]), a["id"]))[:3]]
    return ids


def _spec_from_columns(row_kind: str, columns: list[dict],
                       category_norm: str | None) -> dict:
    """Build a CrossviewSpec-dict from the VALIDATED columns (each carrying its
    `col_kind`) + the category filter. Columns are typed for `build_grid`."""
    filters: dict = {}
    if category_norm:
        filters["category_norm"] = category_norm
    return {
        "row_kind": row_kind,
        "columns": [{"id": c["id"], "kind": c["col_kind"], "label": c["label"]}
                    for c in columns],
        "filters": filters,
    }


def _failsafe(row_kind: str, allowed: list[dict], category_norm: str | None,
              reply: str) -> dict:
    """Deterministic no-LLM proposal: the top-N best-covered allowed columns, ready=false.
    NEVER a fabricated column — if nothing is covered, an honest empty proposal."""
    top = sorted(allowed, key=lambda a: (-int(a["coverage"]), a["id"]))[:FAILSAFE_TOP_N]
    return {
        "reply": reply,
        "proposed_spec": _spec_from_columns(row_kind, top, category_norm),
        "columns": _public_columns(allowed),
        "gaps": [],
        "ready": False,
    }


_SYSTEM_PROMPT = """\
You are a data-dictionary consultant helping a user design ONE grounded table view.
You are given the user's GOAL, an optional follow-up MESSAGE, the columns they already
picked, and an ALLOWED COLUMN CATALOG — the ONLY columns that have real grounded data.

Rules:
- Choose `chosen_column_ids` ONLY from the catalog's `id` values, verbatim. NEVER invent
  a column id or pick one that is not in the catalog.
- If the user asks for something the catalog does NOT contain, do NOT force a wrong
  column — record it in `gaps` (with the phrase they used + a short note) and leave it
  out of `chosen_column_ids`.
- GOAL-FIRST: if the GOAL is empty, vague, or just a greeting, set `ready=false` and make
  `reply` a short question asking what they want the table to show. Do NOT propose a full
  set of columns yet.
- When the goal is clear, pick the few catalog columns that best serve it, set
  `ready=true`, and make `reply` a short plain-language explanation of the choice.
- Keep `reply` short and plain. Prefer higher-coverage columns when several fit."""


def _render_catalog(columns: list[dict]) -> str:
    if not columns:
        return "(no grounded columns available for this row kind)"
    return "\n".join(
        f"- {c['id']} — {c['label']} [dimension: {c['dimension']}, "
        f"coverage: {c['coverage']} rows]" for c in columns)


async def crossview_turn(*, store, llm, row_kind: str, goal: str,
                         current_columns: list[str] | None, message: str | None,
                         tenant_id: str = "demo",
                         category_norm: str | None = None) -> dict:
    """One turn of the goal-first, server-validated column-design conversation.

    Returns `{reply, proposed_spec, columns, gaps, ready}` where `proposed_spec` is a
    CrossviewSpec-dict whose columns are ALL typed + coverage>0 (the server gate
    guarantees it), `columns` is the full allowed catalog (FE picker), and `gaps` names
    requested-but-missing dimensions with `closest` allowed ids.

    Rule 18: the LLM proposes meaning (which columns serve the goal); CODE owns the
    coverage gate — a column the LLM names that is not a real active predicate/edge with
    coverage>0 is DROPPED here, never emitted."""
    rk = row_kind if row_kind in VALID_ROW_KINDS else "company"
    allowed = await build_column_catalog(
        store, row_kind=rk, tenant_id=tenant_id, category_norm=category_norm)
    allowed_by_id = {a["id"]: a for a in allowed}

    # ---- Fail-safe: no llm → deterministic top-covered proposal (never fabricate) ----
    if llm is None:
        return _failsafe(
            rk, allowed, category_norm,
            "Here is a starting set of the best-covered columns. Tell me your goal and "
            "I'll tailor the view.")

    # ---- ONE cheap structured LLM call (goal-first) ----
    try:
        from pydantic import BaseModel

        class _Gap(BaseModel):
            requested: str = ""
            note: str = ""

        class _Plan(BaseModel):
            reply: str = ""
            chosen_column_ids: list[str] = []
            gaps: list[_Gap] = []
            ready: bool = False

        user_parts = [f"GOAL: {(goal or '').strip() or '(empty)'}"]
        if message and message.strip():
            user_parts.append(f"MESSAGE: {message.strip()}")
        if current_columns:
            user_parts.append("CURRENT COLUMNS: " + ", ".join(current_columns))
        user_parts.append(f"ROW KIND: {rk}")
        user_parts.append("ALLOWED COLUMN CATALOG:\n" + _render_catalog(allowed))

        comp = await llm.complete(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n\n".join(user_parts)[:6000]}],
            response_format=_Plan, max_tokens=400)
        plan = comp.parsed
    except Exception:  # noqa: BLE001 — provider/replay error → deterministic fail-safe
        return _failsafe(
            rk, allowed, category_norm,
            "I couldn't reach the design model just now — here is a starting set of the "
            "best-covered columns.")

    # ---- SERVER RE-VALIDATION GATE (the grounding guarantee, code-owned) ----
    chosen: list[dict] = []
    seen: set[str] = set()
    for cid in (getattr(plan, "chosen_column_ids", None) or []):
        col = allowed_by_id.get(cid)
        if col is None or int(col["coverage"]) <= 0 or cid in seen:
            continue                       # DROP: not a real covered column
        seen.add(cid)
        chosen.append(col)
        if len(chosen) >= MAX_COLUMNS:
            break                          # clamp

    # ---- Gaps: requested-but-missing dimensions (never a spec column) ----
    gaps: list[dict] = []
    for g in (getattr(plan, "gaps", None) or []):
        req = getattr(g, "requested", "") or ""
        if not req.strip():
            continue
        gaps.append({
            "requested": req.strip(),
            "reason": (getattr(g, "note", "") or "no grounded column covers this yet"),
            "closest": _closest_ids(req, allowed),
        })

    ready = bool(getattr(plan, "ready", False)) and bool(chosen)
    reply = (getattr(plan, "reply", "") or "").strip() or (
        "Tell me what you'd like the table to show and I'll pick grounded columns.")

    return {
        "reply": reply,
        "proposed_spec": _spec_from_columns(rk, chosen, category_norm),
        "columns": _public_columns(allowed),
        "gaps": gaps,
        "ready": ready,
    }
