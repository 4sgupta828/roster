"""Edge-candidate verification — the C-6 pipeline (KG spec v3).

A drafted edge activates ONLY if the corpus itself supports it: run the edge's own
retrieval, let ONE LLM call select a supporting quote AND judge clinical notability, then
CODE re-verifies the quote verbatim against the selected block (the span gate — the LLM
cannot fabricate support). Activation label is capped at `supported` (a corpus statement is
not `established`). Fail-safe everywhere: any error/abstention → unsupported, edge stays
shadow. Kernel-generic: the caller supplies the proposition sentence and retrieved blocks.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from roster_kernel.research.provenance import normalize

# KERNEL-NEUTRAL judging directive (no domain vocabulary — the vertical supplies flavor via
# `domain_directive`, e.g. what "notable" means to its practitioners).
_SYSTEM = """You verify whether retrieved evidence supports a proposed relationship between two topics.

Judge STRICTLY:
- supported = a block EXPLICITLY states or directly demonstrates the relationship (not merely
  mentions both topics). Negated/qualified statements ("rarely", "unlike X") are NOT support
  for the plain relationship.
- notable = the relationship is actionable knowledge a domain practitioner relies on (a
  recognized decision branch), not an isolated-report curiosity.
- quote = a VERBATIM span (<= 40 words) copied EXACTLY from the supporting block. The quote
  must EXPLICITLY involve the proposed subject topic itself (by name) in relation to the
  object — a passage discussing only one side, or an unrelated report, is NOT support.
If no block supports it, verdict=unsupported and leave quote empty. Never paraphrase quotes."""


class EdgeVerdict(BaseModel):
    verdict: Literal["supported", "unsupported"] = "unsupported"
    notable: bool = False
    block_index: int = Field(default=-1, description="index of the supporting block, -1 if none")
    quote: str = ""


def subject_token_in(quote: str, subject: str) -> bool:
    """STRUCTURAL anchor check: the supporting quote must name the hidden condition — at least
    one significant token (>4 chars) of the subject label appears in the quote. Catches the
    case-report-drift failure (a quote about something else entirely passing entailment).
    Code, not semantics: it never approves — it only vetoes (Rule 18-safe)."""
    q = normalize(quote)
    toks = [t for t in normalize(subject).replace("(", " ").replace(")", " ").split()
            if len(t) > 4]
    return any(t in q for t in toks) if toks else True


async def verify_edge_candidate(*, sentence: str, blocks: list[tuple[str, str, str]],
                                llm, subject: str = "", domain_directive: str = "",
                                max_tokens: int = 300) -> dict:
    """blocks = [(document_id, block_id, text)]. Returns {supported, notable, document_id,
    block_id, quote} — supported ONLY if the LLM's quote passes the verbatim span check
    against the block it cited AND (when `subject` given) structurally names the subject."""
    if not blocks:
        return {"supported": False, "notable": False, "reason": "no_blocks"}
    listing = "\n\n".join(f"[{i}] {t[:1200]}" for i, (_d, _b, t) in enumerate(blocks[:8]))
    try:
        comp = await llm.complete(
            system=_SYSTEM + (("\n\nDOMAIN GUIDANCE:\n" + domain_directive)
                              if domain_directive else ""),
            messages=[{"role": "user", "content":
                       f"PROPOSED RELATIONSHIP: {sentence}\n\nRETRIEVED BLOCKS:\n{listing}"}],
            response_format=EdgeVerdict, max_tokens=max_tokens)
        v: EdgeVerdict = comp.parsed
    except Exception as e:   # noqa: BLE001 — fail safe: unverified stays shadow
        return {"supported": False, "notable": False, "reason": f"llm_error: {e}"[:200]}
    if v.verdict != "supported" or not v.quote or not (0 <= v.block_index < len(blocks)):
        return {"supported": False, "notable": False, "reason": "unsupported"}
    doc_id, block_id, text = blocks[v.block_index]
    if normalize(v.quote) not in normalize(text):        # the span gate — code, not trust
        return {"supported": False, "notable": False, "reason": "quote_not_verbatim"}
    if subject and not subject_token_in(v.quote, subject):
        return {"supported": False, "notable": False, "reason": "quote_lacks_subject"}
    return {"supported": True, "notable": bool(v.notable),
            "document_id": doc_id, "block_id": block_id, "quote": v.quote.strip()}
