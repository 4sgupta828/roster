"""EVAL TRAPS (docs/specs/facet-contract-evaluator.md §9) — cases DESIGNED to pass a naive path while being
wrong: a wrong-field posting that reads well semantically, a level that was never stated, pay that was
never written, a startup label with no lookup behind it. Each must fail safe through the evaluator."""
from __future__ import annotations

import asyncio

from roster_kernel.facets import UNKNOWN, Contract, FacetWeights, InMemoryFacetStore, evaluate
from roster_vertical.facet_schema import FACET_SCHEMA, FACET_WEIGHTS

from api.facets_engine import extract_envelopes


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ROWS = [
    # semantically the closest text for a "founder cto" brief, but a SALES role (a Field CTO)
    {"id": "field_cto", "kind": "job", "sim": 0.64, "title": "Field CTO", "facets": {"field": ["sales"], "function": ["sales"], "level": ["leadership"], "work_type": ["ic"]}},
    # the right field, slightly lower similarity
    {"id": "founding_cto", "kind": "job", "sim": 0.60, "title": "Founding CTO", "facets": {"field": ["software"], "function": ["executive"], "level": ["leadership"], "work_type": ["founder"], "company": ["acme"]}},
    # a title that states no level at all: must never satisfy a level must
    {"id": "swe", "kind": "job", "sim": 0.61, "title": "Software Engineer", "facets": {"field": ["software"], "function": ["engineering"]}},
    # a company nobody has lookup data for: company_type stays unknown
    {"id": "mystery", "kind": "job", "sim": 0.59, "title": "CTO", "facets": {"field": ["software"], "level": ["leadership"], "company": ["mysteryco"]}},
]


def test_wrong_field_ranks_below_the_right_field_even_when_more_similar():
    c = Contract(kind="job", text="founder cto", prefer={"field": ["software"]}, avoid={"field": ["sales"]})
    out = _run(evaluate(c, InMemoryFacetStore(ROWS, FACET_SCHEMA), FACET_SCHEMA, FACET_WEIGHTS, noise_floor=0.4))
    ids = [r["id"] for r in out["rows"]]
    assert ids.index("founding_cto") < ids.index("field_cto")
    fc = next(r for r in out["rows"] if r["id"] == "field_cto")
    assert any("avoid field: sales" in x for x in fc["reasons"])


def test_an_unstated_level_never_satisfies_a_level_must_and_is_counted_as_unknown():
    c = Contract(kind="job", text="founder cto", must={"level": ["leadership"]})
    out = _run(evaluate(c, InMemoryFacetStore(ROWS, FACET_SCHEMA), FACET_SCHEMA, FACET_WEIGHTS, noise_floor=0.4))
    assert "swe" not in {r["id"] for r in out["rows"]}
    all_counts = _run(evaluate(Contract(kind="job", text="x"), InMemoryFacetStore(ROWS, FACET_SCHEMA), FACET_SCHEMA, FACET_WEIGHTS, noise_floor=0.4))["counts"]
    assert all_counts["level"][UNKNOWN] == 1 and all_counts["company_type"] == {UNKNOWN: 4}


def test_invented_pay_never_becomes_a_band():
    llm = lambda s, u: {"items": [{"i": 0, "comp": {"min": 200000, "max": 250000, "currency": "USD", "period": "year"}}]}
    env = extract_envelopes("job", [{"title": "CTO", "head": "Join us. Competitive package.", "tail": "We are an equal opportunity employer."}], FACET_SCHEMA, llm)
    assert "comp" not in env[0]["facets"]
    c = Contract(kind="job", text="cto", must={"comp": {"min": 150000}})
    out = _run(evaluate(c, InMemoryFacetStore(ROWS, FACET_SCHEMA), FACET_SCHEMA, FACET_WEIGHTS, noise_floor=0.4))
    assert out["rows"] == []                                                       # nothing states pay → nothing passes a pay floor


def test_a_startup_label_needs_lookup_data_behind_it():
    c = Contract(kind="job", text="cto", must={"company_type": ["startup"]})
    out = _run(evaluate(c, InMemoryFacetStore(ROWS, FACET_SCHEMA), FACET_SCHEMA, FACET_WEIGHTS, noise_floor=0.4))
    assert out["rows"] == []                                                       # no company facets → no startup claims
    rows = [{**ROWS[1], "facets": {**ROWS[1]["facets"], "company_type": ["startup"]}}]   # lookup present (via company)
    out2 = _run(evaluate(c, InMemoryFacetStore(rows, FACET_SCHEMA), FACET_SCHEMA, FACET_WEIGHTS, noise_floor=0.4))
    assert [r["id"] for r in out2["rows"]] == ["founding_cto"]
