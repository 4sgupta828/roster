"""Contract-search mechanics (spec §12): scarcity-gated collapse detection, co-occurrence readings by share / lift /
support, and rank fusion with provenance. Opaque keys throughout."""
from roster_kernel.facets.contract_search import collapsing_musts, cooccurrence_readings, rrf_fuse


def test_a_must_collapses_only_under_scarcity_and_only_when_relaxable_and_not_the_users():
    without = {"m": 5000, "k": 40, "u": 9000, "d": 200000}
    out = collapsing_musts(12, without, user_keys={"u"}, relaxable_keys={"m", "k", "u"})
    assert [c.key for c in out] == ["m"]                      # k barely helps; u is the user's; d is not relaxable
    assert out[0].pool_without == 5000 and "instead of 12" in out[0].reason
    assert collapsing_musts(20000, {"m": 100000}, user_keys=set(), relaxable_keys={"m"}) == []   # 20k is not scarce
    assert collapsing_musts(30, {"m": 120000}, user_keys=set(), relaxable_keys={"m"}) == []      # 30 is a valid small answer
    assert collapsing_musts(0, {"m": 3}, user_keys=set(), relaxable_keys={"m"}) == []             # removing it still leaves ~nothing


def test_cooccurrence_needs_share_lift_and_support_and_can_yield_two_readings():
    bg = {"a": 300000, "b": 60000, "c": 40000, "unknown": 100000}
    dist = {"a": 45, "b": 40, "c": 5, "unknown": 30}
    r = cooccurrence_readings("sp", "x", dist, bg, target_key="fd")
    assert [x.target_value for x in r] == ["b"]                # a: share .5 but lift 0.67 (it is just the majority); b: share .44, lift 2.9
    assert r[0].support == 40 and r[0].share == 0.444
    dist2 = {"a": 300, "b": 250, "c": 10}
    r2 = cooccurrence_readings("sp", "y", dist2, bg, target_key="fd", min_lift=0.5)
    assert {x.target_value for x in r2} == {"a", "b"}          # bimodal → two readings
    assert cooccurrence_readings("sp", "z", {"b": 10}, bg, target_key="fd") == []              # support too small


def test_rrf_fuse_rewards_agreement_and_keeps_provenance():
    lists = {"strict": [{"id": "p1"}, {"id": "p2"}], "wide": [{"id": "p3"}, {"id": "p1"}, {"id": "p4"}], "alt": [{"id": "p2"}, {"id": "p3"}]}
    out = rrf_fuse(lists, k=60)
    ids = [r["id"] for r in out]
    assert ids[0] in ("p1", "p2", "p3") and set(ids) == {"p1", "p2", "p3", "p4"}
    p1 = next(r for r in out if r["id"] == "p1"); assert p1["_found_by"] == {"strict": 1, "wide": 2}
    p4 = next(r for r in out if r["id"] == "p4"); assert p4["_found_by"] == {"wide": 3} and ids[-1] == "p4"
    w = rrf_fuse(lists, k=60, weights={"strict": 3.0}); assert [r["id"] for r in w][0] == "p1"
    assert rrf_fuse({}) == []


# ---- step 2: recipes, survivors, blind, merged order, head precision --------------------------------------------

def _c(**kw):
    from roster_kernel.facets import Contract
    return Contract(kind="x", text="t", **kw)


def test_the_ladder_relaxes_compiled_musts_on_relaxable_keys_never_the_users_and_swaps_readings():
    from roster_kernel.facets.contract_search import recipes
    c = _c(must={"k1": ["a"], "k2": ["b"], "k3": ["c"], "k4": ["d"]}, prefer={"k5": ["e"]})
    rs = recipes(c, user_keys={"k3"}, relaxable_keys={"k2", "k3", "k4"}, readings=[{"key": "k1", "value": "z"}, {"key": "k3", "value": "q"}, {"key": "k1", "value": "a"}])
    names = [r.name for r in rs]
    assert names[0] == "strict"
    assert "relaxed:k2" in names and "relaxed:k4" in names and "relaxed:k3" not in names          # the user's key holds
    assert "loose" in names and "reading:k1=z" in names and "reading:k3=q" not in names and "reading:k1=a" not in names
    loose = next(r for r in rs if r.name == "loose")
    assert set(loose.contract.must) == {"k1", "k3"} and loose.contract.prefer["k2"] == ["b"] and loose.contract.prefer["k4"] == ["d"]
    rd = next(r for r in rs if r.name == "reading:k1=z")
    assert rd.contract.must["k1"] == ["z"] and rd.contract.must["k2"] == ["b"]
    # identical contracts collapse: with one relaxable key there is no 'loose'
    rs2 = recipes(_c(must={"k1": ["a"], "k2": ["b"]}), user_keys=set(), relaxable_keys={"k2"})
    assert [r.name for r in rs2] == ["strict", "relaxed:k2"]


