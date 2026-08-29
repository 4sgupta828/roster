"""Unit tests for the CROSSVIEWS grid builder (`api.crossviews.build_grid`).

No DB: `build_grid` is exercised over a FAKE store that returns canned
`population_claims` / `founder_rows` / `distinct_categories` /
`category_member_companies` shapes. Covers the pivot (multi-value cell + citations,
not_collected), the person + category grids, the every-non-empty-predicate-cell-is-
cited invariant, spec sort, and the empty/bad-spec fail-safe.

    /Users/sgupta/roster/.venv/bin/python -m pytest apps/api/test_crossviews.py -q
"""
from __future__ import annotations

import asyncio

from api.crossviews import _DENSITY_POOL_CAP, CrossviewSpec, build_grid


def _ev(doc: str, block: str, quote: str, tier: int = 3) -> dict:
    return {"document_id": doc, "block_id": block, "quote": quote, "authority_tier": tier}


class FakeStore:
    """Canned async store — records the kwargs each read was called with so the tests
    can assert the builder passed the right restriction (predicates/cap/norm)."""

    def __init__(self, *, population=None, founders=None, categories=None, members=None):
        self._population = population or {"companies": [], "meta": {}}
        self._founders = founders or []
        self._categories = categories or []
        self._members = members or {}
        self.calls: dict[str, dict] = {}

    async def population_claims(self, **kw):
        self.calls["population_claims"] = kw
        return self._population

    async def founder_rows(self, **kw):
        self.calls["founder_rows"] = kw
        return list(self._founders)

    async def distinct_categories(self, **kw):
        self.calls["distinct_categories"] = kw
        return list(self._categories)

    async def category_member_companies(self, **kw):
        self.calls.setdefault("category_member_companies", kw)
        return list(self._members.get(kw.get("category_norm"), []))


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# company grid                                                                #
# --------------------------------------------------------------------------- #
def _company_population() -> dict:
    return {
        "companies": [
            {
                "entity_id": "domain:acme.com", "name": "Acme", "kind": "company",
                "primary_domain": "acme.com",
                "claims": [
                    {"predicate": "has_founder", "object_kind": "entity",
                     "object_value": "", "object_norm": "jane roe",
                     "object_entity_id": "person:jane", "confidence": 0.9,
                     "evidence": _ev("doc1", "b1", "Jane Roe co-founded Acme")},
                    {"predicate": "has_founder", "object_kind": "entity",
                     "object_value": "", "object_norm": "john doe",
                     "object_entity_id": "person:john", "confidence": 0.9,
                     "evidence": _ev("doc1", "b2", "John Doe co-founded Acme")},
                    {"predicate": "offers_product", "object_kind": "value",
                     "object_value": "AcmeWidget", "object_norm": "acmewidget",
                     "object_entity_id": "", "confidence": 0.8,
                     "evidence": _ev("doc2", "b3", "Acme offers AcmeWidget", tier=2)},
                    # NOTE: no 'raised_funding' claim → that column must be not_collected.
                ],
            },
        ],
        "meta": {"company_count": 1, "companies_truncated": False,
                 "claims_truncated": False, "company_cap": 400},
    }


def test_company_grid_multivalue_notcollected_and_citations() -> None:
    store = FakeStore(population=_company_population())
    spec = CrossviewSpec(row_kind="company", columns=[
        {"id": "has_founder", "kind": "predicate"},
        {"id": "offers_product", "kind": "predicate"},
        {"id": "raised_funding", "kind": "predicate"},
    ])
    grid = _run(build_grid(store, spec))

    # The builder restricted the population read to exactly the predicate columns.
    assert store.calls["population_claims"]["predicates"] == [
        "has_founder", "offers_product", "raised_funding"]
    assert store.calls["population_claims"]["claims_per_company_cap"] == 200

    assert grid["row_kind"] == "company"
    assert grid["meta"]["row_count"] == 1
    row = grid["rows"][0]
    assert row["id"] == "domain:acme.com" and row["name"] == "Acme"

    # multi-value cell → a LIST of both founders + one citation per contributing claim
    founders = row["cells"]["has_founder"]
    assert founders["collected"] is True
    assert founders["value"] == ["jane roe", "john doe"]
    assert len(founders["citations"]) == 2
    assert founders["citations"][0] == {
        "quote": "Jane Roe co-founded Acme", "document_id": "doc1",
        "block_id": "b1", "authority_tier": 3}

    # single-value cell → a scalar string + one citation
    prod = row["cells"]["offers_product"]
    assert prod["value"] == "AcmeWidget"
    assert len(prod["citations"]) == 1

    # empty predicate → not_collected (never fabricated)
    assert row["cells"]["raised_funding"] == {"collected": False}

    # INVARIANT: every non-empty predicate cell carries >=1 citation.
    for cell in row["cells"].values():
        if cell.get("collected"):
            assert len(cell["citations"]) >= 1


