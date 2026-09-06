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


def test_a_preferred_work_type_that_contradicts_the_stated_level_is_dropped_everywhere_with_a_note():
    c = Contract(kind="person", text="hire a cto", must={"field": ["software"], "level": ["leadership"]}, prefer={"work_type": ["ic"], "function": ["engineering"]})
    slice_fn, counts_fn = _fake_index({}, {})
    c2, notes = _run(index_aware(c, kind="person", schema=FACET_SCHEMA, user_keys=set(), slice_fn=slice_fn, counts_fn=counts_fn, coverage={}))
    assert "work_type" not in c2.prefer and notes[0]["rule"] == "tier" and notes[0]["values"] == ["ic"]
    c3, n3 = _run(index_aware(Contract(kind="person", text="x", must={"field": ["software"]}, center={"key": "level", "value": "leadership", "span": 1}, prefer={"work_type": ["ic", "executive"]}),
                              kind="person", schema=FACET_SCHEMA, user_keys=set(), slice_fn=slice_fn, counts_fn=counts_fn, coverage={}))
    assert c3.prefer["work_type"] == ["executive"]
    c4, n4 = _run(index_aware(Contract(kind="person", text="x", must={"level": ["leadership"]}, prefer={"work_type": ["ic"]}), kind="person", schema=FACET_SCHEMA,
                              user_keys={"work_type"}, slice_fn=slice_fn, counts_fn=counts_fn, coverage={}))
    assert c4.prefer["work_type"] == ["ic"] and not n4                                          # the user's own chip holds


def _merged_fixture():
    """A fake index where the strict recipe finds two rows, the relaxed recipe finds six more (two of them the
    judge calls fits, one a 'no'), and a field reading finds one row nobody else has."""
    from api.contract_search import merged_search
    calls = {"evaluate": [], "judge": 0, "slices": [], "log": []}
    rows = {
        "strict": [{"id": "p1", "facets": {"field": ["software"], "level": ["leadership"], "evidence": ["repos"]}}, {"id": "p2", "facets": {"field": ["software"]}}],
        "relaxed:skill": [{"id": "p1", "facets": {}}, {"id": "p3", "facets": {}}, {"id": "p4", "facets": {}}, {"id": "p5", "facets": {}}, {"id": "p6", "facets": {}}, {"id": "p7", "facets": {}}],
        "reading:field=marketing": [{"id": "p9", "facets": {"field": ["marketing"]}}],
        "default": [{"id": "p1", "facets": {}}, {"id": "p8", "facets": {}}],
    }
    def name_of(cd):
        if cd["must"].get("field") == ["marketing"]:
            return "reading:field=marketing"
        if "skill" in cd["must"]:
            return "strict" if "field" in cd["must"] else "default"
        return "relaxed:skill"
    async def evaluate_fn(cd, depth=None):
        calls["evaluate"].append({**cd, "_depth": depth}); n = name_of(cd)
        return {"rows": rows[n], "counts": {"field": {"software": 5}}, "coverage": {"pool": len(rows[n])}, "contract": cd, "labels": {"values": {}}}
    async def slice_fn(kind, must):
        calls["slices"].append(dict(must)); return 0 if must.get("field") == ["nowhere"] else 10
    def llm(system, user):
        calls["judge"] += 1
        assert "WORDS: hire a cto" in user and "[r" in user and "strict" not in user             # blind: no recipe names
        ids = {}
        for line in user.split("ROWS:", 1)[1].strip().splitlines():
            bid = line.split("]")[0].strip("[")
            ids[bid] = line
        # the judge reads the lines; the test maps by the person line we passed (role line = the id)
        out = []
        for bid, line in ids.items():
            rid = line.split("] ", 1)[1].split(" · ")[0]
            fit = {"p1": "yes", "p3": "yes", "p4": "partial", "p5": "no", "p9": "yes"}.get(rid, "partial")
            out.append({"id": bid, "fit": fit, "why": f"{rid} {fit}"})
        return {"verdicts": out}
    async def lines_fn(ids):
        return {i: f"{i} — words" for i in ids}
    async def log_fn(rec):
        calls["log"].append(rec)
    return merged_search, calls, evaluate_fn, slice_fn, llm, lines_fn, log_fn


def test_merged_search_fuses_survivors_judges_the_head_blind_and_orders_fits_first():
    merged_search, calls, evaluate_fn, slice_fn, llm, lines_fn, log_fn = _merged_fixture()
    c = Contract(kind="person", text="hire a cto", must={"field": ["software"], "skill": ["kafka"]}, prefer={"function": ["engineering"]})
    notes = [{"rule": "readings", "readings": [{"value": "crm", "field": "marketing", "share": 0.3}]}]
    out = _run(merged_search(c, kind="person", user_keys=set(), notes=notes, evaluate_fn=evaluate_fn, slice_fn=slice_fn, llm_json=llm, lines_fn=lines_fn, log_fn=log_fn))
    m = out["merge"]
    assert [r["name"] for r in m["recipes"]] == ["strict", "default", "relaxed:skill", "reading:field=marketing"] and calls["judge"] == __import__("roster_vertical.intake", fromlist=["JUDGE_BATCHES"]).JUDGE_BATCHES   # concurrent batches
    ids = [r["id"] for r in out["rows"]]
    assert ids[:3] == ["p1", "p3", "p9"] or ids[:3] == ["p1", "p9", "p3"]                      # fits first (p1 agreed by two recipes)
    assert ids[-1] == "p5" and out["rows"][-1]["fit"] == "no"                                    # the 'no' sinks with its verdict
    assert out["rows"][0]["found_by"] == {"strict": 1, "default": 1, "relaxed:skill": 1} and out["rows"][0]["fit_why"] == "p1 yes"
    assert m["fits"] == 3 and m["nos"] == 1 and m["graded"] == 9 and m["weak"] is False and m["union"] == 9
    assert out["counts"] == {"field": {"software": 5}} and out["contract"]["must"] == {"field": ["software"], "skill": ["kafka"]}   # the rail keeps the ratified contract
    assert calls["log"] and calls["log"][0]["tallies"]["yes"] == 3 and calls["log"][0]["recipes"][0]["name"] == "strict"
    assert calls["evaluate"][0]["_depth"] is None and all(e["_depth"] == {"counts": False} for e in calls["evaluate"][1:])   # counts once, for the ratified contract


def test_switched_off_recipes_and_empty_pools_are_left_out_and_a_failed_judge_leaves_fused_order():
    merged_search, calls, evaluate_fn, slice_fn, llm, lines_fn, log_fn = _merged_fixture()
    c = Contract(kind="person", text="hire a cto", must={"field": ["software"], "skill": ["kafka"]})
    def broken(system, user): raise RuntimeError("provider down")
    out = _run(merged_search(c, kind="person", user_keys=set(), notes=[], evaluate_fn=evaluate_fn, slice_fn=slice_fn, llm_json=broken, lines_fn=lines_fn, off=["relaxed:skill"]))
    m = out["merge"]
    assert [r["name"] for r in m["recipes"]] == ["strict", "default"] and m["off"] == ["relaxed:skill"] and m["ladder"] == ["strict", "default", "relaxed:skill"]
    assert m["graded"] == 0 and m["judge_error"] and [r["id"] for r in out["rows"]] == ["p1", "p2", "p8"] and out["rows"][0]["fit"] is None
