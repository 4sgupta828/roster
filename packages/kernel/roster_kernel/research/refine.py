"""Pre-answer question REFINEMENT — propose a few distinct, sharper versions of a FRESH question.

Before answering, offer the user a small set of more-specific, more-answerable standalone questions
to pick from (express refinement — optimize for a better answer right away). This is NOT clarification
(which fires only on genuine ambiguity of a follow-up); it is proactive scoping of a fresh question,
and the user can always proceed with their original wording. The domain framing (what a "sharper"
medical question is) lives in the vertical's `refine_prompt`; the kernel owns only the mechanics.

These are QUESTIONS, not answers — picking one runs the normal grounded loop, so nothing here
bypasses the grounding invariant. Returns [] when the question is already precise (nothing useful to
refine) OR on any failure — so the caller falls straight through to a normal answer.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from roster_kernel.providers.llm import LLMClient


class Refinements(BaseModel):
    # Empty when the question is already specific enough to answer well as-is.
    questions: list[str] = Field(default_factory=list,
                                 description="0, or 3–4 DISTINCT sharper standalone questions")


async def refine_question(
    *, llm: LLMClient, refine_prompt: str, question: str, max_tokens: int = 320,
) -> list[str]:
    """Return 3–4 DISTINCT refined questions, or [] (already precise / nothing useful / error).

    Structural gate (code owns structure, not meaning): fewer than 2 distinct options is not a
    meaningful choice → return [] so the FE just answers the original question."""
    user = (
        f"USER QUESTION:\n{question}\n\n"
        "Propose a few DISTINCT, sharper, self-contained questions the user most likely means — each a "
        "different meaningful angle/scope (e.g. a specific population, outcome, intervention, comparison, "
        "or setting), NOT reworded near-duplicates. If the question is ALREADY specific enough to answer "
        "well as-is, return an EMPTY list — do not invent choices for a clear question.")
    try:
        res = await llm.complete(
            system=refine_prompt,
            messages=[{"role": "user", "content": user}],
            response_format=Refinements, max_tokens=max_tokens)
    except Exception:
        return []                       # fail-safe: any error → no refinement, answer the original
    seen, out = set(), []
    for q in (res.parsed.questions or []):
        s = (q or "").strip()
        if s and s.lower() not in seen and s.lower() != (question or "").strip().lower():
            seen.add(s.lower()); out.append(s)
    return out[:4] if len(out) >= 2 else []
