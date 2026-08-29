"""Second-model fallback grounder — rescue a claim-less terminal into cited claims.

Adapted from factra's research orchestrator (fallback_grounder.py). When the primary Anthropic
agent gathers relevant evidence but emits an EMPTY claims list (abstains), re-asking the SAME model
is unreliable on narrative/"criteria" questions (factra bake-off: Sonnet 0/8; a second model 8/8).
So a SECOND model (OpenAI) atomizes the gathered atoms into specific claims, each citing an atom and
a VERBATIM quote from it.

PROVENANCE IS UNCHANGED: every fallback claim is still run through the kernel's verbatim
BlockSpanVerifier by the caller — the quote must exist in the cited atom or the claim is rejected.
So this can only ADD claims that pass the exact same span gate; it never weakens grounding. Fully
fail-safe: no key / API error / no eligible atoms / nothing parseable → returns [] and the caller
keeps the original abstention.
"""
from __future__ import annotations

import os

DEFAULT_MODEL = "gpt-4o"
_MODEL_ENV = "ROSTER_FALLBACK_MODEL"

_SYSTEM = (
    "You extract GROUNDED CLAIMS from retrieved evidence. You are given a QUESTION and a numbered "
    "list of evidence atoms, each prefixed with its id like [a5]. Output STRICT JSON: "
    '{"claims":[{"text":"<one specific factual sentence>","atom_id":"a5","quote":"<verbatim span>"}]}.\n'
    "RULES:\n"
    "- Each claim is ONE specific factual sentence DIRECTLY supported by the single atom you cite.\n"
    "- 'quote' MUST be copied EXACTLY, character-for-character, as a contiguous substring of that "
    "atom's text. Do NOT paraphrase, summarize, translate, or reformat numbers/units.\n"
    "- Extract EVERY supported fact you can (one per relevant atom is a good default). PARTIAL "
    "coverage is expected and valuable — do NOT return an empty list when relevant evidence exists, "
    "even if the question asks for a ranking/recommendation/completeness the evidence can't fully "
    "settle. Report the supported facts.\n"
    "- Cite ONLY atom ids present in the list; never invent an id."
)


async def ground_claimless(
    *, question: str, atoms: list[tuple[str, str]], client=None,
    model: str | None = None, atom_cap: int = 900,
) -> list[dict]:
    """Atomize `atoms` (list of (atom_id, text)) into claim dicts {text, atom_id, quote}.

    Returns [] on any failure (fail-safe). The caller must still verify each returned claim through
    the span gate — this only proposes candidates.
    """
    eligible = [(aid, (txt or "").strip()) for aid, txt in atoms if (txt or "").strip()]
    if not eligible:
        return []
    if client is None:
        if not os.environ.get("OPENAI_API_KEY"):
            return []
        from roster_kernel.providers.openai_llm import OpenAIJsonLLM
        client = OpenAIJsonLLM()
    mdl = model or (os.environ.get(_MODEL_ENV) or "").strip() or DEFAULT_MODEL
    block = "\n".join(f"[{aid}] {txt[:atom_cap]}" for aid, txt in eligible)
    user = f"QUESTION:\n{question}\n\nEVIDENCE ATOMS:\n{block}\n\nReturn ONLY the JSON."
    try:
        raw = await client.complete_json(model=mdl, system=_SYSTEM, user=user)
    except Exception:          # noqa: BLE001 — fail-safe: keep the original abstention
        return []
    valid = {aid for aid, _ in eligible}
    out: list[dict] = []
    for c in (raw.get("claims") if isinstance(raw, dict) else None) or []:
        if not isinstance(c, dict):
            continue
        aid = c.get("atom_id")
        quote = (c.get("quote") or "").strip()
        if aid in valid and quote:
            out.append({"text": (c.get("text") or "").strip(), "atom_id": aid, "quote": quote})
    return out
