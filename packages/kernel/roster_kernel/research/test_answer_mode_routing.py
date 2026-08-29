"""Evidence Contract stage 4 — answer-mode routing (ROSTER_ANSWER_MODE_ROUTING).

The enumerative-compose ADDENDUM (a vertical-owned opaque string) is appended to the existing
compose directive ONLY when: (a) the flag is on, (b) the derived QuestionContract says
mode=enumerative, AND (c) ≥2 contract entities hold at least one slot-matched claim in the FINAL
verified selection (panel A3: never trust the pre-retrieval contract alone for compose routing).
Any other combination → the compose user prompt is BYTE-IDENTICAL to today. Offline throughout —
scripted LLMs, no network (mirrors test_question_contract.py, whose helpers it reuses)."""
from __future__ import annotations

import asyncio

from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import run_react
from roster_kernel.research.test_question_contract import (
    _PROMPT,
    RecordingLLM,
    _contract_ns,
    _slot_script,
    _slot_source,
)

_ADDENDUM = "ENUMERATIVE ADDENDUM: lead with the per-item table."
_BASE_DIRECTIVE = "BASE DIRECTIVE: shape the answer for the reader."


def _run(llm, *, mode="shadow", routing=True, addendum=_ADDENDUM, answer_format=None,
         cap=30, diag=True):
    return asyncio.run(run_react(
        question="what is the value for each item?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=_slot_source(), tenant_id="A", budget=BudgetState(max_calls=20),
        question_contract=mode, contract_prompt=_PROMPT,
        answer_mode_routing=routing, enumerative_compose_addendum=addendum,
        answer_format=answer_format, compose_claim_cap=cap,
        collect_diagnostics=diag, max_steps=4))


def _compose_prompt(llm: RecordingLLM) -> str:
    """The compose call is always the LAST scripted call (derivation, search, answer, compose)."""
    return llm.prompts[-1]


def _off_compose_prompt(*, answer_format=None) -> str:
    """The byte-identity baseline: same scripts, routing OFF."""
    llm = RecordingLLM(_slot_script())
    _run(llm, routing=False, answer_format=answer_format)
    return _compose_prompt(llm)


# ---- the addendum fires: flag + enumerative + ≥2 covered entities ------------------------------

def test_addendum_appended_when_flag_enumerative_and_two_covered():
    # default contract entities (alphaline, betaline) are both covered by the scripted claims
    llm = RecordingLLM(_slot_script())
    res = _run(llm)
    assert res.grounded
    prompt = _compose_prompt(llm)
    assert prompt.endswith("\n\n" + _ADDENDUM)
    # ...and it is EXACTLY the OFF prompt + the appended addendum (nothing else moved)
    assert prompt == _off_compose_prompt() + "\n\n" + _ADDENDUM
    assert res.diagnostics["question_contract"]["answer_mode"] == \
        {"routed": True, "covered_entities": 2}


def test_addendum_appends_after_base_directive_untouched():
    llm = RecordingLLM(_slot_script())
    _run(llm, answer_format=_BASE_DIRECTIVE)
    prompt = _compose_prompt(llm)
    # base directive intact, addendum after it — the protected baseline is never edited
    assert prompt.endswith("\n\n" + _BASE_DIRECTIVE + "\n\n" + _ADDENDUM)
    assert prompt == _off_compose_prompt(answer_format=_BASE_DIRECTIVE) + "\n\n" + _ADDENDUM


def test_addendum_fires_in_steer_mode_too():
    llm = RecordingLLM(_slot_script())
    res = _run(llm, mode="steer")
    assert _compose_prompt(llm).endswith("\n\n" + _ADDENDUM)
    assert res.diagnostics["question_contract"]["answer_mode"]["routed"] is True


# ---- everything else is byte-identical ---------------------------------------------------------

def test_flag_off_is_byte_identical():
    llm = RecordingLLM(_slot_script())
    res = _run(llm, routing=False)
    assert _compose_prompt(llm) == _off_compose_prompt()
    assert _ADDENDUM not in _compose_prompt(llm)
    # flag off → no routing decision is even recorded
    assert "answer_mode" not in res.diagnostics["question_contract"]


def test_exploratory_contract_is_byte_identical():
    llm = RecordingLLM(_slot_script(mode="exploratory"))
    _run(llm)
    assert _compose_prompt(llm) == _off_compose_prompt()


def test_single_covered_entity_is_byte_identical():
    # contract names two entities but only alphaline is covered by any claim → nothing to
    # enumerate → today's framing (panel A3: coverage, not the pre-retrieval contract, decides)
    llm = RecordingLLM(_slot_script())
    llm._steps[0] = _contract_ns(entities=("alphaline", "zetaline"))
    res = _run(llm)
    assert _compose_prompt(llm) == _off_compose_prompt()
    assert res.diagnostics["question_contract"]["answer_mode"] == \
        {"routed": False, "covered_entities": 1}


def test_no_addendum_supplied_is_byte_identical():
    llm = RecordingLLM(_slot_script())
    res = _run(llm, addendum=None)
    assert _compose_prompt(llm) == _off_compose_prompt()
    assert "answer_mode" not in res.diagnostics["question_contract"]


def test_no_contract_flag_is_byte_identical():
    # question-contract flag off entirely → no derivation, no routing (stage 4 needs stage 3)
    llm = RecordingLLM(_slot_script(contract=False))
    res = _run(llm, mode="")
    assert _ADDENDUM not in _compose_prompt(llm)
    assert res.question_contract is None


# ---- the derived contract is surfaced for session persistence (schema-registry phase 0) --------

def test_result_carries_question_contract_when_derived():
    llm = RecordingLLM(_slot_script())
    res = _run(llm, routing=False, diag=False)      # independent of routing AND diagnostics
    assert res.question_contract == {"mode": "enumerative",
                                     "entities": ["alphaline", "betaline"],
                                     "axes": ["axis one", "axis two"],
                                     "stance": ""}
