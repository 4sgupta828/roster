"""Unit tests (NO DB) for the claim-graph pure helpers + vertical predicate seed.

These run in the offline suite with no Postgres. The DB round-trips live in
`test_claimgraph_integration.py` (gated on ROSTER_CORPUS_DSN).
"""
from __future__ import annotations

from api.claimgraph import (
    make_claim_id,
    make_evidence_id,
    normalize_name,
    normalize_quote,
    quote_hash,
)


# --- normalize_name: legal-suffix strip + ws/case ------------------------- #
def test_normalize_name_strips_legal_suffixes() -> None:
    assert normalize_name("Acme, Inc.") == "acme"
    assert normalize_name("Acme LLC") == "acme"
    assert normalize_name("Acme Ltd") == "acme"
    assert normalize_name("Acme Corp") == "acme"
    assert normalize_name("Acme Corporation") == "acme"


def test_normalize_name_ws_and_case() -> None:
    assert normalize_name("  OpenAI   Global  ") == "openai global"
    assert normalize_name("HuggingFace") == "huggingface"
    # multiple trailing legal forms collapse
    assert normalize_name("Acme Holdings Corp Inc") == "acme holdings"


def test_normalize_name_no_suffix_untouched_core() -> None:
    # a mid-name token that happens to match a suffix word is NOT stripped
    assert normalize_name("Corp Software") == "corp software"
    assert normalize_name("") == ""
    assert normalize_name("Inc") == ""   # a bare legal form normalizes to empty


# --- normalize_quote: must equal provenance.normalize --------------------- #
def test_normalize_quote_matches_provenance() -> None:
    from roster_kernel.research.provenance import normalize as prov_normalize
    samples = [
        "  The  Company\tships\n a Product.  ",
        "Nine point SIX percent",
        "\n\nMixed   WHITESPACE and CASE\t\t",
        "already normal",
        "",
    ]
    for s in samples:
        assert normalize_quote(s) == prov_normalize(s)


def test_quote_hash_stable_and_reflow_tolerant() -> None:
    a = quote_hash("The  company ships\na product")
    b = quote_hash("the company ships a product")   # ws-collapsed + lowered → same
    assert a == b
    assert quote_hash("a different quote") != a
    assert len(a) == 64   # sha256 hex


# --- deterministic ids: stability + dedup-key correctness ----------------- #
def _base_kwargs():
    return dict(tenant_id="demo", subject_id="domain:acme.com",
                predicate="operates_in_category", object_kind="entity",
                object_norm="", object_entity_id="category:devtools",
                valid_from="", schema_version=1)


def test_claim_id_same_logical_claim_same_id() -> None:
    a = make_claim_id(**_base_kwargs())
    b = make_claim_id(**_base_kwargs())
    assert a == b and len(a) == 64


def test_claim_id_valid_from_changes_id() -> None:
    a = make_claim_id(**_base_kwargs())
    kw = _base_kwargs(); kw["valid_from"] = "2025-01-01"
    b = make_claim_id(**kw)
    assert a != b   # bitemporal: a different valid_from is a different logical claim


def test_claim_id_object_discriminator() -> None:
    # entity-claims key on object_entity_id; object_norm is ignored for them
    kw = _base_kwargs(); kw["object_norm"] = "ignored"
    assert make_claim_id(**kw) == make_claim_id(**_base_kwargs())
    # value-claims key on object_norm
    v1 = make_claim_id(tenant_id="demo", subject_id="s", predicate="offers_product",
                       object_kind="value", object_norm="widget",
                       object_entity_id="", valid_from="", schema_version=1)
    v2 = make_claim_id(tenant_id="demo", subject_id="s", predicate="offers_product",
                       object_kind="value", object_norm="gadget",
                       object_entity_id="", valid_from="", schema_version=1)
    assert v1 != v2
    # different subject/predicate/tenant/schema_version all change the id
    for field, val in [("subject_id", "s2"), ("predicate", "uses_technology"),
                       ("tenant_id", "other"), ("schema_version", 2)]:
        kw2 = dict(tenant_id="demo", subject_id="s", predicate="offers_product",
                   object_kind="value", object_norm="widget", object_entity_id="",
                   valid_from="", schema_version=1)
        kw2[field] = val
        assert make_claim_id(**kw2) != v1


def test_evidence_id_stable_and_span_scoped() -> None:
    a = make_evidence_id(claim_id="c1", document_id="d1", block_id="b1",
                         quote_hash_hex="qh")
    assert a == make_evidence_id(claim_id="c1", document_id="d1", block_id="b1",
                                 quote_hash_hex="qh")
    # different block or quote → different evidence id
    assert a != make_evidence_id(claim_id="c1", document_id="d1", block_id="b2",
                                 quote_hash_hex="qh")
    assert a != make_evidence_id(claim_id="c1", document_id="d1", block_id="b1",
                                 quote_hash_hex="qh2")


# --- vertical predicate seed shape --------------------------------------- #
def test_slice1_predicates_shape() -> None:
    from roster_vertical.claim_predicates import SLICE1_PREDICATES
    names = [p["name"] for p in SLICE1_PREDICATES]
    assert names == ["operates_in_category", "offers_product", "targets_customer",
                     "uses_technology", "claims_differentiator", "compared_to"]
    assert len(SLICE1_PREDICATES) == 6
    for p in SLICE1_PREDICATES:
        assert p["status"] == "active"
        assert p["object_kind"] in ("value", "entity", "either")
        assert p["cardinality"] in ("single", "multi")
        assert p["temporal_policy"] in ("static", "point", "interval")
        assert p.get("description")
    # the two relational predicates are entity-valued
    by = {p["name"]: p for p in SLICE1_PREDICATES}
    assert by["operates_in_category"]["object_kind"] == "entity"
    assert by["compared_to"]["object_kind"] == "entity"
