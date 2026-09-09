"""TDD for the intent debugger (kernel). Pure: no model, no DB, no domain vocabulary."""
from __future__ import annotations

from roster_kernel.facets import Contract, FacetKey, FacetSchema, FacetType
from roster_kernel.facets.intent_check import evidence, parse, worth_asking

SCHEMA = FacetSchema(keys=(
    FacetKey(key="mode", type=FacetType.categorical, kinds=("thing",), label="Mode",
             values=("remote", "hybrid", "onsite")),
    FacetKey(key="tier", type=FacetType.ordinal, kinds=("thing",), label="Tier",
             values=("junior", "mid", "senior")),
    FacetKey(key="tags", type=FacetType.set, kinds=("thing",), label="Tags"),
))


def test_the_evidence_shows_what_was_asked_what_was_decided_and_what_came_back():
    """A debugger that will not show its current hypothesis is just a prompt. The model is given the
    query, the contract in force, and the actual result set — enough to judge whether they agree."""
    c = Contract(kind="thing", text="platform", must={"mode": ["remote"]}, prefer={"tier": ["senior"]})
    rows = [{"title": "Platform Engineer", "company": "acme"}, {"title": "Infra Engineer", "company": "acme"}]
    counts = {"tier": {"senior": 60, "mid": 40}, "mode": {"remote": 100}}
    ev = evidence("platform", c, rows, counts, SCHEMA, kind="thing")
    assert ev["query"] == "platform"
    assert ev["believed"]["must"] == {"mode": ["remote"]}
    assert ev["returned"]["titles"] == ["Platform Engineer", "Infra Engineer"]
    assert ev["spread"]["tier"][0] == {"value": "senior", "share": 0.6}
    assert "mode" not in ev["spread"], "a single-valued key distinguishes nothing and is left out"
    assert ev["vocabulary"]["mode"] == ["remote", "hybrid", "onsite"]


def test_a_reading_keeps_only_values_the_vocabulary_holds():
    """The same discipline a compiled contract gets: an invented value never reaches a search."""
    got = parse({"question": "Which did you mean?", "readings": [
        {"label": "Infra", "says": "the substrate", "text": "kubernetes reliability",
         "prefer": {"tier": ["senior", "wizard"], "made_up": ["x"]}}]}, SCHEMA, "thing")
    assert got is not None
    assert got.readings[0].prefer == {"tier": ["senior"]}


def test_a_reading_that_would_change_nothing_is_dropped():
    """An option that carries no edit is not an option — choosing it would re-run the same search."""
    got = parse({"question": "Which?", "readings": [
        {"label": "Empty", "says": "nothing at all"},
        {"label": "Real", "says": "something", "text": "developer tooling"}]}, SCHEMA, "thing")
    assert [r.label for r in got.readings] == ["Real"]


def test_no_readings_means_no_question():
    """Saying nothing is a valid and common answer; an empty menu must not reach the reader."""
    assert parse({"question": "Which?", "readings": []}, SCHEMA, "thing") is None
    assert parse({"readings": [{"label": "x", "text": "y"}]}, SCHEMA, "thing") is None, "a menu needs its question"
    assert parse(None, SCHEMA, "thing") is None


def test_at_most_three_readings_reach_the_reader():
    raw = {"question": "Which?", "readings": [{"label": f"r{i}", "text": f"t{i}"} for i in range(6)]}
    assert len(parse(raw, SCHEMA, "thing").readings) == 3


# ---------------------------------------------------------------- when to ask at all

def test_a_short_query_is_worth_asking_about():
    """Few words carry little constraint — the case where a reading is most likely to be wrong."""
    assert worth_asking("platform engineer", {"pool": 400})[0] is True


def test_a_long_specific_query_that_matched_well_is_left_alone():
    """A debugger that interrupts a session it understands is noise, and every ask costs a call."""
    ok, why = worth_asking("staff backend engineer at stripe working on payments infrastructure",
                           {"pool": 400, "best_match": 95})
    assert ok is False and "specific" in why


def test_an_ambiguous_query_is_always_worth_asking_about():
    ok, _ = worth_asking("a very long and otherwise specific sounding sentence about work", {"pool": 400},
                         ambiguous=True)
    assert ok is True


def test_a_thin_result_set_is_never_reinterpreted():
    """With almost nothing back, the problem is coverage, not comprehension."""
    assert worth_asking("platform", {"pool": 3})[0] is False
