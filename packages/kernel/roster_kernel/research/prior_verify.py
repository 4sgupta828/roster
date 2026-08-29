"""Directed single-claim grounder — the parametric-led VERIFY stage (ROSTER_PARAMETRIC_LED, T2).

In parametric mode the model has ALREADY drafted its facts (`PriorDraft`); retrieval no longer
AUTHORS the answer, it VALIDATES each asserted fact. `ground_asserted_claim` is the claim-DIRECTED
sibling of `fallback_grounder.ground_claimless`: given ONE `AssertedClaim` and the atoms retrieved
for it, it asks the LLM to find a SINGLE atom carrying a VERBATIM, contiguous quote that EXPLICITLY
and UNEQUIVOCALLY proves that specific claim — or NONE. It is deliberately ADVERSARIAL in the same
spirit as the stage-2 binding judge (`claims_first.entail_claims`): shared vocabulary / keyword
overlap is not proof; a tangential or similar-but-different span must be rejected.

PROVENANCE IS UNCHANGED: the caller runs the returned quote through the kernel's UNTOUCHED verbatim
`BlockSpanVerifier` — a model fact reaches `verified_claims` ONLY when a real retrieved block
entails it. This module can only PROPOSE a candidate; the span-gate is the fact wall (Rule 20).

Fail-CLOSED everywhere (Rule 18 — grounding judgment is the LLM's, never a keyword fallback): no llm /
no atoms / LLM error / unknown atom id / blank quote / span-gate rejection / budget exhausted → None →
the claim stays UNVERIFIED (labeled by the caller), never laundered into grounded prose.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from roster_kernel.research.budget import BudgetExceeded

logger = logging.getLogger(__name__)

# Per-atom char window shown to the grounder (mirrors the fallback grounder's atom_cap).
_ATOM_CAP = 1600


class _GroundVerdict(BaseModel):
    """The directed grounder's single verdict: the atom whose verbatim quote UNEQUIVOCALLY proves
    the claim, or empty fields for 'no atom proves it'. Defaults make a blank/partial/abstained
    emission parse safely as 'not grounded' (fail-closed)."""
    atom_id: str = ""
    quote: str = ""


_SYSTEM = (
    "You are a RUTHLESS, ADVERSARIAL fact-verifier. You are given ONE CLAIM and a numbered list of "
    "evidence atoms, each prefixed with its id like [a5]. Find a SINGLE atom that contains a "
    "VERBATIM, contiguous quote which EXPLICITLY and UNEQUIVOCALLY PROVES the claim.\n"
    'Output STRICT JSON: {"atom_id":"a5","quote":"<verbatim contiguous span>"} for that atom, or '
    '{} (leave atom_id empty) if NO atom proves the claim.\n'
    "RULES — err toward {} :\n"
    "- The 'quote' MUST be copied EXACTLY, character-for-character, as a contiguous substring of "
    "that atom's text. NEVER paraphrase, summarize, translate, or reformat numbers/units/dates.\n"
    "- REJECT tangential, merely topical, or keyword-only matches. Shared vocabulary is NOT proof. "
    "The quote must state the SPECIFIC fact the claim asserts — its exact subject/entity, value, "
    "and time period — not a similar or adjacent fact.\n"
    "- If the evidence only PARTIALLY supports the claim, hedges it, or supports a "
    "similar-but-different fact, return {}. Do NOT stretch. When in any doubt, return {}.\n"
    "- Cite ONLY an atom id that appears in the list; never invent one."
)


async def ground_asserted_claim(claim_text, atoms_for_claim, llm, verifier, *, budget):
    """Directed grounder for ONE asserted fact.

    Returns `(atom_id, quote)` iff some provided atom carries a verbatim quote that the LLM judges
    to UNEQUIVOCALLY prove `claim_text` AND that quote passes the untouched span-gate
    (`verifier.verify(quote, atom.locator)`); otherwise `None`.

    Fail-CLOSED: no llm / no eligible atoms / budget exhausted / LLM error / unknown-or-locatorless
    atom / blank quote / span-gate rejection / verifier error → `None` (the claim is not grounded).
    Charges one LLM call on a successful invocation.
    """
    text = (claim_text or "").strip()
    eligible = [a for a in (atoms_for_claim or []) if (getattr(a, "text", "") or "").strip()]
    if llm is None or not text or not eligible:
        return None
    by_id = {a.atom_id: a for a in eligible}
    block = "\n".join(f"[{a.atom_id}] {a.text[:_ATOM_CAP]}" for a in eligible)
    user = f"CLAIM:\n{text}\n\nEVIDENCE ATOMS:\n{block}\n\nReturn ONLY the JSON."
    try:
        budget.reserve()
    except BudgetExceeded:
        return None
    try:
        res = await llm.complete(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            response_format=_GroundVerdict, max_tokens=400)
        budget.charge(calls=1, tokens=getattr(res, "output_tokens", 0))
    except Exception:   # noqa: BLE001 — fail-closed: a broken/timed-out judge grounds nothing
        logger.debug("ground_asserted_claim LLM call failed; claim stays unverified", exc_info=True)
        return None
    verdict = res.parsed
    atom = by_id.get((getattr(verdict, "atom_id", "") or "").strip())
    quote = (getattr(verdict, "quote", "") or "").strip()
    if atom is None or atom.locator is None or not quote:
        return None
    # The UNTOUCHED span-gate is the ONLY path to a verified claim (invariant): the quote must exist
    # verbatim in the cited atom's block, or the claim is not grounded. A confident-but-wrong model
    # fact mapped to a tangential quote dies here even if the adversarial judge above let it slip.
    try:
        ok = verifier.verify(quote, atom.locator)
    except Exception:   # noqa: BLE001 — a verifier error fails closed, never grounds
        return None
    return (atom.atom_id, quote) if ok else None
