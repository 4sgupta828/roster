"""Guided intake / triage — a short clarifying conversation that converges on a crisp, answerable
question, then RECOMMENDS a route (Quick Q&A vs Specialist Panel).

The triage agent is a QUERY-FORMULATION CLARIFIER, not an advisor: it asks the minimum questions
needed to make the eventual grounded answer high-quality, then hands off. It never diagnoses,
recommends treatment, or gives medical advice — it only narrows intent and routes.

Domain framing (what a "material" clarification is, the medical routing boundary, the safety
guardrails) lives in the vertical's `triage_prompt`; the kernel owns only the turn mechanics and the
structural convergence cap (code owns structure, the LLM owns meaning — Rule 18):
  - the LLM decides, each turn, whether it needs one more clarification (`status="ask"`) or has
    enough to route (`status="ready"`);
  - the CALLER counts turns and passes `force_ready=True` once the hard cap is reached, so the
    conversation can never become an interrogation.

Nothing here answers the medical question — a `ready` turn only produces a refined question + a route;
running it goes through the normal grounded loop, so the grounding invariant is untouched.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from roster_kernel.providers.llm import LLMClient

_log = logging.getLogger(__name__)


class TriageTurn(BaseModel):
    # "ask" → keep clarifying (message = the ONE next question); "ready" → route (refined_question set).
    status: Literal["ask", "ready"] = "ask"
    message: str = ""                        # the clarifying question, or a brief handoff note
    understood_problem: str = ""             # cumulative crisp restatement of the case / intent
    refined_question: str = ""               # standalone question to run (required when ready)
    recommended_mode: Literal["qa", "panel"] = "qa"
    # Generic answer-MODALITY hint, kernel-opaque, meaning supplied by the vertical prompt (like
    # `register`). The vertical decides values (e.g. medical: "allopathic" | "alternative"); the caller
    # applies it to the routed question's retrieval scope. "" → the caller's default.
    modality: str = ""
    rationale: str = ""                      # one line: why this route
    # Generic, non-diagnostic urgency flag — the FE can surface a "seek urgent evaluation" notice.
    # NOT a diagnosis; the model is told to set this only for plainly emergent presentations.
    safety: Literal["ok", "urgent"] = "ok"


class TriageTurnV2(TriageTurn):
    """Intake v2 turn — GENERIC extensions only; all semantics come from the vertical prompt
    (the kernel stays domain-free: category names, register meaning, and the retrieval
    vocabulary are the vertical's, opaquely echoed through these fields):
      - `register`: the model's echoed turn-1 choice — "fact" (a factual/evidence lookup:
        converge immediately) vs "case" (a situation is being described: the caller may allow
        a deeper structured intake before forcing convergence);
      - `case_facts`: cumulative structured facts gathered so far ({category, text} — the
        category vocabulary is supplied by the vertical prompt);
      - `retrieval_terms`: the retrieval vocabulary used in the refined question (a display
        artifact for the caller's UI)."""
    register: Literal["fact", "case"] = "fact"
    case_facts: list[dict] = []              # items: {category: str, text: str}
    retrieval_terms: list[str] = []


def _last_user(transcript: list[dict]) -> str:
    for t in reversed(transcript or []):
        if (t.get("role") or "") == "user" and (t.get("text") or "").strip():
            return t["text"].strip()
    return ""


async def run_triage_turn(
    *, llm: LLMClient, triage_prompt: str, transcript: list[dict],
    roster_summary: str = "", force_ready: bool = False, max_tokens: int = 700,
    schema_v2: bool = False,
) -> TriageTurn:
    """Run ONE triage turn over the running transcript ([{role:"user"|"assistant", text}]) and return a
    validated TriageTurn. Fail-safe: any error → route the last user message straight to Quick Q&A so the
    user is never stuck. `force_ready` (caller-enforced turn cap) coerces a route this turn.
    `schema_v2` selects the TriageTurnV2 response schema (register/case_facts/retrieval_terms) — the
    default (False) keeps every v1 call byte-identical."""
    fmt: type[TriageTurn] = TriageTurnV2 if schema_v2 else TriageTurn
    if schema_v2 and max_tokens < 1600:
        # The v2 schema (case_facts + retrieval_terms + register + a clinical-register refined
        # question) does not fit v1's 700-token budget — truncation silently degraded every v2
        # turn to the fail-safe shape (batch-1 eval: terms collapsed to 0.11). v1 calls keep 700.
        max_tokens = 1600
    msgs: list[dict] = []
    for t in (transcript or []):
        role = "assistant" if (t.get("role") == "assistant") else "user"
        text = (t.get("text") or "").strip()
        if text:
            msgs.append({"role": role, "content": text})
    if not msgs:
        return fmt(status="ask", message="What clinical question can I help you narrow down?")
    if msgs[-1]["role"] != "user":
        # the conversation must end on the user's turn for the model to respond to
        msgs.append({"role": "user", "content": "(continue)"})
    if roster_summary:
        msgs.insert(0, {"role": "user", "content":
                        f"[Available specialist lenses for a panel: {roster_summary}]"})
    if force_ready:
        msgs.append({"role": "user", "content":
                     "[You have enough context. Return status=\"ready\" now with your best "
                     "refined_question and recommended_mode — do NOT ask another question.]"})
    try:
        comp = await llm.complete(
            system=triage_prompt, messages=msgs, response_format=fmt, max_tokens=max_tokens)
        turn = comp.parsed
    except Exception as e:   # noqa: BLE001 — triage must never block the user
        _log.warning("triage turn failed: %r", e)
        return fmt(status="ready", recommended_mode="qa", refined_question=_last_user(transcript),
                   understood_problem=_last_user(transcript),
                   message="Let me search the evidence for that.", rationale="fallback")
    if force_ready:
        turn.status = "ready"
    if turn.status == "ready" and not (turn.refined_question or "").strip():
        turn.refined_question = turn.understood_problem.strip() or _last_user(transcript)
    return turn