def test_survivors_drop_empty_pools_keep_unprobed_and_put_strict_first():
    from roster_kernel.facets.contract_search import Recipe, survivors
    rs = [Recipe("relaxed:k2", _c(), "w"), Recipe("strict", _c(), "w"), Recipe("loose", _c(), "w"), Recipe("reading:k1=z", _c(), "w")]
    s = survivors(rs, {"strict": 3, "relaxed:k2": 0, "loose": 900}, max_keep=5)
    assert [r.name for r in s] == ["strict", "loose", "reading:k1=z"] and s[0].pool == 3 and s[2].pool is None


def test_blind_hides_source_and_rank_deterministically():
    from roster_kernel.facets.contract_search import blind
    rows = [{"id": f"e{i}", "_found_by": {"strict": i}} for i in range(6)]
    items, mapping = blind(rows, seed=7)
    assert [b for b, _ in items] == ["r1", "r2", "r3", "r4", "r5", "r6"] and sorted(mapping.values()) == [f"e{i}" for i in range(6)]
    assert [mapping[b] for b, _ in items] != [f"e{i}" for i in range(6)]                     # shuffled
    assert blind(rows, seed=7)[1] == mapping                                                   # and reproducible


def test_merged_order_is_fits_then_partials_then_ungraded_tail_then_nos():
    from roster_kernel.facets.contract_search import order_by_verdicts, head_precision
    fused = [{"id": f"e{i}"} for i in range(8)]
    verdicts = {"e0": {"fit": "no", "why": "other"}, "e1": {"fit": "partial", "why": "near"}, "e2": {"fit": "yes", "why": "fits"}, "e3": {"fit": "yes", "why": "fits"},
                "e6": {"fit": "yes", "why": "beyond the head — ignored"}}
    out = order_by_verdicts(fused, verdicts, head=5)
    assert [r["id"] for r in out] == ["e2", "e3", "e1", "e4", "e5", "e6", "e7", "e0"]
    assert out[0]["_fit"] == "yes" and out[3]["_fit"] is None and out[-1]["_fit"] == "no" and out[-1]["_why"] == "other"
    assert head_precision(out, verdicts, k=5, weak_ids={"e3"}) == round((1 + 0.8 + 0.4) / 5, 3)


def test_a_judges_id_is_resolved_however_it_writes_it():
    from roster_kernel.facets.contract_search import blind, resolve_blind_id
    _, mapping = blind([{"id": "e0"}, {"id": "e1"}, {"id": "e2"}], seed=1)
    assert resolve_blind_id(mapping, "r2") == mapping["r2"] and resolve_blind_id(mapping, 2) == mapping["r2"]
    assert resolve_blind_id(mapping, "[R3]") == mapping["r3"] and resolve_blind_id(mapping, "x9") is None and resolve_blind_id(mapping, "") is None


def test_the_default_recipe_lets_compiled_musts_on_named_keys_rank_but_never_the_users():
    from roster_kernel.facets.contract_search import recipes
    c = _c(must={"k1": ["a"], "k2": ["b"], "k3": ["c"]})
    rs = recipes(c, user_keys={"k3"}, relaxable_keys=set(), default_keys={"k1", "k3"})
    assert [r.name for r in rs] == ["strict", "default"]
    d = rs[1].contract
    assert d.must == {"k2": ["b"], "k3": ["c"]} and d.prefer["k1"] == ["a"]
    assert [r.name for r in recipes(c, user_keys={"k1", "k3"}, relaxable_keys=set(), default_keys={"k1", "k3"})] == ["strict"]
