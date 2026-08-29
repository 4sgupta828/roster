"""Unit tests for the predicate registry (SLICE1 + slice-2 DILIGENCE vocabulary).

Pure, offline: the registry is DATA. These lock the shape the extraction job and the
extractor prompt depend on — every predicate has the required fields, entity predicates
carry `object_entity_kind` (the config that drives the job's data-driven object router,
Rule 18), and `active_predicates(include_diligence=...)` composes the two lists correctly.
"""
from __future__ import annotations

from roster_vertical.claim_predicates import (
    DILIGENCE_PREDICATES,
    SLICE1_PREDICATES,
    active_predicates,
)

# The diligence predicates the build requires (breadth of dimensions; +revenue/ICP first-classed).
_EXPECTED_DILIGENCE = {
    "has_founder", "has_investor", "raised_funding", "headcount",
    "company_location", "company_status", "business_model", "moat_claim",
    "traction_metric", "customer_evidence", "risk_factor", "go_to_market",
    "revenue", "ideal_customer_profile",
}
# Entity predicates → the rs_entity.kind their object must mint.
_EXPECTED_ENTITY_KIND = {"has_founder": "person", "has_investor": "investor"}

_REQUIRED_FIELDS = {"name", "status", "object_kind", "cardinality",
                    "temporal_policy", "description"}
_VALID_OBJECT_KIND = {"value", "entity", "either"}
_VALID_CARDINALITY = {"single", "multi"}
_VALID_TEMPORAL = {"static", "point", "interval"}


def test_all_diligence_predicates_exist_and_are_active():
    names = [p["name"] for p in DILIGENCE_PREDICATES]
    assert len(names) == 14
    assert set(names) == _EXPECTED_DILIGENCE
    assert len(set(names)) == 14                       # no dups
    for p in DILIGENCE_PREDICATES:
        assert p["status"] == "active"


def test_every_diligence_predicate_has_the_required_shape():
    for p in DILIGENCE_PREDICATES:
        assert _REQUIRED_FIELDS <= set(p), f"{p.get('name')} missing fields"
        assert p["object_kind"] in _VALID_OBJECT_KIND
        assert p["cardinality"] in _VALID_CARDINALITY
        assert p["temporal_policy"] in _VALID_TEMPORAL
        assert p["description"].strip()                # drives the extractor prompt


def test_entity_diligence_predicates_carry_object_entity_kind():
    for p in DILIGENCE_PREDICATES:
        if p["object_kind"] == "entity":
            assert p.get("object_entity_kind"), f"{p['name']} missing object_entity_kind"
    # the two entity diligence predicates route to person / investor
    by_name = {p["name"]: p for p in DILIGENCE_PREDICATES}
    for name, kind in _EXPECTED_ENTITY_KIND.items():
        assert by_name[name]["object_kind"] == "entity"
        assert by_name[name]["object_entity_kind"] == kind
    # value predicates carry NO object_entity_kind (it is entity-only config)
    for p in DILIGENCE_PREDICATES:
        if p["object_kind"] == "value":
            assert "object_entity_kind" not in p


def test_slice1_entity_predicates_now_carry_object_entity_kind():
    # Change 2: all SLICE1 entity predicates gained object_entity_kind so ALL routing is data-driven.
    by_name = {p["name"]: p for p in SLICE1_PREDICATES}
    assert by_name["operates_in_category"]["object_entity_kind"] == "category"
    assert by_name["compared_to"]["object_entity_kind"] == "company"
    # no SLICE1 entity predicate is left without the field
    for p in SLICE1_PREDICATES:
        if p["object_kind"] == "entity":
            assert p.get("object_entity_kind"), f"{p['name']} missing object_entity_kind"


def test_active_predicates_default_is_slice1_only():
    default = active_predicates()
    names = {p["name"] for p in default}
    assert names == {p["name"] for p in SLICE1_PREDICATES}
    assert _EXPECTED_DILIGENCE.isdisjoint(names)        # no diligence leakage by default


def test_active_predicates_include_diligence_returns_slice1_plus_diligence():
    both = active_predicates(include_diligence=True)
    names = {p["name"] for p in both}
    assert names == {p["name"] for p in SLICE1_PREDICATES} | _EXPECTED_DILIGENCE
    assert len(both) == len(SLICE1_PREDICATES) + len(DILIGENCE_PREDICATES)
    assert all(p.get("status", "active") == "active" for p in both)


def test_active_predicates_filters_out_non_active():
    # A candidate-status predicate is excluded from the active set even if present in the source.
    orig = list(DILIGENCE_PREDICATES)
    try:
        DILIGENCE_PREDICATES.append({
            "name": "internal_only", "status": "candidate", "object_kind": "value",
            "cardinality": "multi", "temporal_policy": "static", "description": "x"})
        names = {p["name"] for p in active_predicates(include_diligence=True)}
        assert "internal_only" not in names
    finally:
        DILIGENCE_PREDICATES[:] = orig
