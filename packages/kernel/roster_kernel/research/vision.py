"""Vision pre-step — turn user-uploaded image(s) into a LABELED visual observation.

Epistemic contract (why this is safe): the observation is a DESCRIPTIVE reading of a
user's image (color, shape, borders, texture, distribution) produced by the vertical's
`vision_prompt`. It is NEVER corpus-grounded and NEVER a verified claim — it only frames
what the research agent should search for and how to interpret findings. The kernel keeps
it strictly separate from the compose step (which sees only span-verified findings), so it
can never leak into the answer as if it were evidence.

The domain wording (what to describe, and the "do not diagnose" guardrail) lives in the
vertical's `vision_prompt`; the kernel owns only the mechanics.
"""
from __future__ import annotations

from pydantic import BaseModel

from roster_kernel.providers.llm import LLMClient
from roster_kernel.research.budget import BudgetState


class VisualObservation(BaseModel):
    """A descriptive reading of the uploaded image(s). NOT a diagnosis, NOT evidence."""
    observation: str


# A kernel-side guardrail appended to every vision system prompt, independent of vertical.
_GUARD = (
    "\n\nYou are describing a user-provided image to help a downstream search over an "
    "evidence corpus. Describe ONLY what is visually present (colors, shapes, borders, "
    "texture, distribution, counts, measurable features). Do NOT name a diagnosis, disease, "
    "or condition, and do NOT recommend treatment. If the image is unclear or not "
    "interpretable, say so plainly. This description is an automated aid, not a clinical "
    "finding."
)


async def observe_images(
    *,
    llm: LLMClient,
    vision_prompt: str,
    images: list[dict],
    budget: BudgetState,
    max_images: int = 4,
) -> str:
    """Return a labeled descriptive observation of `images`, or "" if none/failed.

    `images` are dicts {media_type, data} where data is base64 (already normalized to a
    vision-capable image type by the caller). The call rides as Anthropic content blocks
    (text + image), which the LLM port passes straight through.
    """
    imgs = [im for im in (images or []) if im.get("data") and im.get("media_type")][:max_images]
    if not imgs:
        return ""
    content: list[dict] = [{
        "type": "text",
        "text": "Describe the following image(s) per your instructions.",
    }]
    for im in imgs:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": im["media_type"], "data": im["data"]},
        })
    res = await llm.complete(
        system=vision_prompt + _GUARD,
        messages=[{"role": "user", "content": content}],
        response_format=VisualObservation,
        max_tokens=1024,
    )
    budget.charge(calls=1, tokens=res.output_tokens)
    return (res.parsed.observation or "").strip()


# Guardrail for DOCUMENT reading (the image guard would misdirect: it asks for visual features,
# which makes the model DESCRIBE a report instead of TRANSCRIBING it — caught in live verification).
_DOC_GUARD = (
    "\n\nYou are transcribing a user-provided document to aid a downstream evidence search. "
    "Reproduce the document's CONTENT faithfully per the instructions above. Do NOT diagnose, "
    "interpret, or recommend treatment; do NOT transcribe patient-identifying details; if a value "
    "is unreadable, say so rather than guessing. Return the digest as the observation field."
)


async def read_documents(
    *,
    llm: LLMClient,
    report_prompt: str,
    pdfs: list[dict],
    budget: BudgetState,
    max_docs: int = 2,
) -> list[dict]:
    """NATIVE document reading — pass raw PDFs to the model as document content blocks (the model
    sees page LAYOUT, so report tables keep their row/column associations — the fix for scrambled
    text-layer extraction). Returns [{name, digest}] per successfully-read PDF; failures return no
    entry (the caller falls back to the text layer). Same epistemic contract as observe_images:
    the digest FRAMES the search / provides faithful transcription context — it is never a
    verified claim and never enters the grounded answer as evidence."""
    out: list[dict] = []
    for pdf in (pdfs or [])[:max_docs]:
        data = (pdf.get("data") or "").strip()
        if not data:
            continue
        content = [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text",
             "text": "Read this document and produce the digest per your instructions."},
        ]
        try:
            comp = await llm.complete(
                system=report_prompt + _DOC_GUARD,
                messages=[{"role": "user", "content": content}],
                response_format=VisualObservation, max_tokens=16000)
            digest = (comp.parsed.observation or "").strip()
            if digest:
                import logging
                logging.getLogger("roster.documents").info(
                    "native read: %s → %d chars", pdf.get("name"), len(digest))
                out.append({"name": pdf.get("name") or "document", "digest": digest})
        except Exception:   # noqa: BLE001 — a failed native read must not break research
            continue
    return out
