"""Pure unit tests for `build_market_map` — no DB, no LLM.

Asserts the grounded-map contract: category grouping (incl. a company under multiple
categories), every non-empty cell resolves to a citation carrying document+block+quote,
deterministic citation ordering, `not_collected` (a missing predicate is omitted), and
the stats counts.
"""
from __future__ import annotations

from roster_vertical.claim_predicates import SLICE1_PREDICATES
from roster_vertical.landscape_map import build_market_map


def _ev(doc: str, block: str, quote: str, tier: int = 1) -> dict:
    return {"document_id": doc, "block_id": block, "quote": quote, "authority_tier": tier}


def _cat_claim(onorm: str, label: str, ent_id: str, ev: dict) -> dict:
    return {"predicate": "operates_in_category", "object_kind": "entity",
            "object_value": label, "object_entity_id": ent_id, "object_norm": onorm,
            "confidence": 0.9, "evidence": ev}


def _val_claim(pred: str, value: str, onorm: str, ev: dict) -> dict:
    return {"predicate": pred, "object_kind": "value", "object_value": value,
            "object_entity_id": "", "object_norm": onorm, "confidence": 0.8,
            "evidence": ev}


def _fixture() -> list[dict]:
    # Acme: two categories (vector db + observability), cells across several predicates.
    acme = {
        "entity_id": "domain:acme.com", "name": "Acme", "kind": "company",
        "primary_domain": "acme.com",
        "claims": [
            _cat_claim("vector database", "Vector Database", "category:vecdb",
                       _ev("d1", "b1", "Acme builds a vector database")),
            _cat_claim("observability", "Observability", "category:obs",
                       _ev("d1", "b2", "Acme also does observability")),
            _val_claim("offers_product", "AcmeVec", "acmevec",
                       _ev("d1", "b3", "AcmeVec is our vector product")),
            _val_claim("targets_customer", "enterprise", "enterprise",
                       _ev("d1", "b4", "We sell to enterprise buyers")),
            _val_claim("uses_technology", "HNSW", "hnsw",
                       _ev("d1", "b5", "Powered by HNSW indexing")),
            {"predicate": "compared_to", "object_kind": "entity", "object_value": "",
             "object_entity_id": "domain:beta.com", "object_norm": "beta",
             "confidence": 0.7, "evidence": _ev("d1", "b6", "unlike Beta, Acme is faster")},
        ],
    }
    # Beta: one category (vector db). No uses_technology cell → not_collected.
    beta = {
        "entity_id": "domain:beta.com", "name": "Beta", "kind": "company",
        "primary_domain": "beta.com",
        "claims": [
            _cat_claim("vector database", "Vector Database", "category:vecdb",
                       _ev("d2", "b1", "Beta is a vector database company")),
            _val_claim("offers_product", "BetaStore", "betastore",
                       _ev("d2", "b2", "BetaStore is our flagship")),
        ],
    }
    return [acme, beta]


def test_structure_and_grouping() -> None:
    m = build_market_map(_fixture(), predicates=SLICE1_PREDICATES)
    cats = {c["object_norm"]: c for c in m["categories"]}
    assert set(cats) == {"vector database", "observability"}
    # vector database has both Acme and Beta; observability only Acme.
    vec_names = {c["name"] for c in cats["vector database"]["companies"]}
    obs_names = {c["name"] for c in cats["observability"]["companies"]}
    assert vec_names == {"Acme", "Beta"}
    assert obs_names == {"Acme"}
    # category label comes from the stated object_value.
    assert cats["vector database"]["name"] == "Vector Database"


def test_company_under_multiple_categories() -> None:
    m = build_market_map(_fixture(), predicates=SLICE1_PREDICATES)
    appearances = [co["entity_id"]
                   for cat in m["categories"] for co in cat["companies"]]
    assert appearances.count("domain:acme.com") == 2   # placed under 2 categories


