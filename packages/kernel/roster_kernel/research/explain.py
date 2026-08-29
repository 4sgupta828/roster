"""Layman re-explanation — transform a grounded answer into plain language.

This is a faithful REPHRASING of an already-produced answer for a non-expert audience, not
new research: the model is given only the answer and instructed to keep its essence and
accuracy while dropping jargon. It adds no new facts, so it inherits the grounding of the
answer it rephrases. The domain voice (how a clinician explains to a patient) lives in the
vertical's `layman_prompt`; the kernel owns only the mechanics.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from roster_kernel.providers.llm import LLMClient


class LaymanExplanation(BaseModel):
    explanation: str


_MARKERS = re.compile(r"\[\[/?[FRK]\]\]")


async def explain_for_layperson(*, llm: LLMClient, layman_prompt: str,
                                question: str, answer: str, max_tokens: int = 0) -> str:
    """Return a plain-language rephrasing of `answer`, or "" if there's nothing to explain."""
    clean = _MARKERS.sub("", answer or "").strip()   # drop highlight markers
    if not clean:
        return ""
    if not max_tokens:
        # SCALE the cap to the answer being rephrased: a fixed 1600 truncated the structured
        # emit on long multi-part answers (prod failure). A plain-language rewrite runs about
        # the answer's length (~1 token per ~4 chars) — give 2× headroom, floor at the old
        # default, cap sanely.
        max_tokens = min(8000, max(1600, len(clean) // 2))
    user = (f"QUESTION: {question}\n\nCLINICAL ANSWER (rephrase THIS for the patient — use "
            f"ONLY what is here; add no new facts, drugs, numbers, or claims):\n{clean}")
    res = await llm.complete(
        system=layman_prompt,
        messages=[{"role": "user", "content": user}],
        response_format=LaymanExplanation, max_tokens=max_tokens)
    return (res.parsed.explanation or "").strip()
