"""Unit tests for the D3 diligence compose module — NO DB, NO LLM.

Covers the CODE-owned pieces (Rule 18): the DIMENSION map is total + disjoint over the
active predicate registry; the deterministic renderer groups a single company's claims
by dimension, cites every non-empty cell, renders an empty dimension as `not_collected`,
resolves every citation in the panel, and is order-independent (deterministic ids). The
end-to-end wiring (store → compose → derive) is exercised by the DSN-gated integration
test.
"""
from __future__ import annotations

from roster_vertical.claim_predicates import active_predicates
from roster_vertical.diligence_compose import (
    DIMENSIONS,
    PREDICATE_TO_DIMENSION,
    dimension_coverage,
    render_diligence_coverage_facts,
    render_entity_state_for_compose,
)


def _claim(predicate: str, *, value: str = "", entity_id: str = "",
           quote: str, subject: str = "domain:acme.com") -> dict:
    """Minimal entity_claims-shaped claim dict (only the keys the renderer reads)."""
    return {
        "entity_id": subject,
        "predicate": predicate,
        "object_kind": "entity" if entity_id else "value",
        "object_value": value,
        "object_entity_id": entity_id,
        "evidence": {"document_id": "d-" + predicate, "block_id": "b-" + predicate,
                     "quote": quote, "authority_tier": 2},
    }


# --------------------------------------------------------------------------- #
# DIMENSION map: total + disjoint coverage of the active predicate registry.   #
# --------------------------------------------------------------------------- #
def test_dimension_map_covers_every_predicate_exactly_once() -> None:
    registry = {p["name"] for p in active_predicates(include_diligence=True)}
    # Flatten the dimension map WITH multiplicity to catch a predicate placed twice.
    flat = [pred for _name, preds in DIMENSIONS for pred in preds]
    assert len(flat) == len(set(flat)), "a predicate appears in more than one dimension"
    mapped = set(flat)
    assert mapped == registry, (
        f"dimension map != registry; missing={registry - mapped}, extra={mapped - registry}")
    # The flat index agrees with DIMENSIONS.
    assert set(PREDICATE_TO_DIMENSION) == registry
    assert len(PREDICATE_TO_DIMENSION) == len(flat)


def test_dimension_names_are_ordered_and_unique() -> None:
    names = [n for n, _ in DIMENSIONS]
    assert names == ["Team", "Funding", "Traction", "Product & Technology",
                     "Market & Customers", "Moat & Differentiation", "Company profile"]
    assert len(names) == len(set(names))


# --------------------------------------------------------------------------- #
# Renderer: dimension grouping, every cell cited, not_collected, determinism.  #
# --------------------------------------------------------------------------- #
def _sample_claims() -> list[dict]:
    """A company with claims across Team (founder→person + headcount), Product, and
    Market — leaving Funding, Traction, Moat, Company-profile empty."""
    return [
        _claim("has_founder", entity_id="person:jane", quote="Jane Roe co-founded Acme"),
        _claim("headcount", value="25", quote="Acme has 25 employees"),
        _claim("offers_product", value="AcmeVec", quote="Acme offers AcmeVec"),
        _claim("operates_in_category", entity_id="category:vectordb",
               quote="Acme operates in vector databases"),
    ]


def test_render_groups_by_dimension_and_cites_every_cell() -> None:
    rendered, citations = render_entity_state_for_compose("Acme", _sample_claims())
    # every dimension heading present, in order
    for name, _ in DIMENSIONS:
        assert f"### {name}" in rendered
    # populated cells carry a [cN]; empty dimensions say not_collected
    assert "has_founder: person:jane [c" in rendered
    assert "headcount: 25 [c" in rendered
    assert "offers_product: AcmeVec [c" in rendered
    assert "not_collected" in rendered              # Funding/Traction/Moat/Profile empty
    # one citation per grounded claim; each resolves in the panel
    assert len(citations) == 4
    assert "## Citation panel" in rendered
    valid = {c["citation_id"] for c in citations}
    import re
    markers = set(re.findall(r"\[(c\d+)\]", rendered))
    assert markers <= valid                          # no dangling marker
    # panel rows carry id + company + predicate + quote
    assert 'quote: "Jane Roe co-founded Acme"' in rendered
    for c in citations:
        assert c["name"] == "Acme"
        assert c["document_id"] and c["block_id"]    # grounding carried through


def test_empty_company_all_dimensions_not_collected() -> None:
    rendered, citations = render_entity_state_for_compose("Ghost", [])
    assert citations == []
    # every dimension present and marked not_collected
    assert rendered.count("not_collected") == len(DIMENSIONS)


def test_claim_without_evidence_is_not_cited() -> None:
    claims = [
        _claim("offers_product", value="AcmeVec", quote="Acme offers AcmeVec"),
        {"entity_id": "domain:acme.com", "predicate": "headcount", "object_kind": "value",
         "object_value": "25", "object_entity_id": "", "evidence": None},          # no evidence
        {"entity_id": "domain:acme.com", "predicate": "moat_claim", "object_kind": "value",
         "object_value": "x", "object_entity_id": "",
         "evidence": {"document_id": "d", "block_id": "b", "quote": "   ",           # blank quote
                      "authority_tier": 1}},
    ]
    rendered, citations = render_entity_state_for_compose("Acme", claims)
    assert len(citations) == 1                        # only the grounded product claim
    assert citations[0]["predicate"] == "offers_product"


def test_render_is_deterministic_regardless_of_input_order() -> None:
    claims = _sample_claims()
    r1, c1 = render_entity_state_for_compose("Acme", claims)
    r2, c2 = render_entity_state_for_compose("Acme", list(reversed(claims)))
    assert r1 == r2                                   # byte-identical render
    # stable predicate/object → citation_id mapping
    m1 = {(c["predicate"], c["object"]): c["citation_id"] for c in c1}
    m2 = {(c["predicate"], c["object"]): c["citation_id"] for c in c2}
    assert m1 == m2


def test_dimension_coverage_covered_vs_not_collected() -> None:
    cov = dimension_coverage(_sample_claims())
    assert "Team" in cov["covered"]
    assert "Product & Technology" in cov["covered"]
    assert "Market & Customers" in cov["covered"]
    assert "Funding" in cov["not_collected"]
    assert "Traction" in cov["not_collected"]
    assert len(cov["covered"]) + len(cov["not_collected"]) == len(DIMENSIONS)


def test_render_coverage_facts_names_dimensions_and_population() -> None:
    cov = dimension_coverage(_sample_claims())
    cb = {
        "dimensions_covered": cov["covered"],
        "dimensions_not_collected": cov["not_collected"],
        "n_cited_claims": 4,
        "company_name": "Acme",
        "subject_id": "domain:acme.com",
        "population_statement": "NOT a complete picture.",
    }
    block = render_diligence_coverage_facts(cb)
    assert "Team" in block and "Funding" in block
    assert "cited grounded claims: 4" in block
    assert "NOT a complete picture." in block
