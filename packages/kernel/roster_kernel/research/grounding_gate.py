"""Cross-family SEMANTIC grounding gate for the composed answer (ROSTER_INTELLIGENCE_CORE, T4).

The hard-token prose-audit (`react.py::_unsupported_prose_tokens`) only catches
unsupported FIGURES — a number/date/dose/% in the prose that appears in no
verified finding. It is blind to a laundered QUALITATIVE claim: a mechanism, an
entity, a relationship, or a causal link asserted as fact but present in no cited
finding. That is exactly the failure mode "ungrounded model reasoning laundered
as intelligence" the intelligence-core spec names as its biggest risk.

This gate is the semantic backstop, ported from factra's
`finding_grounding_gate.py`: a DIFFERENT-model-family judge (temp 0, closed JSON
shape) re-reads ONLY the composed answer + ONLY the verified claims and answers
one adversarial question:

    Which sentences/spans of the answer assert a MECHANISM, ENTITY, DATE,
    OUTCOME, or CAUSAL claim that NO verified claim supports?

Different family than the composer → uncorrelated failure modes: a hallucination
the composer emits is unlikely to be rationalised by a different-family judge
reading the same claims. The caller (react.py) then recomposes to remove/relabel
those spans as labeled [[R]] inference (never fact).

Design mirrors the factra pattern: closed pydantic shape, temperature 0, an
`LLMClient`-shaped `judge_llm` for stubbing, and — critically — FAIL-CLOSED:

  * judge_llm is None (no cross-family judge available)   → return []
  * no verified_claims                                    → return []
  * any judge error / malformed output                   → return []

`[]` means "no flagged spans" → the caller takes NO action → today's behavior
(the hard-token audit result) stands unchanged. A judge failure NEVER weakens
grounding and NEVER blocks the answer. Rule 18: the judge owns the semantic call;
this code owns only the gate mechanics + the fail-closed contract.
"""
from __future__ import annotations

import logging
import re

from pydantic import BaseModel

_log = logging.getLogger("roster.grounding_gate")

# Give the judge room to echo every flagged span verbatim without truncating a
# long enumeration into a non-parseable response (the factra 800→3000 lesson).
_MAX_OUTPUT_TOKENS = 3000
_MAX_SPANS = 50

# Strip citation markers ([n]) and reasoning labels ([[R]] / [[/R]]) before the
# check — they are provenance/label scaffolding, not asserted prose, and a stray
# marker should not confuse the judge (the caller passes Layer-1 prose).
_REF_MARK_RE = re.compile(r"\[\d+\]")
_R_MARK_RE = re.compile(r"\[\[/?\s*R\s*\]\]", re.IGNORECASE)


class GroundingProbe(BaseModel):
    """Closed shape the cross-family judge returns. `unsupported` is the list of
    verbatim answer sentences/spans that assert something no verified claim
    supports. Empty list ⇒ fully grounded."""
    unsupported: list[str] = []


_SYSTEM_PROMPT = """\
You are an adversarial grounding auditor. You receive ONE composed answer and
ONLY the verified claims it is allowed to rest on. You see NOTHING else — not the
original question, not any other evidence, and NOT your own background knowledge.

Your ONE job: find every sentence or span in the answer that ASSERTS, IN ITS OWN
VOICE AND AS FACT, something the verified claims do NOT state. That includes:
  • a MECHANISM or "how/why it works" claim no verified claim describes;
  • an ENTITY (company, product, person, place, standard) the claims do not name;
  • a RELATIONSHIP between entities the claims do not establish;
  • a DATE or time period the claims do not give;
  • an OUTCOME or disposition (launched, approved, acquired, failed, adopted,
    grew, declined) no claim records;
  • a CAUSAL or motivational claim (because / led to / drove / in order to /
    therefore / as a result) that no claim states.

Be strict. If the answer says MORE than the verified claims literally state —
even if it "sounds right" or matches what is generally true — it is NOT grounded.
Do NOT assume "a source must say X" if the claims you were given do not show X.

Two things are ALWAYS fine and must NOT be flagged:
  • text the answer explicitly marks as its own inference or reasoning (hedged
    with "likely / probably / suggests / may / appears" or framed as an analytical
    read rather than a stated fact) — the answer is allowed to reason, only not to
    assert unstated facts;
  • a claim that any verified claim supports, even if worded differently.

Return ONE JSON object exactly matching this schema:

  {"unsupported": ["<verbatim span from the answer>", ...]}

List every unsupported span (verbatim). If every factual assertion is supported
by the verified claims, return {"unsupported": []}. Output ONLY the JSON object.
"""


def _strip_markers(text: str) -> str:
    s = _R_MARK_RE.sub(" ", text or "")
    s = _REF_MARK_RE.sub(" ", s)
    return s


def _build_user_message(*, answer: str, claim_texts: list[str]) -> str:
    claims_block = "\n\n".join(
        f"### Verified claim {i + 1}\n{t}" for i, t in enumerate(claim_texts)
    ) or "<NO VERIFIED CLAIMS — any specific factual assertion is unsupported>"
    return (
        "# Composed answer (judge every sentence)\n\n"
        f"{answer}\n\n"
        "# Verified claims (the ONLY facts the answer may assert)\n\n"
        + claims_block
        + "\n\nProduce the JSON grounding verdict now."
    )


async def cross_family_ground_check(
    answer: str,
    verified_claims: list,
    judge_llm,
    *,
    budget,
) -> list[str]:
    """One adversarial cross-family call: return the answer spans that assert a
    mechanism/entity/date/outcome/causal claim NO verified claim supports.

    FAIL-CLOSED (never weakens grounding, never blocks the answer):
      judge_llm is None / no verified_claims / empty answer / judge error /
      malformed output  → return [] (no flagged spans → caller takes no action).

    The judge MUST be a different model family than the composer — that is the
    whole point (uncorrelated failure modes). The caller is responsible for only
    passing a cross-family client here (and passing None otherwise, so this gate
    fails closed rather than running same-family).
    """
    # Fail-closed guards ------------------------------------------------------
    if judge_llm is None:
        return []
    if not verified_claims:
        return []
    clean = _strip_markers(answer or "").strip()
    if not clean:
        return []

    claim_texts = [
        (
            (getattr(vc, "text", "") or "")
            + ((" — quote: " + q) if (q := (getattr(vc, "quote", "") or "").strip()) else "")
        ).strip()
        for vc in verified_claims
    ]

    try:
        res = await judge_llm.complete(
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_message(
                answer=clean, claim_texts=claim_texts)}],
            response_format=GroundingProbe,
            max_tokens=_MAX_OUTPUT_TOKENS,
            temperature=0,
        )
    except Exception as e:   # noqa: BLE001 — a judge failure fails CLOSED (no action), never weakens
        _log.warning("cross-family grounding gate call failed: %r", e)
        return []

    # Charge the request budget for the judge call (charge-after, like compose /
    # derive-weave). A metering failure must not sink the result.
    try:
        if budget is not None and not getattr(budget, "exhausted", False):
            budget.charge(calls=1, tokens=int(getattr(res, "output_tokens", 0) or 0))
    except Exception:   # noqa: BLE001
        pass

    parsed = getattr(res, "parsed", None)
    unsupported = getattr(parsed, "unsupported", None)
    if not isinstance(unsupported, list):
        return []
    out: list[str] = []
    for s in unsupported:
        s = str(s).strip()
        if s:
            out.append(s)
        if len(out) >= _MAX_SPANS:
            break
    return out


__all__ = ["cross_family_ground_check", "GroundingProbe"]
