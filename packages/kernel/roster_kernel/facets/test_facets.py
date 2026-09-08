"""TDD for the kernel facet mechanics (docs/specs/facet-contract-evaluator.md §2, §4, §5, §9).

Pure, no DB, no model. The schema here is a TEST vocabulary — the kernel never names a domain; the real
vocabulary lives in the vertical."""
from __future__ import annotations

import asyncio

import pytest

from roster_kernel.facets import (UNKNOWN, Contract, FacetKey, FacetSchema, FacetType, FacetWeights,
                                  InMemoryFacetStore, count_rows, edit, evaluate, matches_must, state_of,
                                  validate_contract)


def _schema() -> FacetSchema:
    return FacetSchema(keys=(
        FacetKey(key="colour", type=FacetType.categorical, kinds=("thing",), label="Colour", values=("red", "green", "blue")),
        FacetKey(key="size", type=FacetType.ordinal, kinds=("thing",), label="Size", values=("xs", "s", "m", "l", "xl")),
        FacetKey(key="price", type=FacetType.numeric, kinds=("thing",), label="Price", unit="usd",
                 bands=(("under_10", None, 10.0), ("10_to_50", 10.0, 50.0), ("50_plus", 50.0, None))),
        FacetKey(key="tags", type=FacetType.set, kinds=("thing",), label="Tags", top_n=3),
        FacetKey(key="place", type=FacetType.hierarchical, kinds=("thing", "maker"), label="Place"),
        FacetKey(key="maker_tier", type=FacetType.categorical, kinds=("thing",), label="Maker tier", values=("a", "b"), via="maker"),
    ))


def _rows():
    # facets: key → list of normalized values; numeric raw values ride under "numeric"; "sim" = semantic similarity
    return [
        {"id": "t1", "kind": "thing", "sim": 0.62, "facets": {"colour": ["red"], "size": ["m"], "price": ["10_to_50"], "tags": ["wool", "warm"], "place": ["eu/de/berlin"], "maker_tier": ["a"]}, "numeric": {"price": 20.0}},
        {"id": "t2", "kind": "thing", "sim": 0.60, "facets": {"colour": ["green"], "size": ["xl"], "price": ["50_plus"], "tags": ["wool"], "place": ["us/ca/bay_area"]}, "numeric": {"price": 80.0}},
        {"id": "t3", "kind": "thing", "sim": 0.58, "facets": {"colour": ["red"], "size": ["s"], "tags": ["cotton"], "place": ["us/ny/nyc"]}},                      # price unknown
        {"id": "t4", "kind": "thing", "sim": 0.55, "facets": {"colour": ["blue"], "price": ["under_10"], "place": ["eu/fr/paris"]}, "numeric": {"price": 5.0}},  # size unknown
        {"id": "t5", "kind": "thing", "sim": 0.50, "facets": {"colour": ["red", "blue"], "size": ["l"], "price": ["10_to_50"], "tags": ["warm", "cotton"], "place": ["us/ca/la"]}, "numeric": {"price": 30.0}},
    ]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------- schema ----------------

def test_schema_validates_values_by_type_and_never_guesses():
    s = _schema()
    assert s.validate_value("colour", "Red") == "red"                    # normalized
    assert s.validate_value("colour", "purple") is None                  # off-vocabulary → caller stores UNKNOWN
    assert s.validate_value("size", "XL") == "xl"
    assert s.validate_value("tags", "  Merino Wool ") == "merino wool"  # sets keep normalized free text
    assert s.validate_value("place", "US/CA/Bay_Area") == "us/ca/bay_area"
    assert s.validate_value("price", "10_to_50") == "10_to_50" and s.validate_value("price", "cheap") is None
    assert s.validate_value("nope", "x") is None
    assert UNKNOWN == "unknown"


def test_ordinal_distance_bands_and_hierarchy():
    s = _schema()
    assert s.ordinal_distance("size", "m", "m") == 0 and s.ordinal_distance("size", "xs", "xl") == 4
    assert s.ordinal_distance("size", "m", UNKNOWN) is None and s.ordinal_distance("colour", "red", "blue") is None
    assert s.band_of("price", 5) == "under_10" and s.band_of("price", 10) == "10_to_50" and s.band_of("price", 500) == "50_plus"
    assert s.band_of("price", None) == UNKNOWN
    assert s.contains("place", "us/ca/bay_area", "us") and s.contains("place", "us/ca/bay_area", "us/ca")
    assert not s.contains("place", "us/ca/bay_area", "us/ny") and not s.contains("place", "usa", "us")


