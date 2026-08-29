"""Tests for the reliable intelligence draft + line-protocol parser (ROSTER_INTELLIGENCE_CORE, T1).

Covers `parse_hypotheses` (pure structural parsing — Rule 18) and `draft_intelligence` (the one
structured LLM call + budget charge + fail-safe). `asyncio.run` drives the async draft in sync tests
(matching test_entity_open_leg.py — no hand-rolled loop).
"""
from __future__ import annotations

import asyncio

from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.intelligence_draft import (Hypothesis, IntelligenceDraft,
                                                      draft_intelligence, parse_hypotheses)


# ---- parse_hypotheses (structural) ------------------------------------------

def test_parse_clean_three_line_protocol() -> None:
    text = (
        "H1 | Growth is demand-driven | demand signals for X | X demand is flat or falling | usage metrics decline YoY\n"
        "H2 | Growth is hype-driven | X marketing spend surge | organic retention for X | churn stays high after launch\n"
        "H3 | There is no real growth | X adoption plateau evidence | X user counts rising | headline user counts keep climbing"
    )
    hyps = parse_hypotheses(text)
    assert len(hyps) == 3
    assert all(isinstance(h, Hypothesis) for h in hyps)
    assert hyps[0].claim == "Growth is demand-driven"
    assert hyps[0].for_query == "demand signals for X"
    assert hyps[0].against_query == "X demand is flat or falling"
    assert hyps[0].falsifier == "usage metrics decline YoY"
    assert hyps[2].claim == "There is no real growth"


def test_parse_tolerant_of_missing_trailing_fields() -> None:
    # A line with only claim + for-query still parses; the omitted trailing fields default to "".
    text = ("H1 | Claim A | for-query A\n"
            "H2 | Claim B | for-query B | against-query B")
    hyps = parse_hypotheses(text)
    assert len(hyps) == 2
    assert hyps[0].claim == "Claim A"
    assert hyps[0].for_query == "for-query A"
    assert hyps[0].against_query == ""
    assert hyps[0].falsifier == ""
    assert hyps[1].against_query == "against-query B"
    assert hyps[1].falsifier == ""


def test_parse_claim_only_line_still_parses() -> None:
    # Just a labeled claim, no pipes/queries → one Hypothesis with empty queries.
    hyps = parse_hypotheses("H1 | Only a bare claim")
    assert len(hyps) == 1
    assert hyps[0].claim == "Only a bare claim"
    assert hyps[0].for_query == "" and hyps[0].against_query == "" and hyps[0].falsifier == ""


def test_parse_label_optional() -> None:
    # No Hn label → the first field is the claim (label stripping is tolerant, not required).
    hyps = parse_hypotheses("The claim | for | against | falsifier")
    assert len(hyps) == 1
    assert hyps[0].claim == "The claim"
    assert hyps[0].for_query == "for"


def test_parse_skips_blank_and_garbage_lines() -> None:
    text = ("\n"
            "   \n"
            "H1 | Real hypothesis | fq | aq | fx\n"
            "H2 |    |   |   \n"          # label-only / empty claim → skipped
            "|||\n"                        # all-empty fields → skipped
            "H3 | Another real one | fq3 | aq3 | fx3\n")
    hyps = parse_hypotheses(text)
    assert len(hyps) == 2
    assert [h.claim for h in hyps] == ["Real hypothesis", "Another real one"]


def test_parse_empty_returns_empty_list() -> None:
    assert parse_hypotheses("") == []
    assert parse_hypotheses("   \n\n  ") == []


def test_fewer_than_two_is_the_callers_concern() -> None:
    # parse_hypotheses returns whatever is well-formed; the >=2 guard lives in the caller. A single
    # well-formed line yields exactly one Hypothesis (the caller will then fall back).
    hyps = parse_hypotheses("H1 | lonely hypothesis | fq | aq | fx")
    assert len(hyps) == 1


# ---- draft_intelligence (LLM call + budget + fail-safe) ----------------------

class _FakeLLM:
    """Returns a scripted IntelligenceDraft, recording the call. output_tokens feeds budget.charge."""

    def __init__(self, draft: IntelligenceDraft, output_tokens: int = 42) -> None:
        self._draft = draft
        self._output_tokens = output_tokens
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.calls += 1
        return LLMResult(parsed=self._draft, output_tokens=self._output_tokens, model="fake")


class _BoomLLM:
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        raise RuntimeError("llm exploded")


def test_draft_success_returns_parsed_and_charges_budget() -> None:
    draft = IntelligenceDraft(frame="the frame",
                              hypotheses_text="H1 | a | fa | aa | xa\nH2 | b | fb | ab | xb")
    llm = _FakeLLM(draft, output_tokens=77)
    budget = BudgetState(max_calls=80)
    before = budget.spent_calls
    out = asyncio.run(draft_intelligence("why is X growing?", llm, "PROMPT", budget=budget))
    assert out is draft
    assert llm.calls == 1
    # budget.charge(calls=1, tokens=77) was applied on success.
    assert budget.spent_calls == before + 1
    assert budget.spent_tokens >= 77


def test_draft_fail_safe_llm_none() -> None:
    budget = BudgetState(max_calls=80)
    out = asyncio.run(draft_intelligence("q", None, "PROMPT", budget=budget))
    assert out is None
    assert budget.spent_calls == 0


def test_draft_fail_safe_blank_prompt() -> None:
    llm = _FakeLLM(IntelligenceDraft(frame="f", hypotheses_text="H1 | a | | |"))
    budget = BudgetState(max_calls=80)
    out = asyncio.run(draft_intelligence("q", llm, "   ", budget=budget))
    assert out is None
    assert llm.calls == 0                    # never reached the LLM
    assert budget.spent_calls == 0


def test_draft_fail_safe_blank_question() -> None:
    llm = _FakeLLM(IntelligenceDraft())
    budget = BudgetState(max_calls=80)
    out = asyncio.run(draft_intelligence("", llm, "PROMPT", budget=budget))
    assert out is None
    assert llm.calls == 0


def test_draft_fail_safe_on_exception() -> None:
    budget = BudgetState(max_calls=80)
    out = asyncio.run(draft_intelligence("q", _BoomLLM(), "PROMPT", budget=budget))
    assert out is None
    assert budget.spent_calls == 0            # not charged on failure
