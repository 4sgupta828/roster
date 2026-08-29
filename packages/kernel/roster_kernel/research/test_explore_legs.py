"""Exploratory-legs extension (ROSTER_EXPLORE_LEGS) — Evidence Contract retrieval for
EXPLORATORY contracts.

The missed-axes finding: on exploratory (understand/discover) questions 17%+ of must-cover
dimensions were absent from answers despite usable corpus evidence, because exploratory
contracts carried no axes and got no retrieval legs. Minimal v1 (retrieval only): the vertical
now derives 2-4 axes for exploratory questions; build_legs expands them into AXIS-ONLY legs
(each axis verbatim, cap 4, no entity expansion); react executes them under the SAME steer
gate + late-merge seam as enumerative legs — but ONLY when the explore_legs flag is also on.
OFF → exploratory legs are never built: every prompt/behavior byte-identical to today even
though the derived contract carries axes. NO slot grid, NO coverage gaps, NO compose-seat
reservation for exploratory in this version. Offline throughout — scripted LLMs, no network
(mirrors test_question_contract.py, whose helpers it reuses)."""
from __future__ import annotations

import asyncio

from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.contract import Contract, build_legs
from roster_kernel.research.react import run_react
from roster_kernel.research.test_question_contract import (
    _LEG_TEXT,
    _PROMPT,
    CountingSource,
    RecordingLLM,
    _base_script,
    _contract_ns,
    _source,
)


def _explore_ns(axes=("axis one", "axis two")):
    """A scripted EXPLORATORY derivation output — entities stay empty (the vertical's rule)."""
    return _contract_ns(mode="exploratory", entities=(), axes=axes)


def _run(llm, source, *, mode="", explore=False, prompt=_PROMPT, budget=None, diag=True,
         graph_legs=None, graph_late=False):
    return asyncio.run(run_react(
        question="what is the baseline metric?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=source, tenant_id="A", budget=budget or BudgetState(max_calls=20),
        question_contract=mode, contract_prompt=prompt, explore_legs=explore,
        collect_diagnostics=diag, graph_legs=graph_legs, graph_late=graph_late, max_steps=4))


# ---- build_legs: exploratory axis-only expansion ----------------------------------------------

def test_build_legs_exploratory_axis_only_verbatim_no_entity_expansion():
    # entities present on the contract are IGNORED — each axis verbatim, nothing else
    c = Contract(mode="exploratory", entities=["e1", "e2"], axes=["axis one", "axis two"])
    assert build_legs(c) == ["axis one", "axis two"]


def test_build_legs_exploratory_cap_4():
    c = Contract(mode="exploratory", axes=["a1", "a2", "a3", "a4", "a5"])
    assert build_legs(c) == ["a1", "a2", "a3", "a4"]           # capped at 4 even under cap=12
    assert build_legs(c, cap=2) == ["a1", "a2"]                # a smaller caller cap still binds


def test_build_legs_exploratory_dedupes_self_and_exclude_case_insensitive():
    c = Contract(mode="exploratory", axes=["Axis One", "axis one", "axis two", "axis three"])
    assert build_legs(c, exclude={"AXIS TWO"}) == ["Axis One", "axis three"]


def test_build_legs_enumerative_unchanged_by_extension():
    # the same axes on an ENUMERATIVE contract still expand per-entity (byte-identical path)
    c = Contract(mode="enumerative", entities=["e1", "e2"], axes=["axis one"])
    assert build_legs(c) == ["axis one", "e1 axis one", "e2 axis one"]


# ---- flag OFF: byte-parity even though the contract now carries axes ---------------------------

def test_flag_off_steer_exploratory_axes_is_byte_identical():
    b_off, b_ex = BudgetState(max_calls=20), BudgetState(max_calls=20)
    llm_off = RecordingLLM(_base_script())
    src_off = CountingSource(_source())
    res_off = _run(llm_off, src_off, mode="", budget=b_off)
    llm_ex = RecordingLLM([_explore_ns()] + _base_script())
    src_ex = CountingSource(_source())
    res_ex = _run(llm_ex, src_ex, mode="steer", explore=False, budget=b_ex)
    # every planner/compose prompt string is EXACTLY the OFF run's (derivation call is extra)
    assert llm_ex.prompts[1:] == llm_off.prompts
    assert res_ex.composed_answer == res_off.composed_answer
    assert src_ex.searches == src_off.searches                 # not one leg retrieval
    assert b_ex.spent_calls == b_off.spent_calls + 1           # exactly the derivation charge
    assert res_ex.coverage_gaps == res_off.coverage_gaps == []
    # OFF strips the legs BEFORE diag/SSE — no trace of them anywhere (true parity)
    assert res_ex.diagnostics["question_contract"]["legs"] == []
    # ...while the derived axes stay observable (they were already logged pre-extension)
    assert res_ex.question_contract == {"mode": "exploratory", "entities": [],
                                        "axes": ["axis one", "axis two"], "stance": ""}


