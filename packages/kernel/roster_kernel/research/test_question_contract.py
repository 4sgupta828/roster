"""Evidence Contract stage 3 — the question-contract flag (ROSTER_QUESTION_CONTRACT).

Modes: "" off (byte-identical); "shadow" → derive the contract + compute the per-entity legs +
log them to the diag trace (contract, legs, slot grid) — NO leg retrieval, NO selection change,
exactly +1 charged LLM call; "steer" → enumerative contracts execute the legs (cap 8,
round-robin across entities, k=4 each, concurrent, LATE-merged like graph legs — baseline
retrieval unchanged), compose selection reserves cap seats for slot-filling claims (an off-slot
claim can never evict a slot-filler), and entities left with zero claims become honest
loop-produced coverage gaps. Offline throughout — scripted LLMs, no network (mirrors
test_evidence_identity.py / test_graph_legs.py)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.contract import Contract, build_legs, derive_contract, match_entities
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource

_BASE_TEXT = "baseline metric reading is 9 percent for the cohort"
_LEG_TEXT = "alphaline axis one value is 7 percent"


def _source(extra_leg_block: bool = False) -> InMemoryRetrievalSource:
    """Blocks are embedding-less → lexical-only matching, so the planner query and the leg
    queries each hit ONLY their own block (the test_graph_legs pattern)."""
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_BASE_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
        document_title="BASELINE DOC", source_key="corpus"))
    if extra_leg_block:
        src.add(IndexedBlock(
            block_id="b2", document_id="d2", tenant_id="A", text=_LEG_TEXT,
            locator=Locator("block_span", "d2", {"block_id": "b2"}),
            document_title="LEG DOC", source_key="corpus"))
    return src


class CountingSource:
    """Wraps a RetrievalSource: records every (query, k), tracks concurrent in-flight searches."""
    def __init__(self, inner):
        self.inner = inner
        self.searches: list[tuple[str, int]] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def search(self, req):
        self.searches.append((req.query, req.k))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.005)          # give concurrent legs a chance to overlap
        self.in_flight -= 1
        return await self.inner.search(req)

    def make_block_loader(self, tenant_id, workspace_id=None):
        return self.inner.make_block_loader(tenant_id, workspace_id)


class RecordingLLM:
    """Returns pre-scripted parsed objects in order AND records every prompt it saw."""
    def __init__(self, steps):
        self._steps = list(steps)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.systems.append(system)
        self.prompts.append(messages[0]["content"])
        return LLMResult(parsed=self._steps.pop(0), output_tokens=5, model="scripted")


class RaisingLLM:
    async def complete(self, **kw):
        raise RuntimeError("boom")


_PROMPT = "Derive the contract."           # stands in for the vertical's derivation directive


def _contract_ns(mode="enumerative", entities=("alphaline", "betaline"),
                 axes=("axis one", "axis two")):
    """A scripted derivation output (duck-typed like the pydantic parse)."""
    return SimpleNamespace(mode=mode, entities=list(entities), axes=list(axes))


def _base_script():
    """search → answer (one verified claim) → compose."""
    return [
        AgentStep(action="search", query="baseline metric reading cohort"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="baseline reading reported", atom_id="a1",
                     quote="baseline metric reading is 9 percent")]),
        ComposedAnswer(answer="The baseline reading is 9 percent [1]."),
    ]


# ---- derive_contract: parse / validate / fail-safe -------------------------------------------

def test_derive_contract_parses_and_normalizes():
    llm = RecordingLLM([SimpleNamespace(
        mode=" Enumerative ", entities=["  alphaline ", "", 7, "betaline"], axes=[" axis one "])])
    c = asyncio.run(derive_contract("q?", llm, _PROMPT))
    assert c == Contract(mode="enumerative", entities=["alphaline", "betaline"], axes=["axis one"])
    assert llm.systems == [_PROMPT]


def test_derive_contract_parses_probe_entities_capped_and_deduped():
    # enum-probe: probe_entities parsed, stripped, deduped case-insensitively, capped at ENUM_PROBE_ENTITY_CAP
    # (24, for a rich comparison-table roster) — entities stay empty (rows evidence-discovered).
    from roster_kernel.research.contract import ENUM_PROBE_ENTITY_CAP
    llm = RecordingLLM([SimpleNamespace(mode="enumerative", entities=[], axes=["pricing"],
        probe_entities=["Claude Code", " claude code ", "Cursor", "Copilot", "a", "b", "c", "d", "e", "f"])])
    c = asyncio.run(derive_contract("q?", llm, _PROMPT))
    assert c.entities == []                                     # rows still discovered from evidence
    assert c.probe_entities[:3] == ["Claude Code", "Cursor", "Copilot"]  # dedup drops the case-variant
    assert len(c.probe_entities) == 9                           # 10 in, 1 case-dup dropped, 9 < cap → all kept
    # cap check: >cap entities are truncated
    many = RecordingLLM([SimpleNamespace(mode="enumerative", entities=[], axes=["x"],
        probe_entities=[f"co{i}" for i in range(40)])])
    assert len(asyncio.run(derive_contract("q?", many, _PROMPT)).probe_entities) == ENUM_PROBE_ENTITY_CAP


def test_derive_contract_unions_probe_entities_across_winning_votes():
    # a terse winning-mode vote that omits a flagship must NOT drop it — union across winners.
    llm = RecordingLLM([
        SimpleNamespace(mode="enumerative", entities=[], axes=["pricing", "model"], probe_entities=["Cursor"]),
        SimpleNamespace(mode="enumerative", entities=[], axes=["pricing"], probe_entities=["Claude Code"]),
        SimpleNamespace(mode="enumerative", entities=[], axes=["pricing"], probe_entities=["Cursor"]),
    ])
    c = asyncio.run(derive_contract("q?", llm, _PROMPT, votes=3))
    assert set(c.probe_entities) == {"Cursor", "Claude Code"}   # Claude Code survives the terser votes
    assert c.axes == ["pricing", "model"]                       # richest winner still chosen for columns


def test_derive_contract_invalid_mode_is_none():
    llm = RecordingLLM([SimpleNamespace(mode="banana", entities=["x"], axes=["y"])])
    assert asyncio.run(derive_contract("q?", llm, _PROMPT)) is None


def test_derive_contract_llm_error_is_none():
    assert asyncio.run(derive_contract("q?", RaisingLLM(), _PROMPT)) is None


def test_derive_contract_no_prompt_is_none_without_llm_call():
    llm = RecordingLLM([])                  # any call would pop from an empty script and raise
    assert asyncio.run(derive_contract("q?", llm, None)) is None
    assert asyncio.run(derive_contract("q?", llm, "   ")) is None
    assert llm.prompts == []


def test_derive_contract_majority_vote_picks_richest_winner():
    # 3 votes: 2 enumerative (one richer) + 1 exploratory → majority enumerative, richest axes kept.
    llm = RecordingLLM([
        SimpleNamespace(mode="enumerative", entities=[], axes=["a", "b"]),
        SimpleNamespace(mode="enumerative", entities=[], axes=["a", "b", "c"]),
        SimpleNamespace(mode="exploratory", entities=[], axes=["x"])])
    c = asyncio.run(derive_contract("q?", llm, _PROMPT, votes=3))
    assert c is not None and c.mode == "enumerative" and c.axes == ["a", "b", "c"]


def test_derive_contract_vote_survives_a_lone_none():
    # one failed vote (invalid mode → None) can't swing the majority.
    llm = RecordingLLM([
        SimpleNamespace(mode="enumerative", entities=[], axes=["a"]),
        SimpleNamespace(mode="enumerative", entities=[], axes=["a"]),
        SimpleNamespace(mode="banana", entities=[], axes=[])])
    c = asyncio.run(derive_contract("q?", llm, _PROMPT, votes=3))
    assert c is not None and c.mode == "enumerative"


def test_derive_contract_enumerative_with_axes_no_entities_stays_enumerative():
    # "build me a table of all X": the DIMENSIONS (axes) are named, the row items are not — they get
    # discovered from evidence. This MUST stay enumerative (the old coercion-to-exploratory was the bug
    # that made such asks never tabulate).
    llm = RecordingLLM([SimpleNamespace(mode="enumerative", entities=[], axes=["value", "roi"])])
    c = asyncio.run(derive_contract("q?", llm, _PROMPT))
    assert c is not None and c.mode == "enumerative" and c.entities == [] and c.axes == ["value", "roi"]


def test_derive_contract_enumerative_no_entities_no_axes_degrades_to_exploratory():
    # nothing to tabulate — no rows AND no columns — still degrades to exploratory (inert).
    llm = RecordingLLM([SimpleNamespace(mode="enumerative", entities=[], axes=[])])
    c = asyncio.run(derive_contract("q?", llm, _PROMPT))
    assert c is not None and c.mode == "exploratory" and c.entities == []


def test_build_legs_enumerative_no_entities_uses_axis_only():
    # discovered-entity enumerative → axis-only legs (like exploratory), so rows can be found.
    c = Contract(mode="enumerative", entities=[], axes=["value", "roi", "limits"])
    assert build_legs(c) == ["value", "roi", "limits"]


# ---- ENUM-PROBE (flag ROSTER_ENUM_ENTITY_PROBE): probe_entities SEED targeted retrieval, never rows -----

def test_build_legs_probe_off_is_axis_only_byte_identical():
    # probe_entities present but probe=False (flag off) → axis-only, exactly today's behavior.
    c = Contract(mode="enumerative", entities=[], axes=["pricing", "model"],
                 probe_entities=["Claude Code", "Cursor"])
    assert build_legs(c) == ["pricing", "model"]
    assert build_legs(c, probe=False) == ["pricing", "model"]


def test_build_legs_probe_on_fires_axis_then_one_bundled_leg_per_entity():
    # probe=True → a few axis-only legs FIRST (discovery), then ONE BUNDLED leg per probe entity
    # ("<entity> <all axes>") — one comprehensive search per named company so a big roster fits the budget.
    c = Contract(mode="enumerative", entities=[], axes=["pricing", "model"],
                 probe_entities=["Claude Code", "Cursor", "Copilot"])
    legs = build_legs(c, cap=30, probe=True)
    assert legs[:2] == ["pricing", "model"]                       # a few axis-only legs first
    assert legs[2:] == ["Claude Code pricing model", "Cursor pricing model", "Copilot pricing model"]
    # each named company gets its OWN comprehensive leg — the roster fix
    assert "Claude Code pricing model" in legs


def test_build_legs_probe_needs_flag_entities_empty_and_axes():
    # probe only fires for enumerative + no user-named entities + probe_entities + axes.
    named = Contract(mode="enumerative", entities=["A"], axes=["x"], probe_entities=["B"])
    assert build_legs(named, probe=True) == ["x", "A x"]          # user-named entities win; probe inert
    no_axes = Contract(mode="enumerative", entities=[], axes=[], probe_entities=["B"])
    assert build_legs(no_axes, probe=True) == []                 # no axes → nothing to probe


def test_probe_entities_never_become_rows():
    # render_contract_directive reads `entities`, NOT `probe_entities` — rows stay evidence-discovered.
    from roster_kernel.research.contract import render_contract_directive
    out = render_contract_directive(voice="V", shapes={"enumerative": "S"}, default="D",
                                    mode="enumerative", entities=[], axes=["pricing"])
    assert "ITEMS to enumerate" not in out                       # no forced rows from a probe
    assert "DIMENSIONS" in out


# ---- build_legs: axis-only coverage first, then entity × axis round-robin, cap, dedupe --------

def test_build_legs_axis_only_first_then_round_robin_axis_major():
    c = Contract(mode="enumerative", entities=["e1", "e2", "e3"], axes=["x", "y"])
    assert build_legs(c) == ["x", "y",                       # every axis covered first
                             "e1 x", "e2 x", "e3 x", "e1 y", "e2 y", "e3 y"]


def test_build_legs_cap_axes_covered_before_entity_breadth():
    # the act-001 starvation regression: with a small cap, later axes must STILL get a leg
    c = Contract(mode="enumerative",
                 entities=["e1", "e2", "e3", "e4", "e5"], axes=["x", "y", "z"])
    legs = build_legs(c, cap=8)
    assert len(legs) == 8
    assert legs[:3] == ["x", "y", "z"]                       # all axes covered first
    assert legs[3:] == ["e1 x", "e2 x", "e3 x", "e4 x", "e5 x"]


def test_build_legs_default_cap_12():
    c = Contract(mode="enumerative",
                 entities=["e1", "e2", "e3", "e4", "e5"], axes=["x", "y", "z"])
    assert len(build_legs(c)) == 12


def test_build_legs_excludes_graph_queries_case_insensitive():
    c = Contract(mode="enumerative", entities=["e1", "e2"], axes=["x"])
    assert build_legs(c, exclude={"E1 X"}) == ["x", "e2 x"]


def test_build_legs_none_or_exploratory_without_axes_is_empty():
    assert build_legs(None) == []
    # exploratory WITH axes now yields axis-only legs (see test_explore_legs.py) — the empty
    # result is only for a contract with nothing to retrieve on
    assert build_legs(Contract(mode="exploratory", entities=["e1"], axes=[])) == []


def test_build_legs_no_axes_uses_entity_alone():
    c = Contract(mode="enumerative", entities=["e1", "e2"], axes=[])
    assert build_legs(c) == ["e1", "e2"]


# ---- match_entities: structural containment ---------------------------------------------------

def test_match_entities_in_text_and_title_case_insensitive():
    ents = ["Alphaline", "betaline", "zetaline"]
    assert match_entities(ents, "ALPHALINE value reported") == ["Alphaline"]
    assert match_entities(ents, "value reported", "THE BETALINE LABEL") == ["betaline"]
    assert match_entities(ents, "nothing here", "no title match") == []


# ---- shadow: +1 call, no leg retrieval, diag carries everything, otherwise byte-identical ------

def _run(llm, source, *, mode="", prompt=_PROMPT, budget=None, diag=True, cap=30,
         graph_legs=None, graph_late=False):
    return asyncio.run(run_react(
        question="what is the baseline metric?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=source, tenant_id="A", budget=budget or BudgetState(max_calls=20),
        question_contract=mode, contract_prompt=prompt, compose_claim_cap=cap,
        collect_diagnostics=diag, graph_legs=graph_legs, graph_late=graph_late, max_steps=4))


def test_shadow_no_leg_retrieval_and_diag_logs_contract_and_legs():
    src = CountingSource(_source())
    llm = RecordingLLM([_contract_ns()] + _base_script())
    res = _run(llm, src, mode="shadow")
    assert res.grounded
    # ONLY the planner's own search hit the source — legs were computed, never retrieved
    assert src.searches == [("baseline metric reading cohort", 10)]
    qc = res.diagnostics["question_contract"]
    assert qc["mode"] == "shadow"
    assert qc["contract"] == {"mode": "enumerative",
                              "entities": ["alphaline", "betaline"],
                              "axes": ["axis one", "axis two"]}
    assert [d["query"] for d in qc["legs"]] == [
        "axis one", "axis two",
        "alphaline axis one", "betaline axis one", "alphaline axis two", "betaline axis two"]
    assert all("hits" not in d and "merged" not in d for d in qc["legs"])   # never retrieved
    assert "slot_grid" in qc                     # logged at the end (grid over final claims)


def test_shadow_is_byte_identical_beyond_one_charged_call():
    b_off, b_sh = BudgetState(max_calls=20), BudgetState(max_calls=20)
    llm_off = RecordingLLM(_base_script())
    res_off = _run(llm_off, _source(), mode="", budget=b_off, diag=False)
    llm_sh = RecordingLLM([_contract_ns()] + _base_script())
    res_sh = _run(llm_sh, _source(), mode="shadow", budget=b_sh, diag=False)
    # every planner/compose prompt string is EXACTLY the OFF run's (the derivation call is extra)
    assert llm_sh.prompts[1:] == llm_off.prompts
    assert res_sh.composed_answer == res_off.composed_answer
    assert [vc.text for vc in res_sh.verified_claims] == [vc.text for vc in res_off.verified_claims]
    assert res_sh.coverage_gaps == res_off.coverage_gaps       # shadow never writes gaps
    assert b_sh.spent_calls == b_off.spent_calls + 1           # exactly the derivation charge


def test_flag_on_without_vertical_prompt_is_fully_off():
    b_off, b_st = BudgetState(max_calls=20), BudgetState(max_calls=20)
    llm_off = RecordingLLM(_base_script())
    _run(llm_off, _source(), mode="", budget=b_off, diag=False)
    llm_st = RecordingLLM(_base_script())                      # no derivation step scripted
    res = _run(llm_st, _source(), mode="steer", prompt=None, budget=b_st, diag=False)
    assert res.grounded
    assert llm_st.prompts == llm_off.prompts                   # not even a derivation call
    assert b_st.spent_calls == b_off.spent_calls


def test_derivation_failure_in_steer_falls_back_to_todays_behavior():
    src = CountingSource(_source())
    llm = RecordingLLM([SimpleNamespace(mode="banana", entities=[], axes=[])] + _base_script())
    res = _run(llm, src, mode="steer")
    assert res.grounded
    assert src.searches == [("baseline metric reading cohort", 10)]   # no legs
    assert res.diagnostics["question_contract"]["contract"] is None
    assert res.coverage_gaps == []


# ---- steer: legs concurrent, cap 8, k=4, late-merged, deduped vs graph legs --------------------

def test_steer_executes_capped_legs_concurrently_k4():
    src = CountingSource(_source())
    llm = RecordingLLM([_contract_ns(
        entities=("entone", "enttwo", "entthree", "entfour", "entfive"),
        axes=("axone", "axtwo"))] + _base_script())
    res = _run(llm, src, mode="steer")
    assert res.grounded
    leg_searches = [(q, k) for q, k in src.searches if k == 4]
    # concurrent execution → the recorded ARRIVAL order is nondeterministic; the leg SET and
    # the computed (diag) order are exact: cap 8, round-robin (every entity's first axis first)
    expected = [
        "axone", "axtwo",
        "entone axone", "enttwo axone", "entthree axone", "entfour axone", "entfive axone",
        "entone axtwo", "enttwo axtwo", "entthree axtwo", "entfour axtwo", "entfive axtwo"]
    assert sorted(q for q, _ in leg_searches) == sorted(expected)   # cap 12: axes + all 10
    assert [d["query"] for d in res.diagnostics["question_contract"]["legs"]] == expected
    assert src.max_in_flight >= 2                              # asyncio.gather — concurrent
    # baseline retrieval unchanged and mandatory
    assert ("baseline metric reading cohort", 10) in src.searches


def test_steer_legs_late_merge_planner_window_unaffected():
    src = _source(extra_leg_block=True)
    llm = RecordingLLM([_contract_ns(entities=("alphaline",), axes=("axis one",))]
                       + _base_script())
    res = _run(llm, src, mode="steer")
    assert res.grounded
    # mid-loop the planner NEVER saw the leg's atom (late merge — same seam as graph legs)
    answer_step_prompt = llm.prompts[2]                        # derivation, search, THIS
    assert _LEG_TEXT not in answer_step_prompt
    assert f"a1: {_BASE_TEXT}" in answer_step_prompt
    # ...but post-loop the leg evidence joined the pool
    qc = res.diagnostics["question_contract"]
    # the axis-only leg retrieves the block first and owns the merge; the entity leg's
    # identical hit then dedupes (merged 0) — the atom still lands exactly once
    assert {"query": "axis one", "hits": 1, "merged": 1} in qc["legs"]
    assert {"query": "alphaline axis one", "hits": 1, "merged": 0} in qc["legs"]
    assert res.atoms_gathered == 2


def test_steer_legs_dedupe_against_graph_legs():
    src = _source(extra_leg_block=True)
    llm = RecordingLLM([_contract_ns(entities=("alphaline", "betaline"), axes=("axis one",))]
                       + _base_script())
    res = _run(llm, src, mode="steer",
               graph_legs=[{"query": "alphaline axis one", "note": "graph edge"}],
               graph_late=True)
    qc = res.diagnostics["question_contract"]
    # the graph leg already owns "alphaline axis one" → the contract leg set excludes it
    assert [d["query"] for d in qc["legs"]] == ["axis one", "betaline axis one"]


# ---- steer: slot-aware compose selection (panel amendment A1) ----------------------------------

_SLOT_TEXT = ("alphaline value one reported. betaline value two reported. "
              "gammaline value three reported. deltaline value four reported.")


def _slot_source() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_SLOT_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
        document_title="METRICS DOC", source_key="corpus"))
    return src


def _claim(name: str, quote: str) -> ClaimOut:
    return ClaimOut(text=f"{name} value reported", atom_id="a1", quote=quote)


_FOUR_CLAIMS = [                                # OFF-SLOT claims deliberately FIRST (first-come)
    _claim("gammaline", "gammaline value three reported"),
    _claim("deltaline", "deltaline value four reported"),
    _claim("alphaline", "alphaline value one reported"),
    _claim("betaline", "betaline value two reported"),
]


def _slot_script(claims=_FOUR_CLAIMS, contract=True, mode="enumerative"):
    steps = [
        AgentStep(action="search", query="alphaline betaline gammaline deltaline reported"),
        AgentStep(action="answer", claims=list(claims)),
        ComposedAnswer(answer="Values reported [1]."),
    ]
    return ([_contract_ns(mode=mode)] + steps) if contract else steps


def test_slot_fillers_evict_off_slot_claims_never_the_reverse():
    llm = RecordingLLM(_slot_script())
    res = _run(llm, _slot_source(), mode="steer", cap=2)
    # the two SLOT-FILLING claims won the two seats — the first-come off-slot pair is out
    assert [vc.text for vc in res.verified_claims] == \
        ["alphaline value reported", "betaline value reported"]


def test_leftover_seats_filled_by_existing_order_after_slots():
    llm = RecordingLLM(_slot_script())
    res = _run(llm, _slot_source(), mode="steer", cap=3)
    # slots reserved first (alphaline, betaline), leftover seat = best remaining (gammaline);
    # final list keeps the base (first-come) ordering — membership-only selection
    assert [vc.text for vc in res.verified_claims] == \
        ["gammaline value reported", "alphaline value reported", "betaline value reported"]


def test_round_robin_every_covered_entity_represented():
    claims = [
        ClaimOut(text="alphaline first fact", atom_id="a1", quote="alphaline value one reported"),
        ClaimOut(text="alphaline second fact", atom_id="a1", quote="alphaline value one"),
        ClaimOut(text="betaline fact", atom_id="a1", quote="betaline value two reported"),
    ]
    llm = RecordingLLM(_slot_script(claims=claims))
    res = _run(llm, _slot_source(), mode="steer", cap=2)
    # NOT alphaline's two best — every covered entity gets a seat before any gets a second
    assert [vc.text for vc in res.verified_claims] == ["alphaline first fact", "betaline fact"]


def test_selection_off_and_exploratory_are_byte_identical():
    # OFF: today's path never trims loop-emitted claims (no ranking flag on)
    llm_off = RecordingLLM(_slot_script(contract=False))
    res_off = _run(llm_off, _slot_source(), mode="", cap=2)
    assert len(res_off.verified_claims) == 4
    # steer + EXPLORATORY contract: selection untouched too
    llm_ex = RecordingLLM(_slot_script(mode="exploratory"))
    res_ex = _run(llm_ex, _slot_source(), mode="steer", cap=2)
    assert [vc.text for vc in res_ex.verified_claims] == \
        [vc.text for vc in res_off.verified_claims]
    assert res_ex.coverage_gaps == res_off.coverage_gaps == []


def test_shadow_never_selects_but_logs_slot_grid():
    llm = RecordingLLM(_slot_script())
    res = _run(llm, _slot_source(), mode="shadow", cap=2)
    assert len(res.verified_claims) == 4                       # untouched
    assert res.coverage_gaps == []                             # no steer gaps
    assert res.diagnostics["question_contract"]["slot_grid"] == \
        {"alphaline": 1, "betaline": 1}


# ---- steer: honest loop-produced coverage gaps -------------------------------------------------

def test_coverage_gap_for_entity_with_zero_claims():
    llm = RecordingLLM(_slot_script())
    # zetaline exists in the contract but NOTHING in the corpus/claims mentions it
    llm._steps[0] = _contract_ns(entities=("alphaline", "betaline", "zetaline"))
    res = _run(llm, _slot_source(), mode="steer", cap=3)
    assert "No evidence retrieved for zetaline (axis one, axis two)" in res.coverage_gaps
    assert not any("alphaline" in g for g in res.coverage_gaps)
    assert res.diagnostics["question_contract"]["slot_grid"]["zetaline"] == 0
