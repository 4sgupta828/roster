"""PriorDraft — the parametric-led DRAFT stage (ROSTER_PARAMETRIC_LED, T1).

ONE strong-model call produces a structured `PriorDraft`: an answer OUTLINE (sections/axes,
structure only — no facts) plus the discrete `AssertedClaim`s the answer would make, each tagged
`kind` (a checkable FACT vs the model's REASONING/synthesis). For each FACT the model also emits
`needs_freshness` and a targeted `verify_query`. This is the model reasoning/structuring from its
INTEGRATED knowledge BEFORE any retrieval; T2 verifies every fact against the existing span-gate,
T3 recomposes from the verified + labeled-unverified register. T1 only produces the draft and
threads it inertly (unused until T2/T3).

Domain-free (Rule 18): the kernel owns the MECHANICS (one structured call, budget charge, fail-safe);
the JUDGMENT (what is a fact vs reasoning, the verify query) is the LLM's, steered entirely by the
VERTICAL-supplied prompt injected as `prompt`. The kernel names no domain concept.

Fail-safe (Rule 20): no llm / blank prompt / blank question / ANY exception → None → the caller
falls back to today's retrieve-first path byte-identical.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from roster_kernel.providers.llm import LLMClient
from roster_kernel.research.budget import BudgetState

logger = logging.getLogger(__name__)


class AssertedClaim(BaseModel):
    """One discrete assertion the parametric draft would make. `kind` separates a checkable FACT
    (number/date/name/event/attribution — anything that could be right or wrong, to be verified
    against retrieval in T2) from the model's own REASONING/synthesis. Defaults make a partial or
    abstained emission parse safely as an inert fact-less reasoning claim."""
    text: str
    kind: str = "fact"                 # "fact" | "reasoning"
    needs_freshness: bool = False      # (fact only) could it have changed recently → force retrieval
    verify_query: str = ""             # (fact only) targeted search string to corroborate THIS claim


class PriorDraft(BaseModel):
    """The pre-retrieval parametric draft: an answer OUTLINE (structure only) + the discrete
    claims it would assert. Defaults make an empty/partial emission an inert no-op draft."""
    outline: str = ""
    claims: list[AssertedClaim] = []


async def draft_prior(question: str, llm, prompt: str | None, *,
                      budget: BudgetState, max_tokens: int = 5000) -> PriorDraft | None:
    """ONE structured LLM call → PriorDraft, or None on ANY failure. Fail-safe is today's
    retrieve-first path — never a heuristic guess (Rule 18). Charges the budget on success."""
    if llm is None or not (prompt or "").strip() or not (question or "").strip():
        return None
    try:
        res = await llm.complete(system=prompt,
                                 messages=[{"role": "user", "content": question}],
                                 response_format=PriorDraft, max_tokens=max_tokens)
        budget.charge(calls=1, tokens=getattr(res, "output_tokens", 0))
        return res.parsed
    except Exception:   # noqa: BLE001 — fail-safe: the draft must never break the answer path
        logger.debug("draft_prior failed; falling back to retrieve-first path", exc_info=True)
        return None
