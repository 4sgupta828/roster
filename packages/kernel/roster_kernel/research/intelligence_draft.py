"""IntelligenceDraft — the adversarial hypothesis-driven DRAFT stage (ROSTER_INTELLIGENCE_CORE, T1).

ONE strong-model call produces a FLAT `IntelligenceDraft`: a short prose analytical `frame`
(the world-model / key components the question turns on) plus a single `hypotheses_text` string
carrying 2-3 genuinely COMPETING hypotheses in a LINE PROTOCOL — one hypothesis per line:

    Hn | <claim> | <for-search-query> | <against-search-query> | <falsifier>

The model owns the hypotheses (Rule 18 — meaning); CODE owns parsing them into `Hypothesis`
records (`parse_hypotheses`). This is the RELIABILITY shape from the parametric post-mortem: the
parametric draft flaked because a single call had to reason (free text) AND enumerate a NESTED
object-list → the model filled the prose and returned an empty list. A FLAT prose field + a flat
line-delimited string never under-populates the way a nested `list[Hypothesis]` schema does.

T1 only produces the draft + threads it inertly (unused until T2 retrieval / T3 compose consume it).

Domain-free (Rule 18): the kernel owns the MECHANICS (one structured call, budget charge, fail-safe,
line parsing); the JUDGMENT (what the competing hypotheses are, the for/against queries, the
falsifier) is the LLM's, steered entirely by the VERTICAL-supplied prompt injected as `prompt`.

Fail-safe (Rule 20): no llm / blank prompt / blank question / ANY exception → None → the caller
falls back to today's retrieval-led path byte-identical. The caller ALSO guards on >=2 well-formed
hypotheses (a degenerate draft → fall back).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import BaseModel

logger = logging.getLogger(__name__)


@dataclass
class Hypothesis:
    """One competing hypothesis parsed from a protocol line. `claim` is the hypothesis stated as a
    checkable answer; `for_query`/`against_query` are the retrieval searches that seek CONFIRMING and
    DISCONFIRMING evidence (the disconfirmation leg is the one thing missing today); `falsifier` is
    the concrete observation that would prove this hypothesis wrong. Missing trailing fields default
    to "" so a claim-only line still yields a usable (query-less) Hypothesis."""
    claim: str
    for_query: str = ""
    against_query: str = ""
    falsifier: str = ""


class IntelligenceDraft(BaseModel):
    """The pre-retrieval intelligence draft: a prose analytical `frame` + a flat `hypotheses_text`
    LINE PROTOCOL (one `Hn | claim | for | against | falsifier` per line). FLAT by design — NO nested
    object-list (the parametric-draft reliability lesson). Defaults make an empty/partial emission an
    inert no-op draft (the caller's >=2-hypotheses guard then falls back byte-identical)."""
    frame: str = ""
    hypotheses_text: str = ""


def parse_hypotheses(hypotheses_text: str) -> list[Hypothesis]:
    """Parse the line protocol into `Hypothesis` records — PURE structural parsing (Rule 18: code owns
    parsing, the model owns meaning). Split on lines; for each non-blank line split on `|`; strip a
    leading `Hn` label token (e.g. `H1`, `H2` — a bare `H` + digits) if present; map the remaining
    fields → claim / for_query / against_query / falsifier. Tolerant of missing trailing fields (a line
    with just a claim yields a Hypothesis with empty queries). Blank/garbage lines (no non-empty claim
    after the label) are skipped. A well-formed hypothesis needs at least a non-empty `claim`."""
    out: list[Hypothesis] = []
    for raw in (hypotheses_text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split("|")]
        # Strip a leading `Hn` label token (H + digits, e.g. "H1") when present — it's a label, not a field.
        if fields and _is_label(fields[0]):
            fields = fields[1:]
        # Pad to 4 fields so trailing-field omissions default to "" (tolerant parsing).
        fields = (fields + ["", "", "", ""])[:4]
        claim, for_q, against_q, falsifier = fields
        if not claim:                     # a line with no claim (blank/garbage/label-only) is skipped
            continue
        out.append(Hypothesis(claim=claim, for_query=for_q,
                              against_query=against_q, falsifier=falsifier))
    return out


def _is_label(token: str) -> bool:
    """True for a bare `Hn` enumeration label (case-insensitive H followed by digits) — a marker to
    strip, not a claim. Purely structural (Rule 18)."""
    t = token.strip()
    return len(t) >= 2 and t[0] in ("H", "h") and t[1:].isdigit()


async def draft_intelligence(question: str, llm, prompt: str | None, *,
                             budget, max_tokens: int = 1200) -> IntelligenceDraft | None:
    """ONE structured LLM call → IntelligenceDraft, or None on ANY failure. Fail-safe is today's
    retrieval-led path — never a heuristic guess (Rule 18). Charges the budget on success. The caller
    parses `hypotheses_text` + guards on >=2 well-formed hypotheses."""
    if llm is None or not (prompt or "").strip() or not (question or "").strip():
        return None
    try:
        res = await llm.complete(system=prompt,
                                 messages=[{"role": "user", "content": question}],
                                 response_format=IntelligenceDraft, max_tokens=max_tokens)
        budget.charge(calls=1, tokens=getattr(res, "output_tokens", 0))
        return res.parsed
    except Exception:   # noqa: BLE001 — fail-safe: the draft must never break the answer path
        logger.debug("draft_intelligence failed; falling back to retrieval-led path", exc_info=True)
        return None
