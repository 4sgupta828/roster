"""Suggested follow-up questions — propose a few next questions that deepen discovery.

After an answer, suggest 3–4 questions the user could ask next to go deeper: understand the
mechanism/evidence better, explore an adjacent angle, or move toward action. The domain framing
(what "deeper discovery, understanding, and action" means here) lives entirely in the vertical's
`suggest_prompt`; the kernel owns only the mechanics. These are QUESTIONS, not answers — asking
one runs the normal grounded loop, so nothing here bypasses the grounding invariant.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from roster_kernel.providers.llm import LLMClient


class Suggestion(BaseModel):
    question: str = ""
    tag: str = Field(default="", description="optional short lens/category label, vertical-supplied")


class Suggestions(BaseModel):
    questions: list[Suggestion] = Field(default_factory=list, description="a few follow-up questions")


async def suggest_followups(
    *, llm: LLMClient, suggest_prompt: str, question: str, answer: str,
    history: str = "", max_tokens: int = 700, cap: int = 6,
) -> list[dict]:
    """Return up to `cap` follow-up questions as `[{question, tag}]` (or [] if nothing useful).
    `tag` is an optional short lens/category label the vertical's prompt supplies (e.g. a persona);
    the kernel is agnostic to its values. Empty tag → the caller/UI simply shows no label."""
    clean = (answer or "").strip()
    user = (
        (f"CONVERSATION SO FAR:\n{history}\n\n" if history.strip() else "")
        + f"CURRENT QUESTION:\n{question}\n\n"
        + f"ANSWER GIVEN:\n{clean or '(no grounded answer was produced)'}\n\n"
        "Propose the next questions the user could ask. Each must be a single, self-contained "
        "question that stands on its own if clicked."
    )
    res = await llm.complete(
        system=suggest_prompt,
        messages=[{"role": "user", "content": user}],
        response_format=Suggestions, max_tokens=max_tokens)
    seen, out = set(), []
    for q in (res.parsed.questions or []):
        s = (getattr(q, "question", "") or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append({"question": s, "tag": (getattr(q, "tag", "") or "").strip()})
    return out[:cap]