def test_schema_version_is_stable_and_changes_with_vocabulary():
    s1, s2 = _schema(), _schema()
    assert s1.version() == s2.version() and len(s1.version()) == 12
    s3 = FacetSchema(keys=tuple(k if k.key != "colour" else FacetKey(key="colour", type=FacetType.categorical, kinds=("thing",), label="Colour", values=("red", "green", "blue", "pink")) for k in s1.keys))
    assert s3.version() != s1.version()


def test_prompt_block_renders_every_navigable_key_for_the_kind():
    s = _schema()
    block = s.to_prompt_block("thing")
    for k in ("colour", "size", "price", "tags", "place"):
        assert k in block
    assert "xs < s < m < l < xl" in block            # ordinal order is shown in order
    assert "maker_tier" not in block                 # a via-relation key is not extracted from the entity's own text
    assert s.for_kind("maker") and [k.key for k in s.for_kind("maker")] == ["place"]


# ---------------- contract ----------------

def test_contract_canonical_serialization_and_validation():
    s = _schema()
    c = Contract(kind="thing", text="warm red things", must={"colour": ["red"]}, prefer={"tags": ["warm"]})
    d = c.to_dict()
    assert list(d.keys()) == sorted(d.keys())                       # canonical key order
    assert Contract.from_dict(d).to_dict() == d
    assert validate_contract(c, s) == []
    bad = Contract(kind="thing", must={"colour": ["purple"], "nope": ["x"]}, center={"key": "colour", "value": "red", "span": 1})
    errs = validate_contract(bad, s)
    assert any("purple" in e for e in errs) and any("nope" in e for e in errs) and any("center" in e and "ordinal" in e for e in errs)


def test_edit_cycles_off_must_prefer_avoid_off_and_is_pure():
    s = _schema()
    c0 = Contract(kind="thing")
    c1 = edit(c0, "colour", "red", "cycle")
    assert state_of(c1, "colour", "red") == "must" and state_of(c0, "colour", "red") == "off"   # c0 untouched
    c2 = edit(c1, "colour", "red", "cycle"); assert state_of(c2, "colour", "red") == "prefer" and "colour" not in c2.must
    c3 = edit(c2, "colour", "red", "cycle"); assert state_of(c3, "colour", "red") == "avoid"
    c4 = edit(c3, "colour", "red", "cycle"); assert state_of(c4, "colour", "red") == "off" and not c4.avoid
    c5 = edit(c0, "colour", "red", "prefer"); assert state_of(c5, "colour", "red") == "prefer"
    assert validate_contract(c5, s) == []
    with pytest.raises(ValueError):
        edit(c0, "colour", "red", "sideways")


# ---------------- must semantics ----------------

def test_must_is_or_within_and_across_keys_and_excludes_unknown():
    s = _schema()
    rows = {r["id"]: r for r in _rows()}
    assert matches_must(rows["t1"], {"colour": ["red", "green"]}, s)
    assert matches_must(rows["t5"], {"colour": ["blue"]}, s)                        # multi-valued row
    assert not matches_must(rows["t2"], {"colour": ["red"], "size": ["xl"]}, s)     # AND across keys
    assert not matches_must(rows["t4"], {"size": ["s", "m"]}, s)                    # unknown never satisfies a must
    assert matches_must(rows["t2"], {"price": {"min": 60}}, s) and not matches_must(rows["t1"], {"price": {"min": 60}}, s)
    assert not matches_must(rows["t3"], {"price": {"min": 0}}, s)                   # unknown numeric fails a range
    assert matches_must(rows["t1"], {"price": ["10_to_50"]}, s)                     # a band list works too
    assert matches_must(rows["t2"], {"place": ["us"]}, s) and not matches_must(rows["t1"], {"place": ["us/ca"]}, s)
    assert matches_must(rows["t1"], {"tags": ["warm", "silk"]}, s) and not matches_must(rows["t4"], {"tags": ["warm"]}, s)


# ---------------- counts ----------------

