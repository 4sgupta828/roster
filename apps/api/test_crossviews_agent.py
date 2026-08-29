"""Unit tests for the CROSSVIEWS visualizer agent — NO DB, NO LLM.

Covers the CODE-owned grounding guarantee (Rule 18): the SERVER RE-VALIDATION GATE drops
any column the (fake) LLM proposes that is not a real allowed column with coverage>0;
GOAL-FIRST behavior (empty/vague goal → ready=false, asks); a requested-but-missing
column lands in `gaps`, never in the spec; and the no-LLM FAIL-SAFE proposes the
top-covered allowed columns and never a fabricated one. Every proposed spec column is
typed (`predicate`|`edge`|`aggregate`) and backed by coverage>0.

The end-to-end HTTP + DB wiring is exercised by the DSN-gated endpoint test.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from api.crossviews_agent import build_column_catalog, crossview_turn


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeStore:
    """Minimal grounded-store double: company coverage + active registry + the
    person/category read shapes the catalog builder consumes."""

    def __init__(self, *, coverage, active, founders=None, categories=None):
        self._coverage = coverage
        self._active = active
        self._founders = founders or []
        self._categories = categories or []

    async def predicate_coverage(self, *, subject_kind=None, category_norm=None,
                                 tenant_id="demo"):
        return list(self._coverage)

    async def active_predicate_names(self):
        return list(self._active)

    async def founder_rows(self, *, founder_predicate="has_founder",
                           tenant_id="demo", cap=400):
        return list(self._founders)

    async def distinct_categories(self, *, tenant_id="demo", min_members=1):
        return list(self._categories)

    async def close(self):
        pass


class _FakeLLM:
    """`complete` returns a canned plan as `.parsed` (ignores response_format — the agent
    builds the real pydantic model only to constrain a live provider)."""

    def __init__(self, plan):
        self._plan = plan
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=400,
                       temperature=None):
        self.calls += 1
        return SimpleNamespace(parsed=self._plan, output_tokens=5)


def _company_store() -> _FakeStore:
    # operates_in_category (cov 2) and offers_product (cov 1) are BOTH covered AND active.
    return _FakeStore(
        coverage=[{"predicate": "operates_in_category", "rows_covered": 2},
                  {"predicate": "offers_product", "rows_covered": 1}],
        active=[{"name": "operates_in_category", "object_kind": "entity"},
                {"name": "offers_product", "object_kind": "value"}])


def _plan(*, chosen=(), gaps=(), ready=False, reply="ok"):
    gap_objs = [SimpleNamespace(requested=g[0], note=g[1]) for g in gaps]
    return SimpleNamespace(chosen_column_ids=list(chosen), gaps=gap_objs,
                           ready=ready, reply=reply)


def test_gate_drops_non_allowed_column() -> None:
    """The grounding guarantee: an LLM-chosen id that is NOT a real allowed column is
    dropped by the server gate — it never reaches the proposed spec."""
    store = _company_store()
    llm = _FakeLLM(_plan(chosen=["offers_product", "ghost_column"], ready=True,
                         reply="Showing products."))
    out = _run(crossview_turn(store=store, llm=llm, row_kind="company",
                              goal="what products do these companies offer?",
                              current_columns=None, message=None))
    ids = [c["id"] for c in out["proposed_spec"]["columns"]]
    assert ids == ["offers_product"]              # ghost_column dropped by the gate
    assert "ghost_column" not in ids
    assert out["ready"] is True


def test_goal_first_empty_goal_asks() -> None:
    """Empty/vague goal → ready=false and an asking reply; no full table proposed."""
    store = _company_store()
    llm = _FakeLLM(_plan(chosen=[], ready=False,
                         reply="What would you like the table to show?"))
    out = _run(crossview_turn(store=store, llm=llm, row_kind="company",
                              goal="", current_columns=None, message=None))
    assert out["ready"] is False
    assert out["proposed_spec"]["columns"] == []
    assert out["reply"]                            # a non-empty asking reply
    # the full allowed catalog is still returned for the FE picker
    assert {c["id"] for c in out["columns"]} == {"operates_in_category", "offers_product"}


def test_requested_missing_goes_to_gaps_not_spec() -> None:
    """A requested column with no grounded coverage lands in `gaps` (with `closest`
    allowed ids), never as a spec column."""
    store = _company_store()
    llm = _FakeLLM(_plan(chosen=["offers_product"],
                         gaps=[("revenue", "no grounded revenue claims")],
                         ready=True, reply="Revenue isn't collected; showing products."))
    out = _run(crossview_turn(store=store, llm=llm, row_kind="company",
                              goal="show revenue per company", current_columns=None,
                              message=None))
    spec_ids = [c["id"] for c in out["proposed_spec"]["columns"]]
    assert "revenue" not in spec_ids
    assert spec_ids == ["offers_product"]
    assert len(out["gaps"]) == 1
    gap = out["gaps"][0]
    assert gap["requested"] == "revenue"
    assert isinstance(gap["closest"], list)
    # closest is drawn ONLY from the allowed catalog
    assert set(gap["closest"]).issubset({"operates_in_category", "offers_product"})


def test_no_llm_failsafe_proposes_top_covered() -> None:
    """No llm → a deterministic top-covered proposal, ready=false, NEVER a fabricated
    column (every proposed id is a real allowed column)."""
    store = _company_store()
    out = _run(crossview_turn(store=store, llm=None, row_kind="company",
                              goal="anything", current_columns=None, message=None))
    assert out["ready"] is False
    ids = [c["id"] for c in out["proposed_spec"]["columns"]]
    assert ids                                     # non-empty starter set
    allowed_ids = {c["id"] for c in out["columns"]}
    assert set(ids).issubset(allowed_ids)          # no fabricated column
    # top-covered first (operates_in_category has coverage 2 > offers_product's 1)
    assert ids[0] == "operates_in_category"


def test_proposed_columns_all_typed_and_covered() -> None:
    """Every proposed spec column is typed for build_grid AND backed by coverage>0."""
    store = _company_store()
    llm = _FakeLLM(_plan(chosen=["operates_in_category", "offers_product"], ready=True,
                         reply="Category + product."))
    out = _run(crossview_turn(store=store, llm=llm, row_kind="company",
                              goal="map companies by category and product",
                              current_columns=None, message=None))
    cov_by_id = {c["id"]: c["coverage"] for c in out["columns"]}
    for col in out["proposed_spec"]["columns"]:
        assert col["kind"] in ("predicate", "edge", "aggregate")
        assert cov_by_id.get(col["id"], 0) > 0


def test_catalog_intersects_coverage_with_active_registry() -> None:
    """A predicate with coverage but NOT in the active registry is not offered (only real
    active predicates with data are allowed)."""
    store = _FakeStore(
        coverage=[{"predicate": "offers_product", "rows_covered": 3},
                  {"predicate": "retired_pred", "rows_covered": 5}],
        active=[{"name": "offers_product", "object_kind": "value"}])
    cols = _run(build_column_catalog(store, row_kind="company"))
    ids = {c["id"] for c in cols}
    assert ids == {"offers_product"}               # retired_pred excluded (not active)


def test_person_catalog_typed_edge_and_aggregate() -> None:
    """person columns are the fixed CV1 edge/aggregate set, coverage = grounded founder
    count, correctly typed for build_grid."""
    store = _FakeStore(
        coverage=[], active=[],
        founders=[{"person_id": "p1", "name": "Jane", "companies": [{"company_id": "c1"}]}])
    cols = _run(build_column_catalog(store, row_kind="person"))
    by_id = {c["id"]: c for c in cols}
    assert by_id["companies_founded"]["col_kind"] == "edge"
    assert by_id["n_companies"]["col_kind"] == "aggregate"
    assert all(c["coverage"] == 1 for c in cols)   # one grounded founder


def test_person_catalog_empty_when_no_founders() -> None:
    """No grounded founders → no allowed columns (coverage 0 is never offered)."""
    store = _FakeStore(coverage=[], active=[], founders=[])
    cols = _run(build_column_catalog(store, row_kind="person"))
    assert cols == []
