"""Grounded Relationship Graph — pure-logic contract tests (adjacency, identity, demotion).
DB-free by design: GraphStore delegates the hot path to these functions."""
from roster_kernel.graph.store import (build_adjacency, edge_identity, edges_fully_dead,
                                       neighbors_from)


def _e(subj, rel, obj, *, ctx="", conf=1.0, label="established"):
    return {"id": edge_identity(subj, rel, obj, ctx), "subject": subj,
            "subject_norm": " ".join(subj.lower().split()), "relation": rel,
            "object": obj, "object_norm": " ".join(obj.lower().split()),
            "context_topic": ctx, "label": label, "provenance": "curated",
            "confidence": conf}


def test_identity_is_direction_and_context_sensitive():
    assert edge_identity("A", "causes", "B") != edge_identity("B", "causes", "A")
    assert edge_identity("A", "causes", "B") != edge_identity("A", "causes", "B", "in CKD")
    assert edge_identity(" CKD ", "causes", "Anemia") == edge_identity("ckd", "causes", "anemia")


def test_neighbors_reach_edges_in_both_directions():
    adj = build_adjacency([_e("chronic kidney disease", "increases_risk_of", "anemia")])
    out = neighbors_from(adj, ["chronic kidney disease"])
    inn = neighbors_from(adj, ["anemia"])
    assert len(out) == 1 and out[0]["direction"] == "out" and out[0]["object"] == "anemia"
    assert len(inn) == 1 and inn[0]["direction"] == "in"


def test_narrower_than_lifts_one_level_and_is_not_a_neighbor_edge():
    adj = build_adjacency([
        _e("anemia in pregnancy", "narrower_than", "anemia"),
        _e("chronic kidney disease", "increases_risk_of", "anemia"),
    ])
    hits = neighbors_from(adj, ["anemia in pregnancy"])
    # reaches the broad-topic edge via the hierarchy, and the hierarchy edge itself is silent
    assert [h["relation"] for h in hits] == ["increases_risk_of"]
    assert hits[0]["via"] == "anemia in pregnancy"


def test_neighbors_dedupe_rank_by_confidence_and_cap():
    adj = build_adjacency([
        _e("ckd", "increases_risk_of", "anemia", conf=0.5),
        _e("ckd", "comorbid_with", "gout", conf=0.9),
    ])
    hits = neighbors_from(adj, ["ckd", "CKD"], limit=1)   # duplicate topic input
    assert len(hits) == 1 and hits[0]["object"] == "gout"  # highest confidence wins the cap


def test_neighbor_order_is_deterministic_on_confidence_ties():
    edges = [_e("ckd", "increases_risk_of", "osteoporosis"),
             _e("ckd", "increases_risk_of", "anemia"),
             _e("diabetes", "increases_risk_of", "ckd")]
    a = neighbors_from(build_adjacency(edges), ["ckd"])
    b = neighbors_from(build_adjacency(list(reversed(edges))), ["ckd"])
    assert [x["id"] for x in a] == [x["id"] for x in b]     # insertion order must not matter


def test_first_input_topics_edges_win_the_cap_and_case_never_jumps_queue():
    adj = build_adjacency([
        _e("Parkinson disease", "comorbid_with", "depression"),        # capital subject
        _e("chronic kidney disease", "increases_risk_of", "anemia"),
        _e("chronic kidney disease", "increases_risk_of", "osteoporosis"),
    ])
    # asked subject first, brief-mentioned topic second → CKD edges must fill the cap
    hits = neighbors_from(adj, ["chronic kidney disease", "depression"], limit=2)
    assert [h["object"] for h in hits] == ["anemia", "osteoporosis"]
    # case-insensitive lexical: within one topic, capitals don't sort first
    hits2 = neighbors_from(adj, ["depression"], limit=2)
    assert hits2[0]["subject"] == "Parkinson disease"   # only edge — still reachable via rank 0


def test_demotion_requires_all_evidence_dead():
    ev = {"e1": ["d1"], "e2": ["d1", "d2"], "e3": []}
    assert edges_fully_dead(ev, {"d1"}) == ["e1"]          # e2 survives on d2; e3 has no evidence
    assert edges_fully_dead(ev, {"d1", "d2"}) == ["e1", "e2"]