def test_company_grid_sort_desc() -> None:
    pop = {
        "companies": [
            {"entity_id": "c:a", "name": "Alpha", "claims": [
                {"predicate": "headcount", "object_kind": "value",
                 "object_value": "10", "object_norm": "10", "object_entity_id": "",
                 "confidence": 0.8, "evidence": _ev("d", "b", "Alpha has 10")}]},
            {"entity_id": "c:z", "name": "Zeta", "claims": [
                {"predicate": "headcount", "object_kind": "value",
                 "object_value": "90", "object_norm": "90", "object_entity_id": "",
                 "confidence": 0.8, "evidence": _ev("d", "b2", "Zeta has 90")}]},
            {"entity_id": "c:n", "name": "Nil", "claims": []},   # not_collected → sorts last
        ],
        "meta": {"companies_truncated": False, "claims_truncated": False},
    }
    store = FakeStore(population=pop)
    spec = CrossviewSpec(row_kind="company",
                         columns=[{"id": "headcount", "kind": "predicate"}],
                         sort={"column_id": "headcount", "direction": "desc"})
    grid = _run(build_grid(store, spec))
    names = [r["name"] for r in grid["rows"]]
    assert names == ["Zeta", "Alpha", "Nil"]   # 90 > 10, empty last


def test_company_grid_requires_predicate_column() -> None:
    store = FakeStore(population=_company_population())
    # Only an aggregate column → no predicate columns → fail-safe empty grid.
    grid = _run(build_grid(store, CrossviewSpec(
        row_kind="company", columns=[{"id": "n_companies", "kind": "aggregate"}])))
    assert grid["rows"] == []
    assert grid["meta"]["row_count"] == 0
    assert any("predicate" in n for n in grid["meta"]["notes"])


# --------------------------------------------------------------------------- #
# person grid                                                                 #
# --------------------------------------------------------------------------- #
def test_person_grid_companies_and_count() -> None:
    founders = [
        {"person_id": "person:jane", "name": "Jane Roe", "companies": [
            {"company_id": "c:acme", "company_name": "Acme", "quote": "Jane founded Acme",
             "document_id": "d1", "block_id": "b1", "authority_tier": 3},
            {"company_id": "c:beta", "company_name": "Beta", "quote": "Jane founded Beta",
             "document_id": "d2", "block_id": "b2", "authority_tier": 2},
        ]},
    ]
    store = FakeStore(founders=founders)
    grid = _run(build_grid(store, CrossviewSpec(row_kind="person")))

    assert grid["row_kind"] == "person"
    assert [c["id"] for c in grid["columns"]] == ["companies_founded", "n_companies"]
    row = grid["rows"][0]
    assert row["id"] == "person:jane" and row["name"] == "Jane Roe"

    cf = row["cells"]["companies_founded"]
    assert cf["collected"] is True
    assert cf["value"] == ["Acme", "Beta"]
    assert len(cf["citations"]) == 2          # every edge cell is cited
    assert cf["citations"][0]["quote"] == "Jane founded Acme"

    nc = row["cells"]["n_companies"]
    assert nc["value"] == "2" and nc["collected"] is True


def test_person_grid_edge_cell_always_cited() -> None:
    founders = [
        {"person_id": "p:1", "name": "A", "companies": [
            {"company_id": "c", "company_name": "C", "quote": "q", "document_id": "d",
             "block_id": "b", "authority_tier": 1}]},
    ]
    store = FakeStore(founders=founders)
    grid = _run(build_grid(store, CrossviewSpec(row_kind="person",
                columns=[{"id": "companies_founded", "kind": "edge"}])))
    cell = grid["rows"][0]["cells"]["companies_founded"]
    assert cell["collected"] and len(cell["citations"]) >= 1


# --------------------------------------------------------------------------- #
# category grid                                                               #
# --------------------------------------------------------------------------- #
def test_category_grid_membercount_and_representatives() -> None:
    cats = [{"object_norm": "devtools", "name": "Dev Tools", "members": 3}]
    members = {"devtools": [
        {"company_id": "c:a", "name": "Acme", "quote": "Acme is a dev tools company",
         "document_id": "d1", "block_id": "b1", "authority_tier": 3},
        {"company_id": "c:b", "name": "Beta", "quote": "Beta is a dev tools company",
         "document_id": "d2", "block_id": "b2", "authority_tier": 2},
    ]}
    store = FakeStore(categories=cats, members=members)
    grid = _run(build_grid(store, CrossviewSpec(row_kind="category")))

    assert grid["row_kind"] == "category"
    row = grid["rows"][0]
    assert row["id"] == "devtools" and row["name"] == "Dev Tools"

    mc = row["cells"]["member_count"]
    assert mc["value"] == "3" and mc["collected"] is True

    reps = row["cells"]["representative_companies"]
    assert reps["collected"] is True
    assert reps["value"] == ["Acme", "Beta"]
    assert len(reps["citations"]) == 2        # each exemplar cites its placement span
    # aggregate-basis is documented in notes (member_count derivation).
    assert any("member_count" in n for n in grid["meta"]["notes"])


