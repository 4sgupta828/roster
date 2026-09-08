"""The brief and the consultant move (spec guided-consultant-v3 §2–§4): sources, the parse-and-gate of a planner's
output, the fork gate, readiness, leverage, and the hardness rule from brief to contract. Opaque keys only."""
from __future__ import annotations

from roster_kernel.facets.brief import Brief, apply_brief_delta, contract_from_brief, gate_fork, leverage, parse_move, readiness
from roster_kernel.facets.schema import FacetKey, FacetSchema, FacetType

SCH = FacetSchema(keys=(FacetKey(key="k1", type=FacetType.categorical, kinds=("e",), label="K1", values=("a", "b", "c")),
                        FacetKey(key="k2", type=FacetType.ordinal, kinds=("e",), label="K2", values=("lo", "mid", "hi")),
                        FacetKey(key="k3", type=FacetType.set, kinds=("e",), label="K3", top_n=5)))
FIELDS = {"f1", "f2", "f3", "f4"}


def test_a_brief_knows_what_is_known_inferred_skipped_and_a_guess_never_erases_a_fact():
    b = Brief().with_field("f1", "a", "document", span="… a …").with_field("f2", "lo", "inferred")
    assert b.known("f1") and not b.known("f2") and not b.settled("f3") and b.skipped("f3").settled("f3")
    n, notes = apply_brief_delta(b, {"f1": {"value": "b", "source": "inferred"}, "f2": {"value": "hi", "source": "stated"}, "zz": {"value": 1}, "f3": {"value": "x", "source": "asked"}},
                                 allowed_fields=FIELDS)
    assert n.value("f1") == "a" and n.value("f2") == "hi" and n.source("f2") == "stated" and "f3" not in n.fields
    assert any("known 'f1'" in x for x in notes) and any("unknown field 'zz'" in x for x in notes) and any("cannot set source 'asked'" in x for x in notes)
    assert Brief.from_dict(n.to_dict()).to_dict() == n.to_dict()


def test_parse_move_keeps_one_question_two_sentences_and_never_asks_a_known_field():
    b = Brief().with_field("f1", "a", "stated")
    raw = {"move": "fork", "say": "First. Second. Third sentence is cut.", "question": [
        {"field": "f1", "text": "What is f1?", "options": [{"label": "A", "effect": {"must": {"k1": ["a"]}}}]},
        {"field": "f2", "text": "second question"}],
        "brief_delta": {"f2": {"value": "mid", "source": "inferred", "span": "s"}, "nope": {"value": 1}}, "contract_delta": {"prefer": {"k1": ["b", "zzz"], "k9": ["q"]}}}
    m = parse_move(raw, brief=b, schema=SCH, allowed_fields=FIELDS)
    assert m.say == "First. Second." and m.question is None and m.move == "infer"                    # known field → no question → fork degrades to infer
    assert m.brief_delta == {"f2": {"value": "mid", "source": "inferred", "span": "s", "note": ""}} and m.contract_delta == {"prefer": {"k1": ["b"]}}
    assert any("known field 'f1'" in n for n in m.notes) and any("2 questions" in n for n in m.notes) and any("k1='zzz'" in n for n in m.notes)
    m2 = parse_move({"move": "confirm", "say": "Reading f1 as a — right?", "question": {"field": "f1", "text": "Right?", "options": [{"label": "Yes"}, {"label": "No"}]}}, brief=b, schema=SCH, allowed_fields=FIELDS)
    assert m2.move == "confirm" and m2.question and m2.question.field == "f1" and len(m2.question.options) == 2
    m3 = parse_move({"move": "bogus", "say": ""}, brief=b, schema=SCH, allowed_fields=FIELDS)
    assert m3.move == "infer" and m3.say.startswith("Tell me")


def test_a_fork_is_asked_only_when_two_options_survive_the_index_and_differ():
    opts = [{"label": "A", "effect": {"must": {"k1": ["a"]}}}, {"label": "B", "effect": {"must": {"k1": ["b"]}}}, {"label": "C", "effect": {"must": {"k1": ["c"]}}}]
    kept, worth, why = gate_fork(opts, {"A": 120, "B": 0, "C": 40})
    assert [o["label"] for o in kept] == ["A", "C"] and worth
    kept, worth, why = gate_fork(opts, {"A": 120, "B": 0, "C": 0})
    assert not worth and "fewer than two" in why
    same = [{"label": "X", "effect": {"must": {"k1": ["a"]}}}, {"label": "Y", "effect": {"must": {"k1": ["a"]}}}]
    assert gate_fork(same, {})[1] is False


def test_readiness_and_leverage():
    b = Brief().with_field("f1", "a", "asked").skipped("f2").with_field("f3", "z", "inferred")
    assert readiness(b, ("f1", "f2", "f3")) == ["f3"] and readiness(b, ("f1", "f2", "f3"), accept_inferred=True) == []
    lv = leverage({"k1": {"a": 50, "b": 45, "c": 5}, "k2": {"lo": 95, "hi": 5}, "k3": {"x": 10}}, SCH, exclude={"k3"})
    assert [x["key"] for x in lv] == ["k1"] and lv[0]["values"][0] == ("a", 50)                           # k2 is not spread


def test_contract_from_brief_lets_known_fields_filter_and_inferred_fields_only_rank():
    b = (Brief().with_field("f1", "a", "stated").with_field("f2", "hi", "inferred").with_field("f3", ["x", "y"], "document")
         .with_field("f4", "nope", "asked"))
    mapping = {"f1": ("k1", "must"), "f2": ("k2", "center"), "f3": ("k3", "prefer"), "f4": ("k1", "must")}
    c, notes = contract_from_brief(b, mapping, SCH, kind="e", text="t")
    assert c.must == {"k1": ["a"]} and c.center == {"key": "k2", "value": "hi", "span": 1} and c.prefer == {"k3": ["x", "y"]}
    assert any("f4" in n and "not in the index" in n for n in notes)
    c2, n2 = contract_from_brief(Brief().with_field("f1", "b", "inferred"), {"f1": ("k1", "must")}, SCH, kind="e", text="t")
    assert c2.must == {} and c2.prefer == {"k1": ["b"]} and n2 == ["f1: inferred → ranks, not filters"]
