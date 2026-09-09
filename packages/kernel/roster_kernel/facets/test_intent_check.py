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
    rows = [{"title": "Platform Engineer", "company": "acme", "facets": {"tier": ["senior"], "mode": ["remote"]}},
            {"title": "Infra Engineer", "company": "acme", "facets": {"tier": ["mid"], "mode": ["remote"]}}]
    counts = {"tier": {"senior": 9000, "mid": 1000}}          # the whole slice — deliberately unlike the rows
    ev = evidence("platform", c, rows, counts, SCHEMA, kind="thing")
    assert ev["query"] == "platform"
    assert ev["believed"]["must"] == {"mode": ["remote"]}
    assert ev["returned"]["titles"] == ["Platform Engineer", "Infra Engineer"]
    assert ev["vocabulary"]["mode"] == ["remote", "hybrid", "onsite"]
    # the spread describes THESE ROWS, not the slice they were drawn from
    assert ev["spread"]["tier"] == [{"value": "senior", "share": 0.5}, {"value": "mid", "share": 0.5}]
    assert "mode" not in ev["spread"], "a value every row shares distinguishes nothing"


def test_the_spread_describes_the_rows_not_the_whole_index():
    """THE BUG THIS EXISTS FOR. `counts` are computed over the MUST-SLICE, and when the only must is a
    country that slice is the entire index. Feeding them here told the debugger that a search for ML
    infrastructure had returned "truck driving 3280, insurance 1440" — the shape of the whole job
    market — and it duly reported that retrieval was pulling in noise. It reasoned correctly from
    evidence that was wrong, and accused the search of a fault it did not have."""
    rows = [{"title": "ML Infra Engineer", "facets": {"tags": ["machine learning", "infrastructure"]}},
            {"title": "Distributed Systems Engineer", "facets": {"tags": ["distributed systems", "machine learning"]}}]
    index_wide = {"tags": {"truck driving": 3280, "insurance": 1440, "machine learning": 12}}
    ev = evidence("ml infra", Contract(kind="thing"), rows, index_wide, SCHEMA, kind="thing")
    values = [v["value"] for v in ev["spread"]["tags"]]
    assert "truck driving" not in values and "insurance" not in values
    assert "machine learning" in values


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


def test_a_long_query_still_gets_a_read():
    """A word count cannot judge whether a question was understood — only what came back can, and only
    the model sees that. "ML Infra Distributed Systems, AI Agents VP Director roles" is eight words and
    was silently skipped; a reader cannot tell that from a query that simply had nothing to say. The
    model is asked, and returns no readings when nothing is unclear."""
    ok, why = worth_asking("staff backend engineer at stripe working on payments infrastructure",
                           {"pool": 400, "best_match": 95})
    assert ok is True and why


def test_the_only_silences_are_the_ones_that_cannot_be_argued_with():
    assert worth_asking("", {"pool": 400})[0] is False           # nothing was typed
    assert worth_asking("platform", {"pool": 3})[0] is False     # nothing to have a view about


def test_an_ambiguous_query_is_always_worth_asking_about():
    ok, _ = worth_asking("a very long and otherwise specific sounding sentence about work", {"pool": 400},
                         ambiguous=True)
    assert ok is True


def test_a_thin_result_set_is_never_reinterpreted():
    """With almost nothing back, the problem is coverage, not comprehension."""
    assert worth_asking("platform", {"pool": 3})[0] is False
