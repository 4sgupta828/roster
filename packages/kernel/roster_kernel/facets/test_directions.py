"""TDD for directions (kernel — docs/specs/intent-convergence-loop.md §3).

Pure: no DB, no model, no domain vocabulary.
"""
from __future__ import annotations

from roster_kernel.facets import FacetKey, FacetSchema, FacetType
from roster_kernel.facets.directions import (Direction, cluster_directions, facet_directions,
                                             rank_directions, worth_steering)

SCHEMA = FacetSchema(keys=(
    FacetKey(key="mode", type=FacetType.categorical, kinds=("thing",), label="Mode",
             values=("remote", "hybrid", "onsite")),
    FacetKey(key="tier", type=FacetType.ordinal, kinds=("thing",), label="Tier",
             values=("junior", "mid", "senior", "staff_plus")),
))


def test_a_direction_is_offered_only_when_it_would_actually_change_the_set():
    """A value holding 98 % of the slice is not a choice — picking it changes nothing, and picking
    against it leaves nothing."""
    counts = {"mode": {"remote": 98, "onsite": 2}}
    assert facet_directions(counts, SCHEMA, "thing") == []
    counts = {"mode": {"remote": 50, "onsite": 50}}
    got = facet_directions(counts, SCHEMA, "thing")
    assert {d.values[0] for d in got} == {"remote", "onsite"}
    assert all(d.hits == 50 and d.score > 0.9 for d in got)


def test_a_split_near_half_beats_a_lopsided_one():
    counts = {"mode": {"remote": 50, "onsite": 50}, "tier": {"senior": 90, "junior": 10}}
    ranked = rank_directions(facet_directions(counts, SCHEMA, "thing"), top=2)
    assert ranked[0].key == "mode"


def test_the_menu_asks_three_different_questions_not_three_values_of_one():
    counts = {"mode": {"remote": 34, "hybrid": 33, "onsite": 33},
              "tier": {"senior": 55, "junior": 45}}
    ranked = rank_directions(facet_directions(counts, SCHEMA, "thing"), top=3)
    assert len({d.key for d in ranked}) == len(ranked), "one direction per key"


def test_an_emergent_cluster_is_offered_both_ways_round():
    """'These are mostly agency reposts' is most useful as a way to say NOT THAT — and `avoid` is a
    thing the contract already carries."""
    groups = [{"name": "tour guide", "ids": [1, 2, 3, 4]}]
    got = cluster_directions(groups, 10)
    assert {d.section for d in got} == {"must", "avoid"}
    keep = next(d for d in got if d.section == "must")
    drop = next(d for d in got if d.section == "avoid")
    assert keep.hits == 4 and drop.hits == 6
    assert drop.label == "not tour guide"


def test_only_one_half_of_a_cluster_reaches_the_menu():
    groups = [{"name": "tour guide", "ids": [1, 2, 3, 4, 5]}]
    ranked = rank_directions(cluster_directions(groups, 10), top=3)
    assert len(ranked) == 1, "keep-these and drop-these are one question, not two"


def test_facet_and_cluster_candidates_are_comparable_on_one_scale():
    """The whole point of the shared score: a seniority split and an emergent cluster have to be
    rankable against each other, or the menu is just whichever source ran first."""
    counts = {"tier": {"senior": 90, "junior": 10}}          # lopsided facet
    groups = [{"name": "tour guide", "ids": list(range(5))}]  # even cluster
    ranked = rank_directions(facet_directions(counts, SCHEMA, "thing") + cluster_directions(groups, 10), top=1)
    assert ranked[0].source == "cluster"


# ---------------------------------------------------------------- the gate

def test_a_tight_well_matched_result_set_is_left_alone():
    """The risk the panel named: turning a precise search into a nagging form. Silence is the default."""
    ok, why = worth_steering({"pool": 400, "best_match": 88})
    assert ok is False and "match" in why


def test_a_thin_pool_is_never_split_further():
    ok, why = worth_steering({"pool": 5, "best_match": 20})
    assert ok is False and "few" in why


def test_an_ambiguous_query_is_steered_even_when_the_matches_look_good():
    """`PM` silently chose product over project. A high match score on the wrong reading is exactly the
    case a clarifying question exists for."""
    ok, _ = worth_steering({"pool": 400, "best_match": 95}, ambiguous=True)
    assert ok is True


def test_a_weak_match_alone_steers_but_a_low_score_is_not_treated_as_misunderstanding():
    assert worth_steering({"pool": 400, "best_match": 30, "weak": True})[0] is True
    # a middling score with nothing else wrong is the index being ordinary, not the reader being unclear
    ok, why = worth_steering({"pool": 400, "best_match": 50})
    assert ok is False and "settled" in why