def test_counts_cover_every_type_include_unknown_and_reflect_the_must_filtered_pool():
    s = _schema()
    c = count_rows(_rows(), s, "thing")
    assert c["colour"] == {"red": 3, "green": 1, "blue": 2}                  # a multi-valued row counts once per value
    assert c["size"] == {"m": 1, "xl": 1, "s": 1, "l": 1, UNKNOWN: 1}
    assert c["price"] == {"10_to_50": 2, "50_plus": 1, "under_10": 1, UNKNOWN: 1}
    assert c["tags"] == {"wool": 2, "warm": 2, "cotton": 2}                 # top_n=3 keeps the three most common
    assert c["place"] == {"eu/de/berlin": 1, "us/ca/bay_area": 1, "us/ny/nyc": 1, "eu/fr/paris": 1, "us/ca/la": 1}
    assert count_rows(_rows(), s, "thing", depth={"place": 1})["place"] == {"eu": 2, "us": 3}
    pool = [r for r in _rows() if matches_must(r, {"colour": ["red"]}, s)]
    assert count_rows(pool, s, "thing")["size"] == {"m": 1, "s": 1, "l": 1}   # counts follow the musts


# ---------------- evaluate ----------------

def test_evaluate_filters_ranks_explains_and_is_deterministic():
    s = _schema(); store = InMemoryFacetStore(_rows())
    w = FacetWeights(default_prefer=0.10, default_avoid=0.10, center_per_step=0.06)
    c = Contract(kind="thing", text="q", must={"colour": ["red"]}, prefer={"tags": ["warm"]}, avoid={"size": ["s"]},
                 center={"key": "size", "value": "m", "span": 1}, limit=10)
    out = _run(evaluate(c, store, s, w, noise_floor=0.40))
    ids = [r["id"] for r in out["rows"]]
    assert set(ids) == {"t1", "t3", "t5"}                                    # must honoured; no unknown-colour rows
    assert ids[0] == "t1"                                                    # sim + prefer(warm) + centre exact
    t3 = next(r for r in out["rows"] if r["id"] == "t3")
    assert "avoid" in " ".join(t3["reasons"]).lower() and t3["score"] < 0.58   # avoided size moved it down
    t5 = next(r for r in out["rows"] if r["id"] == "t5")
    assert any("size" in x for x in t5["reasons"])                           # centre distance explained
    assert all(1 <= r["match_pct"] <= 99 for r in out["rows"])
    assert out["counts"]["size"] == {"m": 1, "s": 1, "l": 1} and out["counts"]["colour"]["red"] == 3
    assert out["coverage"]["pool"] == 3 and out["coverage"]["unknown"]["size"] == 0
    again = _run(evaluate(c, store, s, w, noise_floor=0.40))
    assert [r["id"] for r in again["rows"]] == ids and again["counts"] == out["counts"]


def test_evaluate_unknown_is_neutral_for_prefer_avoid_and_centre():
    s = _schema(); store = InMemoryFacetStore(_rows())
    c = Contract(kind="thing", text="q", center={"key": "size", "value": "m", "span": 1}, avoid={"size": ["xl"]}, limit=10)
    out = _run(evaluate(c, store, s, FacetWeights(), noise_floor=0.40))
    t4 = next(r for r in out["rows"] if r["id"] == "t4")                    # size unknown
    assert abs(t4["score"] - 0.55) < 1e-9 and any("unknown" in x for x in t4["reasons"])
    assert out["coverage"]["unknown"]["size"] == 1


def test_rank_by_numeric_and_ordinal_put_unknown_last():
    s = _schema(); store = InMemoryFacetStore(_rows())
    by_price = _run(evaluate(Contract(kind="thing", text="q", rank_by="price", limit=10), store, s, FacetWeights()))
    assert [r["id"] for r in by_price["rows"]] == ["t2", "t5", "t1", "t4", "t3"]
    by_size = _run(evaluate(Contract(kind="thing", text="q", rank_by="size", limit=10), store, s, FacetWeights()))
    assert [r["id"] for r in by_size["rows"]][:2] == ["t2", "t5"] and [r["id"] for r in by_size["rows"]][-1] == "t4"


