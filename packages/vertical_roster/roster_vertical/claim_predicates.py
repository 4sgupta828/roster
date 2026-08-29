"""Slice-1 competitive-landscape predicate registry (VERTICAL-owned vocabulary).

The claim-graph STORE (`apps/api/claimgraph.py`) is domain-neutral: entity kinds,
predicate names, and their object/temporal policies arrive as DATA. This module is
that data for the tech vertical — the ~6 load-bearing predicates that make a real
competitive-landscape answer groundable (panel call: ~6, not 3, not ~10).

Keeping the vocabulary here (not in the store) honors the kernel/vertical split:
the store consumes `SLICE1_PREDICATES` in `ensure_schema()` and seeds them into
`rs_predicate` (idempotent, `ON CONFLICT DO NOTHING` so a later status change is
never clobbered). The LLM owns which predicate a span asserts (Rule 18); code only
validates the emitted name against this closed registry.

Each entry:
  name            — the predicate key (must be stable; it is a claim's `predicate`).
  status          — 'active' (production extraction emits only active predicates).
  object_kind     — 'value' (a text/number literal) | 'entity' (a ref to another
                    resolved entity) | 'either'.
  object_entity_kind — (entity predicates only) the rs_entity.kind to MINT for the
                    object when resolving it (company|person|investor|category|…).
                    This is CONFIG that drives the extraction job's object router
                    (structural, per-predicate) — NOT a semantic heuristic (Rule 18):
                    the LLM still owns which predicate a span asserts; this field only
                    says what kind of node that predicate's object is.
  cardinality     — 'single' (one current value per subject) | 'multi' (a set).
  temporal_policy — 'static' | 'point' | 'interval'. Slice-1 landscape facts are
                    treated as static (no valid_time churn); Slice-2+ widen this.
  description     — human/LLM-facing gloss of what the predicate asserts.
"""
from __future__ import annotations

SLICE1_PREDICATES: list[dict] = [
    {
        "name": "operates_in_category",
        "status": "active",
        "object_kind": "entity",
        "object_entity_kind": "category",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company operates in / competes within the "
                       "given market category (object is a category entity). This "
                       "is the claim that PLACES a company on the landscape map.",
    },
    {
        "name": "offers_product",
        "mention_kind": "product",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company offers the named product or product "
                       "line (object is the product name as stated).",
    },
    {
        "name": "targets_customer",
        "mention_kind": "market",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company targets the named customer segment / "
                       "buyer / market (e.g. 'enterprise', 'SMB', 'developers').",
    },
    {
        "name": "uses_technology",
        "mention_kind": "technology",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company builds on / uses the named technology, "
                       "approach, or architecture (object is the technology name).",
    },
    {
        "name": "claims_differentiator",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company asserts the named differentiator / "
                       "positioning claim about itself (object is the claim text).",
    },
    {
        "name": "compared_to",
        "status": "active",
        "object_kind": "entity",
        "object_entity_kind": "company",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company is compared to / positioned against "
                       "another company (object is a company entity) — the direct "
                       "competitor / substitute signal.",
    },
]


# --------------------------------------------------------------------------- #
# Slice-2 DILIGENCE vocabulary (breadth of dimensions for single-company       #
# grounded diligence). Same schema as SLICE1_PREDICATES; entity predicates     #
# carry `object_entity_kind` so the extraction job's object router is fully     #
# data-driven (Rule 18: config, not a semantic heuristic).                      #
#                                                                              #
# NOTE: `stage` (seed/Series A/growth/…) is deliberately NOT a predicate here.  #
# It is an INFERRED label the decision layer derives from grounded proxies      #
# (batch / company_status / founded date / headcount), never extracted as a     #
# standalone fact — so a stage read is always traceable to grounded evidence.   #
# --------------------------------------------------------------------------- #
DILIGENCE_PREDICATES: list[dict] = [
    {
        "name": "has_founder",
        "status": "active",
        "object_kind": "entity",
        "object_entity_kind": "person",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "A named person is a founder / co-founder of the subject "
                       "company (object is that person).",
    },
    {
        "name": "has_investor",
        "status": "active",
        "object_kind": "entity",
        "object_entity_kind": "investor",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "A named investor / fund / accelerator has invested in or "
                       "backs the subject company (object is that investor).",
    },
    {
        "name": "raised_funding",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "point",
        "description": "The subject company raised a round of funding as stated "
                       "(object is the round/amount as written, e.g. 'Series A: "
                       "$10M'; the date, if any, is captured in valid_from later).",
    },
    {
        "name": "headcount",
        "status": "active",
        "object_kind": "value",
        "cardinality": "single",
        "temporal_policy": "point",
        "description": "The subject company's team size / employee count as stated "
                       "(object is the number/phrase as written).",
    },
    {
        "name": "company_location",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company is located / headquartered in the named "
                       "place (object is the location as stated).",
    },
    {
        "name": "company_status",
        "status": "active",
        "object_kind": "value",
        "cardinality": "single",
        "temporal_policy": "point",
        "description": "The subject company's current operating status as stated "
                       "(e.g. 'active', 'acquired', 'shut down', 'public').",
    },
    {
        "name": "business_model",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "How the subject company makes money / its business model as "
                       "stated (e.g. 'SaaS subscription', 'usage-based', 'marketplace').",
    },
    {
        "name": "moat_claim",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "A defensibility / moat claim the subject company asserts about "
                       "itself (e.g. proprietary data, network effects, switching costs).",
    },
    {
        "name": "traction_metric",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "point",
        "description": "A stated traction / growth metric for the subject company "
                       "(e.g. revenue, ARR, users, growth rate) as written.",
    },
    {
        "name": "customer_evidence",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "Named customers, logos, or concrete customer proof points of "
                       "the subject company (object is the customer/evidence as stated).",
    },
    {
        "name": "risk_factor",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "A stated risk / concern / weakness about the subject company "
                       "(e.g. regulatory, competitive, execution, concentration risk).",
    },
    {
        "name": "go_to_market",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company's go-to-market / distribution / sales "
                       "motion as stated (e.g. 'bottoms-up PLG', 'direct enterprise sales').",
    },
    {
        "name": "revenue",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "point",
        "description": "A stated REVENUE or ARR figure for the subject company, with its period "
                       "if given (e.g. '$12M ARR', '$3M revenue FY24'). The single hardest diligence "
                       "number — keep it distinct from generic traction, exactly as written.",
    },
    {
        "name": "ideal_customer_profile",
        "mention_kind": "market",
        "status": "active",
        "object_kind": "value",
        "cardinality": "multi",
        "temporal_policy": "static",
        "description": "The subject company's IDEAL CUSTOMER PROFILE (ICP) as stated — the specific "
                       "buyer/persona/segment it is built for (e.g. 'mid-market fintech CISOs', "
                       "'AI infra teams'), distinct from the broad market it targets.",
    },
]


def active_predicates(include_diligence: bool = False) -> list[dict]:
    """The active predicate registry to constrain extraction to: SLICE1 always, plus
    the DILIGENCE vocabulary when `include_diligence=True`. Filtered to status=='active'
    (a predicate with no explicit status is treated as active). Returns fresh dicts'
    references from the module-level lists (do not mutate them in place)."""
    src: list[dict] = list(SLICE1_PREDICATES)
    if include_diligence:
        src += DILIGENCE_PREDICATES
    return [p for p in src if p.get("status", "active") == "active"]