# ---- flag ON + steer: ≤4 axis legs, concurrent, k=4, late-merged -------------------------------

def test_flag_on_steer_executes_capped_axis_legs_concurrently_k4():
    src = CountingSource(_source())
    llm = RecordingLLM([_explore_ns(axes=("axone", "axtwo", "axthree", "axfour", "axfive"))]
                       + _base_script())
    res = _run(llm, src, mode="steer", explore=True)
    assert res.grounded
    leg_searches = [(q, k) for q, k in src.searches if k == 4]
    # 5 derived axes → cap 4 legs, each axis VERBATIM (no entity expansion)
    assert sorted(q for q, _ in leg_searches) == ["axfour", "axone", "axthree", "axtwo"]
    assert [d["query"] for d in res.diagnostics["question_contract"]["legs"]] == \
        ["axone", "axtwo", "axthree", "axfour"]
    assert src.max_in_flight >= 2                              # asyncio.gather — concurrent
    # baseline retrieval unchanged and mandatory
    assert ("baseline metric reading cohort", 10) in src.searches


def test_flag_on_steer_legs_late_merge_planner_window_unaffected():
    src = _source(extra_leg_block=True)                        # b2: "alphaline axis one value…"
    llm = RecordingLLM([_explore_ns(axes=("alphaline axis one",))] + _base_script())
    res = _run(llm, src, mode="steer", explore=True)
    assert res.grounded
    # mid-loop the planner NEVER saw the leg's atom (late merge — same seam as graph legs)
    answer_step_prompt = llm.prompts[2]                        # derivation, search, THIS
    assert _LEG_TEXT not in answer_step_prompt
    # ...but post-loop the leg evidence joined the pool, with hits/merged diag like today
    qc = res.diagnostics["question_contract"]
    assert qc["legs"] == [{"query": "alphaline axis one", "hits": 1, "merged": 1}]
    assert res.atoms_gathered == 2
    # retrieval only in v1: no slot grid, no coverage gaps for exploratory
    assert "slot_grid" not in qc
    assert res.coverage_gaps == []


def test_flag_on_steer_legs_dedupe_against_graph_legs():
    src = _source(extra_leg_block=True)
    llm = RecordingLLM([_explore_ns(axes=("axis one", "axis two"))] + _base_script())
    res = _run(llm, src, mode="steer", explore=True,
               graph_legs=[{"query": "axis one", "note": "graph edge"}], graph_late=True)
    # the graph leg already owns "axis one" → the exploratory leg set excludes it
    assert [d["query"] for d in res.diagnostics["question_contract"]["legs"]] == ["axis two"]


# ---- flag ON + shadow: legs computed + logged, NEVER executed ----------------------------------

def test_flag_on_shadow_does_not_execute():
    src = CountingSource(_source())
    llm = RecordingLLM([_explore_ns()] + _base_script())
    res = _run(llm, src, mode="shadow", explore=True)
    assert res.grounded
    # ONLY the planner's own search hit the source — legs were computed, never retrieved
    assert src.searches == [("baseline metric reading cohort", 10)]
    qc = res.diagnostics["question_contract"]
    assert [d["query"] for d in qc["legs"]] == ["axis one", "axis two"]
    assert all("hits" not in d and "merged" not in d for d in qc["legs"])


# ---- flag ON: enumerative contracts byte-identical to flag OFF ---------------------------------

def test_flag_on_enumerative_behavior_unchanged():
    src_on, src_off = CountingSource(_source()), CountingSource(_source())
    llm_on = RecordingLLM([_contract_ns()] + _base_script())
    res_on = _run(llm_on, src_on, mode="steer", explore=True)
    llm_off = RecordingLLM([_contract_ns()] + _base_script())
    res_off = _run(llm_off, src_off, mode="steer", explore=False)
    assert llm_on.prompts == llm_off.prompts
    assert src_on.searches == src_off.searches
    assert [d["query"] for d in res_on.diagnostics["question_contract"]["legs"]] == \
        [d["query"] for d in res_off.diagnostics["question_contract"]["legs"]]
