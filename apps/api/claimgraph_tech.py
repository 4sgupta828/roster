"""Tech-vertical WIRING for the domain-neutral claim graph (H1 decouple).

The store `api.claimgraph.ClaimGraphStore` imports NOTHING from any vertical — it takes
its vocabulary (predicate seed list, placement predicate, subject entity-kind, mintable
object entity-kinds) as constructor config. This module is the single place the TECH
vocabulary is injected, so every tech call site (population route, diligence route, the
extraction job/CLI) builds an identically-configured store. A second vertical would ship
its own `make_<vertical>_claim_store` helper and reuse the store untouched.

This is app-level wiring (app depends on the vertical — the allowed direction), which is
why it lives here in `apps/api/` and not in the store module or the vertical.
"""
from __future__ import annotations

from api.claimgraph import ClaimGraphStore
from roster_vertical.claim_predicates import SLICE1_PREDICATES, active_predicates

# The tech landscape/placement vocabulary (Rule 18: config, not heuristics).
TECH_PLACEMENT_PREDICATE = "operates_in_category"
TECH_SUBJECT_KIND = "company"
TECH_MINTABLE_ENTITY_KINDS: tuple[str, ...] = ("category", "person", "investor", "company")

# The predicate set the LANDSCAPE/population map is built from — SLICE1 only (the diligence
# predicates are single-company depth, not landscape cells). Passed explicitly to
# `population_claims` so the map stays byte-identical to the pre-decouple behavior even
# though the registry now also holds the 12 diligence predicates.
TECH_POPULATION_PREDICATES: list[str] = [p["name"] for p in SLICE1_PREDICATES]


def make_tech_claim_store(dsn: str) -> ClaimGraphStore:
    """Build a TECH-configured `ClaimGraphStore`: seeds the full active tech registry
    (SLICE1 + DILIGENCE = 18 predicates, each with its `object_entity_kind`) into
    `rs_predicate`, and injects the tech placement predicate / subject kind / mintable
    object entity-kinds. Every tech call site uses this so the wiring is consistent."""
    return ClaimGraphStore(
        dsn,
        seed_predicates=active_predicates(include_diligence=True),
        placement_predicate=TECH_PLACEMENT_PREDICATE,
        subject_kind=TECH_SUBJECT_KIND,
        mintable_entity_kinds=TECH_MINTABLE_ENTITY_KINDS,
    )
