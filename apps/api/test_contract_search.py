"""Index-aware compile (spec §12 step 1): deterministic corrections measured against a fake index."""
from __future__ import annotations

import asyncio

from roster_kernel.facets import Contract
from roster_vertical.facet_schema import FACET_SCHEMA

from api.contract_search import index_aware


def _run(c):
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    return loop.run_until_complete(c)


def _fake_index(sizes: dict, dists: dict):
    async def slice_fn(kind, must):
        key = tuple(sorted((k, tuple(v) if isinstance(v, list) else str(v)) for k, v in must.items()))
        return sizes.get(key, 50000)
    async def counts_fn(kind, must):
        key = tuple(sorted((k, tuple(v) if isinstance(v, list) else str(v)) for k, v in must.items()))
        return dists.get(key, {})
    return slice_fn, counts_fn


def test_a_place_named_with_a_mode_ranks_never_filters_unless_the_user_set_it():
    c = Contract(kind="person", text="remote us or seattle", must={"metro": ["seattle"], "country": ["us"]})
    slice_fn, counts_fn = _fake_index({}, {})
    c2, notes = _run(index_aware(c, kind="person", schema=FACET_SCHEMA, user_keys=set(), slice_fn=slice_fn, counts_fn=counts_fn, coverage={}, place_or_mode=True))
    assert "metro" not in c2.must and c2.prefer["metro"] == ["seattle"] and notes[0]["rule"] == "place_or_mode"
    c3, _ = _run(index_aware(c, kind="person", schema=FACET_SCHEMA, user_keys={"metro"}, slice_fn=slice_fn, counts_fn=counts_fn, coverage={}, place_or_mode=True))
    assert c3.must["metro"] == ["seattle"]                                                         # the user's own chip holds
    # the compile may already have left the place under prefer (or dropped it): the reading is still on the card
    c4, n4 = _run(index_aware(Contract(kind="person", text="remote us or seattle", must={"country": ["us"]}, prefer={"metro": ["seattle"]}), kind="person", schema=FACET_SCHEMA,
                              user_keys=set(), slice_fn=slice_fn, counts_fn=counts_fn, coverage={}, place_or_mode=True))
    assert "metro" not in c4.must and n4[0]["rule"] == "place_or_mode" and n4[0]["action"] == "ranks" and n4[0]["values"] == ["seattle"]


def test_a_collapsing_compiled_must_is_demoted_only_under_scarcity_and_on_a_relaxable_key():
    must = {"field": ["software"], "skill": ["kafka", "ledger"], "metro": ["seattle"], "country": ["us"]}
    K = lambda m: tuple(sorted((k, tuple(v)) for k, v in m.items()))
    sizes = {K(must): 4, K({k: v for k, v in must.items() if k != "skill"}): 900, K({k: v for k, v in must.items() if k != "metro"}): 60,
             K({k: v for k, v in must.items() if k != "field"}): 8, K({k: v for k, v in must.items() if k != "country"}): 6}
    slice_fn, counts_fn = _fake_index(sizes, {})
    c = Contract(kind="person", text="x", must=dict(must))
    c2, notes = _run(index_aware(c, kind="person", schema=FACET_SCHEMA, user_keys={"country"}, slice_fn=slice_fn, counts_fn=counts_fn, coverage={"field": 0.7, "metro": 0.6}))
    assert "skill" not in c2.must and sorted(c2.prefer["skill"]) == ["kafka", "ledger"]              # set key, 4 → 900: collapsing
    assert c2.must["metro"] == ["seattle"]                                                         # 4 → 60 (15×) but a place is a promise; only a mode relaxes it
    assert c2.must["field"] == ["software"] and c2.must["country"] == ["us"]
    # a decisive rare must on a well-covered key is never demoted even when the pool is tiny
    sizes2 = {K({"field": ["software"], "specialty": ["compilers"]}): 30, K({"field": ["software"]}): 120000, K({"specialty": ["compilers"]}): 35}
    slice_fn2, counts_fn2 = _fake_index(sizes2, {})
    c3, notes3 = _run(index_aware(Contract(kind="person", text="compiler engineers", must={"field": ["software"], "specialty": ["compilers"]}), kind="person", schema=FACET_SCHEMA,
                                  user_keys=set(), slice_fn=slice_fn2, counts_fn=counts_fn2, coverage={"field": 0.7}))
    assert c3.must["specialty"] == ["compilers"]                                                    # 30 people is a valid small answer (≥ the viability floor), not a collapse


def test_cooccurrence_corrects_a_field_the_carriers_do_not_support_and_records_readings():
    K = lambda m: tuple(sorted((k, tuple(v)) for k, v in m.items()))
    dists = {K({}): {"field": {"software": 300000, "marketing": 20000, "data_ml": 60000, "unknown": 100000}},
             K({"specialty": ["crm"]}): {"field": {"software": 620, "marketing": 40, "data_ml": 90, "unknown": 200}}}
    slice_fn, counts_fn = _fake_index({}, dists)
    c = Contract(kind="person", text="marketing engineering manager crm", must={"field": ["marketing"], "country": ["us"]}, prefer={"specialty": ["crm"], "work_type": ["manager"]})
    c2, notes = _run(index_aware(c, kind="person", schema=FACET_SCHEMA, user_keys=set(), slice_fn=slice_fn, counts_fn=counts_fn, coverage={"field": 0.7}))
    assert c2.must["field"] == ["software"]                                                         # marketing holds 5 % of crm carriers; software 83 % with lift
    fix = next(n for n in notes if n["rule"] == "cooccurrence" and n.get("key") == "field")
    assert "software" in fix["action"] and "crm" in fix["why"]
    rd = next(n for n in notes if n["rule"] == "readings"); assert rd["readings"][0]["field"] == "software" and rd["readings"][0]["support"] == 620
    # the user's own field is never corrected
    c3, _ = _run(index_aware(c, kind="person", schema=FACET_SCHEMA, user_keys={"field"}, slice_fn=slice_fn, counts_fn=counts_fn, coverage={"field": 0.7}))
    assert c3.must["field"] == ["marketing"]
