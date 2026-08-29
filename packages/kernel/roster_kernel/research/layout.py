"""ROSTER_ANSWER_LAYOUT — a grounding-safe PRESENTATION pass.

The compose call generates the answer in one shot while juggling grounding, register, depth, and
coverage — so layout is the first thing it drops, and long grounded answers come out as a wall of text.
This is a dedicated SECOND pass whose only job is to REFLOW the already-composed, already-span-gated
answer into a scannable, whiteboard-style layout (short paragraphs, bullets, tables, arrow-flows, bold
key terms). Separating presentation from content is what makes it reliable where nagging the overloaded
compose pass never was.

It is REFLOW-ONLY and validated in code (never trust the model not to launder new content — Rule 6):
after reflow we REJECT the result unless every [n] citation is preserved (no citation added or dropped)
AND no new hard token (number / % / $ / year) appears that was not in the original. On any violation or
error → return None → the caller keeps the original grounded answer. So the pass can only ever make the
SAME grounded answer more scannable, never less true. Kernel mechanic — the prompt names no domain noun.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from roster_kernel.research.budget import BudgetState
from roster_kernel.providers.llm import LLMClient

_LAYOUT_MAX_TOKENS = 4000

# Domain-free reflow directive. Presentation only; it must not reason about or alter the content.
LAYOUT_PROMPT = (
    "You are a layout editor. You are given a finished, fact-checked answer. Your ONLY job is to reflow "
    "it so it is easy to SCAN and read — like someone sketching it at a whiteboard. You must NOT change "
    "the meaning, add or remove any fact, number, name, date, claim, or citation, or write any new "
    "sentence of content.\n\n"
    "REFLOW RULES:\n"
    "- Break every long paragraph into short ones — at most 2-3 sentences each. No wall of text.\n"
    "- When several parallel things are listed in a sentence (players, factors, options, steps), pull "
    "them into a short BULLET list, one per line.\n"
    "- When the text compares two or more things across the same dimensions, render it as a small "
    "markdown TABLE.\n"
    "- When the text describes a process, pipeline, or sequence, show it as an inline arrow-flow "
    "(A -> B -> C).\n"
    "- Bold the few key terms or the lead phrase of a distinct point, sparingly.\n"
    "- Keep it conversational and light — do NOT add section headings, do NOT add a 'Summary'/'Key "
    "points'/'Conclusion' section, do NOT impose a report template.\n\n"
    "CRITICAL — PRESERVE EXACTLY: every [n] citation stays attached to the same fact it was on; every "
    "number, %, $, date, and name is unchanged; you add NO new number, statistic, or claim. You are only "
    "moving and formatting the words that are already there. Reproduce the full answer — do not shorten "
    "or drop content. Output ONLY the reflowed answer.")

_HARD = re.compile(r"\$\s?\d[\d,.]*|\b\d+(?:\.\d+)?\s?%|\b\d{4}\b|\b\d+(?:\.\d+)?\b")
_REF = re.compile(r"\[\d+\]")
_BULLET = re.compile(r"(?m)^\s*[-*]\s")
_TABLE = re.compile(r"(?m)^\s*\|.*\|")


def _needs_reflow(answer: str) -> bool:
    """ALWAYS reflow a substantial answer. A single compose pass reliably UNDER-formats (wall-of-text is
    the norm, not the exception), the reflow runs on the cheap main model, and it is grounding-guarded
    (a bad reflow is rejected and the original kept) — so there is no reason to skip it. The only skip is
    a trivially short answer (a line or two) where there is nothing to lay out."""
    return len(answer or "") >= 700


class _Reflowed(BaseModel):
    answer: str = ""


def _hard_tokens(t: str) -> set[str]:
    return set(m.group(0).replace(" ", "") for m in _HARD.finditer(t or ""))


def _refs(t: str) -> set[str]:
    return set(_REF.findall(t or ""))


async def reflow_for_scannability(answer: str, llm: LLMClient, prompt: str | None,
                                  *, budget: BudgetState) -> str | None:
    """Return a reflowed (more scannable) version of `answer`, or None to keep the original.

    None is returned on: empty input, LLM error, empty output, a citation added or dropped, a NEW hard
    token introduced, or a length that ballooned/gutted the content (a reflow should be near-isometric).
    COST GATE: returns None WITHOUT any LLM call when the answer is already scannable (`_needs_reflow`)."""
    if not answer or not answer.strip():
        return None
    if not _needs_reflow(answer):
        return None                                   # already scannable — no second pass, no cost
    try:
        comp = await llm.complete(
            system=(prompt or LAYOUT_PROMPT),
            messages=[{"role": "user", "content": answer}],
            response_format=_Reflowed, max_tokens=_LAYOUT_MAX_TOKENS)
        budget.charge(calls=1, tokens=getattr(comp, "output_tokens", 0) or 0)
        new = (comp.parsed.answer or "").strip()
    except Exception:   # noqa: BLE001 — the layout pass is presentation-only; never break the answer
        return None
    if not new:
        return None
    # GROUNDING GUARD (fail-closed): citations must be exactly preserved (no add, no drop), and no new
    # hard token may appear. A reflow that violates either is discarded — keep the original grounded text.
    if _refs(new) != _refs(answer):
        return None
    if not _hard_tokens(new).issubset(_hard_tokens(answer)):
        return None
    # Near-isometric: a reflow moves words, it does not rewrite. Reject a large size change either way.
    if len(new) < 0.55 * len(answer) or len(new) > 1.7 * len(answer):
        return None
    return new
