"""Single-company DILIGENCE compose prompt + renderer (VERTICAL-owned — Slice-2 depth).

Task D3's diligence route (`apps/api/diligence_route.py`) does NOT go through
`run_react`; it reads ONE company's grounded claims (`ClaimGraphStore.entity_claims`),
groups them BY DIMENSION, and composes a grounded diligence brief in a single,
self-contained LLM call. This module owns the two vertical-specific pieces + the
dimension map:

  * `DIMENSIONS` — the ordered diligence-dimension → predicates map (DATA, Rule 18:
    config, NOT a semantic heuristic). Every SLICE1 + DILIGENCE predicate appears in
    EXACTLY one dimension (a unit test asserts full, disjoint coverage). This is the
    axis the brief is organized on — breadth of dimensions is the priority.
  * `render_entity_state_for_compose` — the DETERMINISTIC renderer that turns one
    company's claims into a dimension-grouped, per-cell-CITED state + the numbered
    citation panel the LLM cites against. Pure (no DB/LLM), so directly unit-testable.
    A dimension with no grounded claim renders as `not_collected` (so the compose can
    honestly report coverage).
  * `TECH_DILIGENCE_COMPOSE_PROMPT` — the self-contained system directive: cover every
    dimension in order, cite EVERY fact with a `[cN]` from the panel, `not_collected`
    for empty dimensions, then a grounded `## Assessment`, `## What would change this`,
    and `## Coverage basis`. No uncited proper noun / number — grounding is the moat.

Rule 18 split: the LLM owns the PROSE / assessment; CODE owns the dimension map, the
citation panel it must cite against, and (in the route) the citation-resolution gate.
"""
from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------- #
# DIMENSIONS — the ordered diligence axis (DATA, Rule 18: config not heuristic).#
#                                                                              #
# Every SLICE1 + DILIGENCE predicate is assigned to EXACTLY ONE dimension      #
# (test_diligence_compose asserts full, disjoint coverage against              #
# active_predicates(include_diligence=True)). Two predicates not named in the  #
# D3 brief's illustrative map are slotted here so coverage stays total:        #
#   - `compared_to` (SLICE1 competitor signal)  → Market & Customers           #
#   - `risk_factor` (DILIGENCE stated risk)     → Moat & Differentiation       #
# `stage` is deliberately NOT a predicate/dimension — it is an INFERRED label  #
# the compose labels as such, never asserted as a fact.                        #
# --------------------------------------------------------------------------- #
DIMENSIONS: list[tuple[str, list[str]]] = [
    ("Team", ["has_founder", "headcount"]),
    ("Funding", ["raised_funding", "has_investor"]),
    ("Traction", ["traction_metric", "revenue"]),
    ("Product & Technology", ["offers_product", "uses_technology"]),
    ("Market & Customers",
     ["operates_in_category", "targets_customer", "ideal_customer_profile",
      "customer_evidence", "compared_to"]),
    ("Moat & Differentiation",
     ["claims_differentiator", "moat_claim", "business_model", "go_to_market", "risk_factor"]),
    ("Company profile", ["company_location", "company_status"]),
]

# Ordered dimension names + the flat predicate→dimension index (deterministic).
DIMENSION_NAMES: list[str] = [name for name, _ in DIMENSIONS]
PREDICATE_TO_DIMENSION: dict[str, str] = {
    pred: name for name, preds in DIMENSIONS for pred in preds
}