def test_leaky_pool_invariant_holds_even_when_a_store_returns_extra_rows():
    """A store that ignores musts (or a keyword leg that widens the pool) cannot leak rows past the contract."""
    s = _schema()

    class LeakyStore(InMemoryFacetStore):
        async def semantic(self, kind, text, must, *, cap=400):
            return [dict(r) for r in self._rows if r["kind"] == kind]      # ignores `must` on purpose

    out = _run(evaluate(Contract(kind="thing", text="q", must={"size": ["m", "l"]}, limit=10), LeakyStore(_rows()), s, FacetWeights()))
    assert {r["id"] for r in out["rows"]} == {"t1", "t5"}


def test_via_relation_facets_filter_and_count_like_own_facets():
    s = _schema(); store = InMemoryFacetStore(_rows())
    out = _run(evaluate(Contract(kind="thing", text="q", must={"maker_tier": ["a"]}, limit=10), store, s, FacetWeights()))
    assert [r["id"] for r in out["rows"]] == ["t1"] and out["counts"]["maker_tier"] == {"a": 1}
    assert count_rows(_rows(), s, "thing")["maker_tier"] == {"a": 1, UNKNOWN: 4}


def test_with_text_the_pool_is_the_semantic_neighbourhood_only():
    """A must + text never pulls unrelated rows in through the enumerate leg (prod 2026-09-05: rank_by level put
    'Store Manager' first because enumerate added it with sim 0)."""
    import asyncio
    from roster_kernel.facets import Contract, FacetKey, FacetSchema, FacetType, InMemoryFacetStore, evaluate
    sch = FacetSchema(keys=(FacetKey(key="lv", type=FacetType.ordinal, kinds=("e",), values=("a", "b", "c")), FacetKey(key="c", type=FacetType.set, kinds=("e",))))
    rows = [{"id": "r1", "kind": "e", "sim": 0.9, "facets": {"lv": ["a"], "c": ["us"]}}, {"id": "r2", "kind": "e", "sim": 0.0, "facets": {"lv": ["c"], "c": ["us"]}}]
    class Store(InMemoryFacetStore):
        async def semantic(self, kind, text, must, *, cap=400):
            return [dict(r) for r in self._filtered(kind, must) if float(r.get("sim") or 0) > 0][:cap]
    out = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", text="q", must={"c": ["us"]}, rank_by="lv"), Store(rows, sch), sch))
    assert [r["id"] for r in out["rows"]] == ["r1"] and out["coverage"]["legs"]["enumerate"] == 0
    out2 = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", must={"c": ["us"]}, rank_by="lv"), Store(rows, sch), sch))
    assert [r["id"] for r in out2["rows"]] == ["r2", "r1"]                                  # no text → enumerate, ordinal desc


def test_an_empty_slice_names_the_must_that_empties_it():
    import asyncio
    from roster_kernel.facets import Contract, FacetKey, FacetSchema, FacetType, InMemoryFacetStore, evaluate
    sch = FacetSchema(keys=(FacetKey(key="lv", type=FacetType.ordinal, kinds=("e",), values=("a", "b", "c")),
                            FacetKey(key="ct", type=FacetType.categorical, kinds=("e",), values=("x", "y")),
                            FacetKey(key="m", type=FacetType.set, kinds=("e",))))
    rows = [{"id": f"r{i}", "kind": "e", "sim": 0.5, "facets": {"lv": ["a"], "ct": ["x"], "m": ["p"]}} for i in range(20)]
    rows += [{"id": "z", "kind": "e", "sim": 0.5, "facets": {"lv": ["c"], "ct": ["y"], "m": ["q"]}}]
    st = InMemoryFacetStore(rows, sch)
    out = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", must={"lv": ["a"], "ct": ["y"], "m": ["p"]}), st, sch))
    assert out["rows"] == []
    d = out["coverage"]["diagnosis"]
    assert d["pool"] == 0 and d["keys"][0]["key"] == "ct" and d["keys"][0]["without"] == 20 and d["keys"][0]["alone"] == 1
    assert {k["key"] for k in d["keys"]} == {"lv", "ct", "m"}
    ok = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", must={"lv": ["a"]}), st, sch))
    assert "diagnosis" not in ok["coverage"]                                           # a healthy slice is not diagnosed


