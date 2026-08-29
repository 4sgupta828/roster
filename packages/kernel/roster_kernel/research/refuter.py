"""Red-team REFUTER — cross-family disconfirming-query authoring (ROSTER_INTELLIGENCE_CORE, T-B).

Intelligence-core's adversarial retrieval runs a FOR leg and an AGAINST leg per hypothesis. Today
BOTH queries are written by the DRAFTING model (self-authored disconfirmation = grading its own
homework — soft/blind). This module lets a SEPARATE, DIFFERENT-family model author the disconfirming
("against") queries: a genuine red-team, a second uncorrelated mind whose ONLY job is to find the
evidence that would REFUTE the claim.

Design mirrors the cross-family grounding gate (`grounding_gate.py`): a closed pydantic shape,
temperature 0, an `LLMClient`-shaped `judge_llm` for stubbing, and — critically — FAIL-CLOSED:

  * judge_llm is None (no cross-family judge available)  → return []
  * blank claim                                          → return []
  * any judge error / malformed / empty output           → return []

`[]` means "no red-team queries" → the caller falls back to the hypothesis's self-authored
`against_query` (today's behavior). A refuter failure is NEVER worse than today. Rule 18: the refuter
owns the semantic call (what would disconfirm this?); this code owns only the mechanics + the
fail-closed contract + the budget charge.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

_log = logging.getLogger("roster.refuter")

# A short list of queries — cap output tokens tight (this is a cheap, narrow call).
_MAX_OUTPUT_TOKENS = 512


class RefuterQueries(BaseModel):
    """Closed shape the red-team judge returns: the disconfirming search queries. Empty ⇒ none."""
    queries: list[str] = []


_SYSTEM_PROMPT = """\
You are a skeptic. Given a CLAIM, produce up to N search queries that would surface the STRONGEST
DISCONFIRMING evidence against it — counterexamples, failures, contrary findings, negative results,
or data that would REFUTE it. Do not argue FOR the claim; do not restate it. Author searches whose
hits, if they exist, would undermine or falsify the claim.

Return ONE JSON object exactly matching this schema:

  {"queries": ["<disconfirming search query>", ...]}

Return only the queries (at most N), each a concrete search string. If nothing useful can be authored,
return {"queries": []}. Output ONLY the JSON object."""


async def refute_hypothesis(claim, judge_llm, *, budget, n: int = 2) -> list[str]:
    """One cross-family call: return up to `n` search queries that would surface the STRONGEST
    DISCONFIRMING evidence against `claim`.

    FAIL-CLOSED (never worse than today's self-authored against_query):
      judge_llm is None / blank claim / judge error / malformed / empty → return [] (the caller
      falls back to the hypothesis's own against_query).

    The judge MUST be a DIFFERENT model family than the drafter — that is the whole point (an
    uncorrelated red-team, not the drafter grading its own homework). The caller is responsible for
    passing a genuinely cross-family client here (and None otherwise, so this fails closed).
    """
    if judge_llm is None:
        return []
    c = (claim or "").strip()
    if not c:
        return []
    try:
        _n = int(n)
    except Exception:   # noqa: BLE001
        _n = 2
    if _n <= 0:
        return []

    try:
        res = await judge_llm.complete(
            system=_SYSTEM_PROMPT.replace("N", str(_n)),
            messages=[{"role": "user", "content": (
                f"CLAIM:\n{c}\n\nAuthor up to {_n} disconfirming search queries as JSON now.")}],
            response_format=RefuterQueries,
            max_tokens=_MAX_OUTPUT_TOKENS,
            temperature=0,
        )
    except Exception as e:   # noqa: BLE001 — a judge failure fails CLOSED (fall back), never worse
        _log.warning("refuter call failed: %r", e)
        return []

    # Charge the request budget for the judge call (charge-after, like the grounding gate). A metering
    # failure must not sink the result.
    try:
        if budget is not None and not getattr(budget, "exhausted", False):
            budget.charge(calls=1, tokens=int(getattr(res, "output_tokens", 0) or 0))
    except Exception:   # noqa: BLE001
        pass

    parsed = getattr(res, "parsed", None)
    queries = getattr(parsed, "queries", None)
    if not isinstance(queries, list):
        return []
    out: list[str] = []
    for q in queries:
        q = str(q).strip()
        if q:
            out.append(q)
        if len(out) >= _n:
            break
    return out


__all__ = ["refute_hypothesis", "RefuterQueries"]
