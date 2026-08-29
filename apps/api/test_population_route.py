"""Unit tests for the Task-4 population route — NO DB, NO LLM.

Covers the CODE-owned pieces (Rule 18): the citation-resolution gate drops a
hallucinated `[cN]` and keeps a valid one; the deterministic renderers produce a
citation panel with one line per citation and a coverage block that names the covered
categories + the honest population statement. The end-to-end wiring (store → compose →
derive) is exercised by the DSN-gated integration test.
"""
from __future__ import annotations

from api.population_route import _validate_citations
from roster_vertical.population_compose import (
    render_coverage_facts,
    render_market_map_for_compose,
)


def _market_map() -> dict:
    """Hand-built map in `build_market_map`'s output shape: 2 companies, 1 category,
    two cited cells (c1, c2)."""
    return {
        "categories": [
            {"name": "Vector Database", "object_norm": "vector database", "companies": [
                {"entity_id": "domain:acme.com", "name": "Acme", "cells": {
                    "offers_product": [{"value": "AcmeVec", "citation_id": "c1"}]}},
                {"entity_id": "domain:beta.com", "name": "Beta", "cells": {
                    "targets_customer": [{"value": "enterprise", "citation_id": "c2"}]}},
            ]},
        ],
        "citations": [
            {"citation_id": "c1", "entity_id": "domain:acme.com", "predicate": "offers_product",
             "object": "AcmeVec", "document_id": "d1", "block_id": "b1",
             "quote": "Acme offers AcmeVec", "authority_tier": 2},
            {"citation_id": "c2", "entity_id": "domain:beta.com", "predicate": "targets_customer",
             "object": "enterprise", "document_id": "d2", "block_id": "b2",
             "quote": "Beta targets enterprise", "authority_tier": 1},
        ],
        "stats": {"n_companies": 2, "n_categories": 1, "n_cited_claims": 2},
    }


def test_citation_gate_drops_hallucinated_keeps_valid() -> None:
    valid = {"c1", "c2"}
    answer = "Acme [c1] leads; Beta [c2] follows; Ghost [c9] is invented [c1]."
    cleaned, dropped = _validate_citations(answer, valid)
    assert dropped == 1                       # only [c9] dropped
    assert "[c9]" not in cleaned
    assert cleaned.count("[c1]") == 2         # both valid [c1] survive
    assert "[c2]" in cleaned


def test_citation_gate_noop_when_all_valid() -> None:
    cleaned, dropped = _validate_citations("only [c1] and [c2]", {"c1", "c2"})
    assert dropped == 0
    assert cleaned == "only [c1] and [c2]"


def test_citation_gate_empty_answer() -> None:
    cleaned, dropped = _validate_citations("", {"c1"})
    assert cleaned == "" and dropped == 0


def test_render_panel_one_line_per_citation() -> None:
    mm = _market_map()
    rendered = render_market_map_for_compose(mm)
    assert "## Citation panel" in rendered
    # one panel line per citation, each carrying its id + company + quote
    assert "[c1] Acme — offers_product: AcmeVec" in rendered
    assert "[c2] Beta — targets_customer: enterprise" in rendered
    assert 'quote: "Acme offers AcmeVec"' in rendered
    # the clustered map names the category and cites the cells
    assert "### Vector Database" in rendered
    assert "AcmeVec [c1]" in rendered


def test_render_empty_map() -> None:
    rendered = render_market_map_for_compose({"categories": [], "citations": [], "stats": {}})
    assert "no grounded companies in scope" in rendered
    assert "## Citation panel" in rendered   # panel header still present (empty)


def test_render_coverage_facts_names_categories_and_population() -> None:
    cb = {
        "categories_covered": ["Vector Database", "Observability"],
        "n_categories_total": 5,
        "n_categories_in_scope": 2,
        "n_companies": 7,
        "truncation": {"companies_truncated": True, "company_cap": 400,
                       "claims_truncated": False},
        "population_statement": "NOT all startups in the market.",
    }
    block = render_coverage_facts(cb)
    assert "Vector Database" in block and "Observability" in block
    assert "companies mapped: 7" in block
    assert "TRUNCATED" in block                       # truncation surfaced
    assert "NOT all startups in the market." in block  # honest population statement
