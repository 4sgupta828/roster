"""General query expansion (ROSTER_QUERY_EXPANSION) — enrich a question's meaning + keyword
representation BEFORE retrieval, so a terse question gets the same coverage as a verbose one.

The problem: retrieval keys off the question text, so "Blazel — product, moat, founders" under-
retrieves vs the same ask spelled out fully. This is the reasoned engine's coverage-brief pattern
(augment the question with branches to investigate), generalized to EVERY question and focused on
retrieval breadth:

- ASPECTS: the key dimensions a COMPLETE, high-quality answer must cover — the "questions like this
  also need X" knowledge (the LLM supplies the collaborative-filtering-style expansion from what it
  has learned similar questions cover). E.g. a company ask implies founders/funding/traction/moat/
  competition; a technical ask implies mechanism/evidence/tradeoffs/alternatives/limits.
- KEYWORDS: specific terms, synonyms, and entity names that broaden lexical + semantic recall.

Both are appended to the question as a retrieval brief (research targets, NOT facts) — it steers the
planner + the query embedding, never adds content (the grounding gate still requires a real quote for
every claim). Best-effort: any failure → no expansion (byte-identical). The caller keeps the PRISTINE
question as graph_question so the graph expander still anchors on the asked subject.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class _Expansion(BaseModel):
    aspects: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


_EXPAND_SYSTEM = (
    "You expand a user's QUESTION into a RETRIEVAL BRIEF so the search covers everything a complete, "
    "high-quality answer needs — even when the question is terse. Think about what people asking THIS "
    "KIND of question almost always also want covered, and what a thorough answer must address.\n"
    "Return:\n"
    "- aspects: 3–6 KEY dimensions / sub-questions a thorough answer must investigate (short "
    "investigation targets, not facts). Be specific to the question's actual subject.\n"
    "- keywords: 4–10 specific search terms, synonyms, entity names, or technical terms that would "
    "surface relevant evidence (to broaden lexical + semantic recall).\n"
    "Stay FAITHFUL to the question's real subject and intent — never drift to a different topic. "
    "Concrete and specific to THIS question; no generic filler.")


async def expand_query(llm, question: str) -> dict | None:
    """One cheap LLM call → {aspects, keywords}, or None (best-effort; caller no-ops on None)."""
    q = (question or "").strip()
    if not q or llm is None:
        return None
    try:
        comp = await llm.complete(
            system=_EXPAND_SYSTEM,
            messages=[{"role": "user", "content": q[:1000]}],
            response_format=_Expansion, max_tokens=350)
    except Exception:  # noqa: BLE001 — expansion is an enhancer; its failure never blocks the answer
        return None
    aspects = [a.strip() for a in (getattr(comp.parsed, "aspects", []) or []) if (a or "").strip()][:6]
    keywords = [k.strip() for k in (getattr(comp.parsed, "keywords", []) or []) if (k or "").strip()][:10]
    if not aspects and not keywords:
        return None
    return {"aspects": aspects, "keywords": keywords}


def brief_text(expansion: dict) -> str:
    """Render the expansion as a retrieval brief appended to the question (research targets, not facts)."""
    parts = []
    if expansion.get("aspects"):
        parts.append("aspects this answer must investigate (research questions, not facts): "
                     + "; ".join(expansion["aspects"]))
    if expansion.get("keywords"):
        parts.append("relevant search terms: " + ", ".join(expansion["keywords"]))
    return "\n\n[Coverage brief — " + ".\n".join(parts) + "]"