# ── The self-contained diligence-compose system directive ──────────────────────────
TECH_DILIGENCE_COMPOSE_PROMPT = """\
You compose a GROUNDED single-company diligence brief. You are given (1) the user's
question, (2) that ONE company's grounded facts GROUPED BY DIMENSION, each fact
carrying a `[citation_id]`, and (3) a numbered CITATION PANEL. Every fact you may use
comes ONLY from that grouped state and panel — this is a grounded product surface, not
a from-memory company profile.

HARD GROUNDING RULES (grounding is the moat — violating these breaks the product):
- CITE EVERY FACT. When you state any fact about the company (a founder, funding figure,
  headcount, investor, product, technology, customer, metric, location, status,
  differentiator, or moat), put the exact `[citation_id]` that carries it inline, e.g.
  `Founded by Jane Roe [c3]`.
- NEVER name a person, investor, product, funding figure, customer, number, or any other
  proper noun / metric that is not in the citation panel. Do NOT add well-known facts from
  memory to look more complete. If it is not in the panel, it does not exist for this brief.
- A dimension (or a cell within it) with NO grounded claim is `not_collected`. Say
  `not_collected` explicitly — NEVER guess, infer a plausible value, or fill it from memory.

STRUCTURE — cover EVERY dimension, IN THIS ORDER, each as its own `##` section:
  Team → Funding → Traction → Product & Technology → Market & Customers →
  Moat & Differentiation → Company profile.
For each dimension, state its cited facts (`[cN]`); if the dimension carries no grounded
claim, write exactly `not_collected` for that section. A compact per-dimension table is
fine — put the `[cN]` in the cell. Breadth across dimensions is the priority: never skip a
dimension, even to say `not_collected`.

Then, in order, these three sections:
- `## Assessment` — a grounded, multi-angle read of the company's strengths and risks,
  drawn STRICTLY from the cited facts. Name explicitly what is THIN or MISSING (e.g. "no
  funding or investor data collected [not_collected]", "no traction metrics"). Do not
  overstate: a thin graph is a thin read. Every claim here still carries its `[cN]`.
- `## What would change this` — the specific, currently-uncollected facts that, if found,
  would most change the read (e.g. "a funding round or lead investor", "revenue / ARR",
  "customer logos"). Frame them as the missing evidence, not as guesses about their values.
- `## Coverage basis` — built from the COVERAGE FACTS the user message provides: list the
  dimensions COVERED and those `not_collected`, and state PLAINLY that this brief reflects
  the grounded facts materialized into the claim graph as of ingest for this one company —
  NOT a complete picture. This honest coverage statement is REQUIRED.

STAGE IS NOT A FACT. If you discuss the company's stage (seed / Series A / growth / …),
label it as an INFERENCE from grounded proxies (batch, company_status, founding signals,
headcount) and cite those proxies — never assert a stage as a collected fact.

General-audience prose — clear and scannable, an analyst brief, not a VC memo.
"""


def _cell_display(claim: dict) -> str:
    """A claim's stated object: the referenced entity id for entity-claims, else the
    stated value (mirrors population_compose)."""
    return (claim.get("object_entity_id") or claim.get("object_value") or "").strip()


def _claim_has_grounding(claim: dict) -> bool:
    """True iff the claim carries an ACTIVE evidence row with a non-empty quote. The
    read already excludes claims with no active evidence; this guards an empty quote."""
    ev = claim.get("evidence") or {}
    return bool((ev.get("quote") or "").strip())


