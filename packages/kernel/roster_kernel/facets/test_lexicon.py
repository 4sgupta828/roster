"""TDD for the lexicon (kernel — docs/specs/query-intent-decoding.md §4).

Pure: no DB, no model. The vocabulary here is a TEST vocabulary; the kernel never names a domain.
"""
from __future__ import annotations

from roster_kernel.facets import FacetKey, FacetSchema, FacetType
from roster_kernel.facets.lexicon import find_spans, plan_from_spans

SCHEMA = FacetSchema(keys=(
    FacetKey(key="mode", type=FacetType.categorical, kinds=("thing",), label="Mode",
             values=("remote", "hybrid", "onsite")),
    FacetKey(key="tier", type=FacetType.ordinal, kinds=("thing",), label="Tier",
             values=("junior", "mid", "senior", "staff_plus")),
    FacetKey(key="place", type=FacetType.set, kinds=("thing",), label="Place"),
    FacetKey(key="tags", type=FacetType.set, kinds=("thing",), label="Tags"),
))
ALIASES = {"tier": {"staff": "staff_plus", "head of": "staff_plus"},
           "place": {"new york": "nyc", "big apple": "nyc", "austin": "austin"}}
FILLER = frozenset({"jobs", "in", "the", "a"})
MUST_KEYS = frozenset({"mode", "tier", "place"})
RISKY = frozenset({"remote", "staff"})


def _plan(q):
    spans = find_spans(q, SCHEMA, "thing", aliases=ALIASES)
    return plan_from_spans(q, spans, filler=FILLER, must_keys=MUST_KEYS, risky_values=RISKY)


def test_a_query_that_is_a_closed_vocabulary_value_becomes_that_value():
    """The measured failure this exists for: the query IS the value, verbatim, and the compiler — a
    model told to extract only what a brief 'states explicitly as a requirement' — emitted nothing."""
    p = _plan("remote")
    assert p.must == {"mode": ["remote"]} and p.covered and p.blank_text
    assert p.residual == ""


def test_an_alias_is_canonicalised_to_what_the_index_actually_stores():
    """'new york' is what a person types; `nyc` is what the index holds. A must on the typed form is
    exact, kept (it is not demoted like other set keys), and matches nothing."""
    assert _plan("new york").must == {"place": ["nyc"]}
    assert _plan("jobs in new york").must == {"place": ["nyc"]}, "filler must not defeat coverage"


def test_a_multi_token_alias_beats_the_single_token_inside_it():
    p = _plan("head of")
    assert p.must == {"tier": ["staff_plus"]} and len(p.spans) == 1


def test_an_ordinary_english_word_only_filters_when_it_is_the_whole_query():
    """'remote possibility of travel' must not become a remote-work filter. A lone 'remote' has no
    grammar around it to misread, so it does."""
    alone = _plan("remote")
    assert alone.must == {"mode": ["remote"]}
    in_a_sentence = _plan("remote possibility of travel")
    assert in_a_sentence.must == {} and in_a_sentence.prefer == {"mode": ["remote"]}
    assert not in_a_sentence.covered and not in_a_sentence.blank_text
    assert in_a_sentence.residual == "possibility of travel"   # "of" is not filler in this test vocabulary


def test_a_partly_understood_query_ranks_and_keeps_its_remaining_words():
    """The precise query the gate must leave alone: the lexicon contributes preferences, the rest of
    the words stay as semantic text, and nothing is filtered on a guess."""
    p = _plan("staff backend engineer in austin")
    assert p.must == {}, "a query with unexplained words never hardens"
    assert p.prefer == {"tier": ["staff_plus"], "place": ["austin"]}
    assert p.residual == "backend engineer"
    assert not p.blank_text


def test_a_surface_form_legal_under_two_keys_is_never_hardened():
    schema = FacetSchema(keys=(
        FacetKey(key="a", type=FacetType.categorical, kinds=("thing",), label="A", values=("shared",)),
        FacetKey(key="b", type=FacetType.categorical, kinds=("thing",), label="B", values=("shared",)),
    ))
    spans = find_spans("shared", schema, "thing")
    p = plan_from_spans("shared", spans, must_keys=frozenset({"a", "b"}))
    assert p.must == {} and p.prefer and spans[0].ambiguous == ("b",)
    assert any("could be" in n for n in p.notes)


def test_blank_text_needs_a_must_not_merely_full_coverage():
    """Dropping the semantic text hands the evaluator the must-slice as its pool. With nothing
    narrowing, that is an arbitrary page of the whole index — worse than the embedding it replaced."""
    p = plan_from_spans("tags", find_spans("tags", SCHEMA, "thing"), must_keys=frozenset())
    assert not p.blank_text


def test_nothing_recognised_is_not_an_error():
    p = _plan("cuda kernels")
    assert p.must == {} and p.prefer == {} and p.residual == "cuda kernels" and not p.covered