def test_category_grid_empty_representatives_notcollected() -> None:
    cats = [{"object_norm": "empty", "name": "Empty", "members": 1}]
    store = FakeStore(categories=cats, members={})   # no members returned
    grid = _run(build_grid(store, CrossviewSpec(row_kind="category")))
    cell = grid["rows"][0]["cells"]["representative_companies"]
    assert cell == {"collected": False}


# --------------------------------------------------------------------------- #
# fail-safe                                                                   #
# --------------------------------------------------------------------------- #
def test_bad_row_kind_failsafe() -> None:
    store = FakeStore()
    grid = _run(build_grid(store, {"row_kind": "bogus", "columns": []}))
    assert grid["rows"] == []
    assert grid["meta"]["row_count"] == 0
    assert any("row_kind" in n for n in grid["meta"]["notes"])


def test_none_spec_failsafe() -> None:
    store = FakeStore()
    grid = _run(build_grid(store, None))
    assert grid["rows"] == []
    assert grid["meta"]["notes"]


def test_empty_population_failsafe() -> None:
    store = FakeStore(population={"companies": [], "meta": {}})
    grid = _run(build_grid(store, CrossviewSpec(
        row_kind="company", columns=[{"id": "offers_product", "kind": "predicate"}])))
    assert grid["rows"] == []
    assert any("no grounded companies" in n for n in grid["meta"]["notes"])


def test_dict_spec_coercion() -> None:
    store = FakeStore(population=_company_population())
    grid = _run(build_grid(store, {
        "row_kind": "company",
        "columns": [{"id": "offers_product", "kind": "predicate"}],
    }))
    assert grid["meta"]["row_count"] == 1
    assert grid["rows"][0]["cells"]["offers_product"]["value"] == "AcmeWidget"


# --------------------------------------------------------------------------- #
# density sort — the "most data first" ordering (fetch pool, rank, THEN cap)   #
# --------------------------------------------------------------------------- #
def _varied_density_population() -> dict:
    """3 companies of increasing density but DECREASING alphabetical rank, so a
    cap-before-rank bug would keep the sparse alphabetically-first one."""
    def _c(name, preds):
        return {
            "entity_id": f"domain:{name.lower()}.com", "name": name, "kind": "company",
            "primary_domain": f"{name.lower()}.com",
            "claims": [
                {"predicate": p, "object_kind": "value", "object_value": f"{name}-{p}",
                 "object_norm": f"{name}-{p}".lower(), "object_entity_id": "",
                 "confidence": 0.9, "evidence": _ev("d", "b", f"{name} {p}")}
                for p in preds
            ],
        }
    return {
        "companies": [
            _c("Aardvark", ["has_founder"]),                                 # density 1
            _c("Middle", ["has_founder", "offers_product"]),                 # density 2
            _c("Zeta", ["has_founder", "offers_product", "headcount"]),      # density 3
        ],
        "meta": {"company_count": 3, "companies_truncated": False,
                 "company_cap": _DENSITY_POOL_CAP, "claims_truncated": False},
    }


def test_density_sort_keeps_the_densest_not_the_alphabetical() -> None:
    store = FakeStore(population=_varied_density_population())
    grid = _run(build_grid(store, {
        "row_kind": "company",
        "columns": [{"id": "has_founder", "kind": "predicate"},
                    {"id": "offers_product", "kind": "predicate"},
                    {"id": "headcount", "kind": "predicate"}],
        "sort": {"by": "density"},
        "cap": 2,
    }))
    # the 2 densest survive the cap, densest-first — Aardvark (alphabetical-first,
    # sparsest) is DROPPED, which the old cap-by-name behaviour would have kept.
    assert [r["name"] for r in grid["rows"]] == ["Zeta", "Middle"]
    assert grid["meta"]["truncated"] is True
    assert any("data density" in n for n in grid["meta"]["notes"])
    # and it fetched a POOL (not just `cap`) before ranking, or the cap couldn't have
    # reached past the alphabetically-first rows.
    assert store.calls["population_claims"]["company_cap"] >= _DENSITY_POOL_CAP


def test_density_ties_break_by_name_deterministically() -> None:
    store = FakeStore(population={
        "companies": [
            {"entity_id": "e:b", "name": "Bravo", "kind": "company", "primary_domain": "",
             "claims": [{"predicate": "has_founder", "object_kind": "value",
                         "object_value": "x", "object_norm": "x", "object_entity_id": "",
                         "confidence": 0.9, "evidence": _ev("d", "b", "Bravo x")}]},
            {"entity_id": "e:a", "name": "Alpha", "kind": "company", "primary_domain": "",
             "claims": [{"predicate": "has_founder", "object_kind": "value",
                         "object_value": "y", "object_norm": "y", "object_entity_id": "",
                         "confidence": 0.9, "evidence": _ev("d", "b", "Alpha y")}]},
        ],
        "meta": {"companies_truncated": False, "claims_truncated": False},
    })
    grid = _run(build_grid(store, {
        "row_kind": "company",
        "columns": [{"id": "has_founder", "kind": "predicate"}],
        "sort": {"by": "density"},
    }))
    # equal density (1 each) → stable ascending name tie-break
    assert [r["name"] for r in grid["rows"]] == ["Alpha", "Bravo"]