def test_weak_results_are_diagnosed_and_flagged():
    import asyncio
    from roster_kernel.facets import Contract, FacetKey, FacetSchema, FacetType, InMemoryFacetStore, evaluate
    sch = FacetSchema(keys=(FacetKey(key="ct", type=FacetType.categorical, kinds=("e",), values=("x", "y")),))
    rows = [{"id": f"r{i}", "kind": "e", "sim": 0.42, "facets": {"ct": ["x"]}} for i in range(6)]          # barely above the floor → weak
    out = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", text="q", must={"ct": ["x"]}), InMemoryFacetStore(rows, sch), sch))
    assert out["rows"] and out["coverage"]["weak"] and out["coverage"]["diagnosis"]["reason"] == "weak"
    strong = [dict(r, sim=0.7) for r in rows]
    out2 = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", text="q", must={"ct": ["x"]}), InMemoryFacetStore(strong, sch), sch))
    assert not out2["coverage"]["weak"] and "diagnosis" not in out2["coverage"]


def test_preference_points_are_bounded_but_a_strong_preference_can_lift_a_close_row():
    import asyncio
    from roster_kernel.facets import Contract, FacetKey, FacetSchema, FacetType, FacetWeights, InMemoryFacetStore, evaluate
    sch = FacetSchema(keys=(FacetKey(key="ev", type=FacetType.categorical, kinds=("e",), values=("r", "p")), FacetKey(key="fd", type=FacetType.categorical, kinds=("e",), values=("m", "s"))))
    rows = [{"id": "close", "kind": "e", "sim": 0.55, "facets": {"fd": ["s"]}},                     # 60 %
            {"id": "far_pref", "kind": "e", "sim": 0.46, "facets": {"ev": ["r", "p"]}},             # 24 % + two small hits
            {"id": "near_field", "kind": "e", "sim": 0.53, "facets": {"fd": ["m"]}}]                # 52 % + the strong field hit
    w = FacetWeights(prefer={"ev": 0.10, "fd": 0.25})
    out = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", text="q", prefer={"ev": ["r", "p"], "fd": ["m"]}), InMemoryFacetStore(rows, sch), sch, w))
    ids = [r["id"] for r in out["rows"]]
    assert ids[-1] == "far_pref"                       # 24 + 10 = 34 never passes 60
    assert ids[0] == "near_field"                      # 52 + 12.5 = 64.5 passes 60: a strong preference lifts a close row


def test_a_preferred_value_reaches_the_pool_through_its_own_leg():
    import asyncio
    from roster_kernel.facets import Contract, FacetKey, FacetSchema, FacetType, InMemoryFacetStore, evaluate
    sch = FacetSchema(keys=(FacetKey(key="fd", type=FacetType.categorical, kinds=("e",), values=("m", "s")),))
    rows = [{"id": f"s{i}", "kind": "e", "sim": 0.7 - i * 0.0005, "facets": {"fd": ["s"]}} for i in range(300)]
    rows.append({"id": "mkt", "kind": "e", "sim": 0.50, "facets": {"fd": ["m"]}})                  # outside the nearest 200
    calls = []
    class Store(InMemoryFacetStore):
        async def semantic(self, kind, text, must, *, cap=400):
            calls.append((dict(must), cap)); return await super().semantic(kind, text, must, cap=cap)
    out = asyncio.new_event_loop().run_until_complete(evaluate(Contract(kind="e", text="q", prefer={"fd": ["m"]}, limit=10), Store(rows, sch), sch))
    assert any(m.get("fd") == ["m"] for m, _ in calls) and out["coverage"]["legs"]["prefer"] >= 1
    assert out["coverage"]["pool"] == 161                                                  # the nearest 160 (the neighbourhood floor) plus the marketing row its own leg brought in


def test_weak_diagnosis_reports_the_best_match_without_each_must():
    import asyncio
    from roster_kernel.facets import Contract, FacetKey, FacetSchema, FacetType, InMemoryFacetStore, diagnose_musts
    sch = FacetSchema(keys=(FacetKey(key="ct", type=FacetType.categorical, kinds=("e",), values=("x", "y")), FacetKey(key="m", type=FacetType.set, kinds=("e",))))
    rows = [{"id": "good", "kind": "e", "sim": 0.6, "facets": {"ct": ["y"], "m": ["a"]}}, {"id": "meh", "kind": "e", "sim": 0.42, "facets": {"ct": ["x"], "m": ["a"]}}]
    st = InMemoryFacetStore(rows, sch)
    d = asyncio.new_event_loop().run_until_complete(diagnose_musts(Contract(kind="e", text="q", must={"ct": ["x"], "m": ["a"]}), lambda k, m: st.counts(k, m, sch), sch,
                                                                    semantic_fn=lambda k, t, m: st.semantic(k, t, m)))
    assert d["keys"][0]["key"] == "ct" and d["keys"][0]["best_without"] == 80 and d["keys"][1]["best_without"] == 8