def test_every_cell_is_cited_and_resolves() -> None:
    m = build_market_map(_fixture(), predicates=SLICE1_PREDICATES)
    cit_by_id = {c["citation_id"]: c for c in m["citations"]}
    seen = set()
    for cat in m["categories"]:
        for co in cat["companies"]:
            for pred, entries in co["cells"].items():
                for cell in entries:
                    cid = cell["citation_id"]
                    assert cid in cit_by_id, f"uncited cell {pred} {cell}"
                    c = cit_by_id[cid]
                    # resolves to document_id + block_id + quote
                    assert c["document_id"] and c["block_id"] and c["quote"]
                    assert c["predicate"] == pred
                    # value/entity cell carries the object the citation points at
                    assert cell.get("value", cell.get("entity_id")) == c["object"]
                    seen.add(cid)
    # no orphan citations — every citation is referenced by exactly one cell
    assert seen == set(cit_by_id)


def test_not_collected_predicate_omitted() -> None:
    m = build_market_map(_fixture(), predicates=SLICE1_PREDICATES)
    beta = next(co for cat in m["categories"] for co in cat["companies"]
                if co["entity_id"] == "domain:beta.com")
    # Beta never asserted uses_technology → the cell is simply absent (not empty/None)
    assert "uses_technology" not in beta["cells"]
    assert "offers_product" in beta["cells"]


def test_entity_cell_shape() -> None:
    m = build_market_map(_fixture(), predicates=SLICE1_PREDICATES)
    acme = next(co for cat in m["categories"] for co in cat["companies"]
                if co["entity_id"] == "domain:acme.com")
    compared = acme["cells"]["compared_to"]
    assert compared[0]["entity_id"] == "domain:beta.com"   # entity cell, not "value"
    assert "value" not in compared[0]


def test_determinism() -> None:
    fx = _fixture()
    a = build_market_map(fx, predicates=SLICE1_PREDICATES)
    b = build_market_map(fx, predicates=SLICE1_PREDICATES)
    assert a == b
    # citation ids are the stable c1..cN sequence
    ids = [c["citation_id"] for c in a["citations"]]
    assert ids == [f"c{i}" for i in range(1, len(ids) + 1)]
    # order independence: shuffling input companies yields the SAME map
    c = build_market_map(list(reversed(fx)), predicates=SLICE1_PREDICATES)
    assert c == a


def test_stats() -> None:
    m = build_market_map(_fixture(), predicates=SLICE1_PREDICATES)
    assert m["stats"]["n_companies"] == 2      # Acme + Beta placed
    assert m["stats"]["n_categories"] == 2      # vector database + observability
    assert m["stats"]["n_cited_claims"] == len(m["citations"])
    # Acme: 4 cells (product/customer/tech/compared_to); Beta: 1 (product) = 5
    assert m["stats"]["n_cited_claims"] == 5


def test_uncited_claim_is_dropped() -> None:
    # A cell claim with no evidence must NEVER produce an uncited cell.
    co = {
        "entity_id": "domain:x.com", "name": "X", "kind": "company", "primary_domain": "x.com",
        "claims": [
            _cat_claim("vector database", "Vector Database", "category:vecdb",
                       _ev("d9", "b1", "X is a vector db")),
            {"predicate": "offers_product", "object_kind": "value", "object_value": "Ghost",
             "object_entity_id": "", "object_norm": "ghost", "confidence": 0.5,
             "evidence": None},   # ungrounded → dropped
        ],
    }
    m = build_market_map([co], predicates=SLICE1_PREDICATES)
    x = m["categories"][0]["companies"][0]
    assert "offers_product" not in x["cells"]
    assert m["stats"]["n_cited_claims"] == 0


def test_company_without_category_is_omitted() -> None:
    # No grounded operates_in_category claim → company is not placed anywhere.
    co = {
        "entity_id": "domain:y.com", "name": "Y", "kind": "company", "primary_domain": "y.com",
        "claims": [_val_claim("offers_product", "Thing", "thing", _ev("d3", "b1", "Y makes Thing"))],
    }
    m = build_market_map([co], predicates=SLICE1_PREDICATES)
    assert m["categories"] == []
    assert m["stats"]["n_companies"] == 0
    assert m["stats"]["n_cited_claims"] == 0
