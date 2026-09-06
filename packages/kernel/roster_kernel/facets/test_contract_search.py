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