def test_a_text_search_whose_semantic_leg_is_unavailable_falls_back_to_the_filters_and_says_so():
    """Prod 2026-09-07: the embedding provider ran out of credit; every semantic leg returned [] and every text search
    silently answered zero. The filters must still answer, labelled degraded."""
    import asyncio
    from roster_kernel.facets import Contract, InMemoryFacetStore, evaluate
    from roster_kernel.facets.schema import FacetKey, FacetSchema, FacetType
    sch = FacetSchema(keys=(FacetKey(key="k1", type=FacetType.categorical, kinds=("e",), label="K1", values=("a", "b")),))
    rows = [{"id": f"r{i}", "kind": "e", "sim": 0.5, "facets": {"k1": ["a" if i % 2 else "b"]}} for i in range(20)]

    class NoEmbed(InMemoryFacetStore):
        async def semantic(self, kind, text, must, *, cap=400):
            return []                                                     # the provider is down
    run = lambda c: asyncio.new_event_loop().run_until_complete(c)
    out = run(evaluate(Contract(kind="e", text="anything", must={"k1": ["a"]}), NoEmbed(rows, sch), sch))
    assert len(out["rows"]) == 10 and out["coverage"]["degraded"] == "semantic_unavailable" and out["coverage"]["legs"]["enumerate"] == 10
    # the preferences still shape the degraded pool: their own slices join it, so the right rows can rank first
    seen = []
    class Spy(NoEmbed):
        async def enumerate(self, kind, must, *, cap=400):
            seen.append(dict(must)); return await super().enumerate(kind, must, cap=cap)
    out_p = run(evaluate(Contract(kind="e", text="anything", prefer={"k1": ["b"]}), Spy(rows, sch), sch))
    assert seen == [{}, {"k1": ["b"]}] and out_p["coverage"]["legs"]["prefer"] == 10 and out_p["rows"][0]["facets"]["k1"] == ["b"]
    # a must that genuinely matches nothing still returns nothing — and is not called degraded
    only_b = [r for r in rows if r["facets"]["k1"] == ["b"]]
    out2 = run(evaluate(Contract(kind="e", text="anything", must={"k1": ["a"]}), NoEmbed(only_b, sch), sch))
    assert out2["rows"] == [] and "degraded" not in out2["coverage"]
    # a healthy semantic leg is untouched (no enumerate)
    out3 = run(evaluate(Contract(kind="e", text="anything", must={"k1": ["a"]}), InMemoryFacetStore(rows, sch), sch))
    assert out3["coverage"]["legs"]["enumerate"] == 0 and "degraded" not in out3["coverage"]


def test_counts_only_depth_skips_every_row_leg_and_still_draws_the_rail():
    """depth {'rows': False}: the navigable counts come back, the store's row legs are never asked. A surface
    that already has rows on screen (a saved map's snapshot) draws its chips without re-running the search."""
    s = _schema()

    class NoRows(InMemoryFacetStore):
        async def semantic(self, kind, text, must, cap=200):        # noqa: D102
            raise AssertionError("a counts-only evaluate must not run a semantic leg")

        async def enumerate(self, kind, must, cap=200):             # noqa: A003, D102
            raise AssertionError("a counts-only evaluate must not enumerate rows")

    c = Contract(kind="thing", text="q", must={"colour": ["green"]}, prefer={"tags": ["wool"]}, limit=10)
    out = _run(evaluate(c, NoRows(_rows()), s, depth={"rows": False}))
    full = _run(evaluate(c, InMemoryFacetStore(_rows()), s, noise_floor=0.40))
    assert out["rows"] == []
    assert out["counts"] == full["counts"]                          # the chips are identical to the full search's
    assert out["contract"] == full["contract"]
    assert out["coverage"]["counts_only"] is True
    assert out["coverage"]["slice"] == full["coverage"]["pool"] == 1   # the must-slice, read off the counts
    assert "pool" not in out["coverage"]                            # no rows were ranked: none is claimed
