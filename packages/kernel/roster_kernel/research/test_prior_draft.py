"""draft_prior (ROSTER_PARAMETRIC_LED, T1): one structured call → PriorDraft, fail-safe → None.

Sync tests with asyncio.run (the contamination-free sibling pattern; no event-loop hand-rolling).
"""
from __future__ import annotations

import asyncio

from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.prior_draft import AssertedClaim, PriorDraft, draft_prior


class _FakeLLM:
    """Returns a fixed PriorDraft and records that it was called (+ output_tokens for the charge)."""

    def __init__(self, draft: PriorDraft, output_tokens: int = 42):
        self._draft = draft
        self._output_tokens = output_tokens
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.calls += 1
        assert response_format is PriorDraft          # the module asks for the structured draft
        assert messages[0]["content"]                 # the question rides the user turn
        return LLMResult(parsed=self._draft, output_tokens=self._output_tokens, model="fake")


class _BoomLLM:
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        raise RuntimeError("provider down")


_DRAFT = PriorDraft(
    outline="## Landscape\n## Leaders\n## Outlook",
    claims=[
        AssertedClaim(text="Company X raised $200M in 2024", kind="fact",
                      needs_freshness=True, verify_query="Company X $200M Series C 2024"),
        AssertedClaim(text="The space is consolidating around a few platforms", kind="reasoning"),
    ],
)


def test_draft_prior_returns_parsed_and_charges_budget() -> None:
    llm = _FakeLLM(_DRAFT, output_tokens=42)
    budget = BudgetState(max_calls=10)
    out = asyncio.run(draft_prior("What is the landscape?", llm, "DRAFT PROMPT", budget=budget))
    assert isinstance(out, PriorDraft)
    assert out.outline.startswith("## Landscape")
    assert len(out.claims) == 2
    assert out.claims[0].kind == "fact" and out.claims[0].verify_query
    assert out.claims[1].kind == "reasoning"
    assert llm.calls == 1
    assert budget.spent_calls == 1          # charge(calls=1) on success
    assert budget.spent_tokens == 42        # tokens = res.output_tokens


def test_draft_prior_none_llm_returns_none_no_charge() -> None:
    budget = BudgetState(max_calls=10)
    out = asyncio.run(draft_prior("Q", None, "DRAFT PROMPT", budget=budget))
    assert out is None
    assert budget.spent_calls == 0


def test_draft_prior_blank_prompt_returns_none() -> None:
    llm = _FakeLLM(_DRAFT)
    budget = BudgetState(max_calls=10)
    out = asyncio.run(draft_prior("Q", llm, "   ", budget=budget))
    assert out is None
    assert llm.calls == 0                   # never reaches the provider
    assert budget.spent_calls == 0


def test_draft_prior_blank_question_returns_none() -> None:
    llm = _FakeLLM(_DRAFT)
    budget = BudgetState(max_calls=10)
    out = asyncio.run(draft_prior("  ", llm, "DRAFT PROMPT", budget=budget))
    assert out is None
    assert llm.calls == 0


def test_draft_prior_exception_returns_none_failsafe() -> None:
    budget = BudgetState(max_calls=10)
    out = asyncio.run(draft_prior("Q", _BoomLLM(), "DRAFT PROMPT", budget=budget))
    assert out is None                      # ANY exception → None → today's path
    assert budget.spent_calls == 0          # no charge on failure