def render_entity_state_for_compose(company_name: str,
                                    claims: list[dict]) -> tuple[str, list[dict]]:
    """Render ONE company's grounded claims GROUPED BY the DIMENSIONS above, plus a
    numbered citation panel, and return `(rendered, citations)`.

    Mirrors `population_compose.render_population_flat` but for a single company and
    organized by dimension. DETERMINISTIC and PURE (no DB/LLM): claims are walked in
    DIMENSION order, then dimension-predicate order, then a stable per-predicate sort
    (object display, then quote) — so citation ids `c1,c2,…` are assigned identically
    regardless of the input list's order. Only claims with ACTIVE evidence (a non-empty
    quote) become cited cells. A dimension with no grounded claim renders as
    `not_collected`. The citation panel rows share population's shape:
    `{citation_id, entity_id, name, predicate, object, document_id, block_id, quote,
    authority_tier}`."""
    # Group grounded claims by predicate (stable-sorted within each predicate).
    by_pred: dict[str, list[dict]] = {}
    for cl in claims or []:
        if not _claim_has_grounding(cl):
            continue
        by_pred.setdefault(cl.get("predicate") or "", []).append(cl)
    for pred in by_pred:
        by_pred[pred].sort(
            key=lambda c: (_cell_display(c), (c.get("evidence") or {}).get("quote") or ""))

    citations: list[dict] = []
    lines: list[str] = [f"## Grounded state for {company_name} (grouped by dimension)"]
    n = 0
    for dim_name, preds in DIMENSIONS:
        lines.append("")
        lines.append(f"### {dim_name}")
        dim_rows: list[str] = []
        for pred in preds:                       # dimension-declared predicate order
            for cl in by_pred.get(pred, []):
                ev = cl.get("evidence") or {}
                eid = cl.get("entity_id") or ""
                obj = _cell_display(cl)
                n += 1
                cid = f"c{n}"
                citations.append({
                    "citation_id": cid,
                    "entity_id": eid,
                    "name": company_name,
                    "predicate": pred,
                    "object": obj,
                    "document_id": ev.get("document_id"),
                    "block_id": ev.get("block_id"),
                    "quote": ev.get("quote"),
                    "authority_tier": ev.get("authority_tier"),
                })
                dim_rows.append(f"- {pred}: {obj} [{cid}]")
        if dim_rows:
            lines.extend(dim_rows)
        else:
            lines.append("- not_collected")

    lines.append("")
    lines.append("## Citation panel")
    lines.append("_Cite these ids. Every fact you state MUST carry one of these `[cN]` "
                 "markers; nothing outside this panel may appear in the brief._")
    for c in citations:
        quote = (c.get("quote") or "").replace("\n", " ").strip()
        if len(quote) > 240:
            quote = quote[:237] + "…"
        lines.append(f"[{c['citation_id']}] {c['name']} — {c['predicate']}: {c['object']}  "
                     f"(quote: \"{quote}\")")
    return "\n".join(lines), citations


def dimension_coverage(claims: list[dict]) -> dict[str, list[str]]:
    """Which DIMENSIONS have ≥1 grounded claim vs none. Pure. Returns
    `{covered:[dimension names…], not_collected:[dimension names…]}` in DIMENSIONS
    order. A dimension is covered iff at least one of its predicates has a grounded
    (active-evidence, non-empty-quote) claim."""
    grounded_preds = {
        (cl.get("predicate") or "")
        for cl in (claims or []) if _claim_has_grounding(cl)
    }
    covered: list[str] = []
    not_collected: list[str] = []
    for dim_name, preds in DIMENSIONS:
        if any(p in grounded_preds for p in preds):
            covered.append(dim_name)
        else:
            not_collected.append(dim_name)
    return {"covered": covered, "not_collected": not_collected}


def render_diligence_coverage_facts(coverage_basis: dict[str, Any]) -> str:
    """Render the structured coverage facts the route assembles into the compact block
    the compose LLM turns into its `## Coverage basis` section. Pure + defensive."""
    covered = coverage_basis.get("dimensions_covered") or []
    not_collected = coverage_basis.get("dimensions_not_collected") or []
    company = coverage_basis.get("company_name") or coverage_basis.get("subject_id") or "?"
    parts = [
        "COVERAGE FACTS (build the required `## Coverage basis` from these):",
        f"- company: {company} (id: {coverage_basis.get('subject_id', '')})",
        f"- dimensions covered ({len(covered)}/{len(DIMENSIONS)}): "
        + (", ".join(covered) if covered else "none"),
        f"- dimensions not_collected ({len(not_collected)}): "
        + (", ".join(not_collected) if not_collected else "none"),
        f"- cited grounded claims: {coverage_basis.get('n_cited_claims', 0)}",
        "- POPULATION STATEMENT (state plainly): "
        + coverage_basis.get("population_statement", ""),
    ]
    return "\n".join(p for p in parts if p)
