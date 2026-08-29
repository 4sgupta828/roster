"""TechGatingPolicy — the domain-neutral coverage/gating seam for the tech vertical.

The semantic VERDICT (is a claim sufficiently grounded) stays LLM-run in the kernel; this
only declares WHEN the gate applies and WHICH claims face it. `gate_applies` fires when the
question looks like a structured id (arXiv/CIK) or the plan binds a tech subject dimension.
"""
from __future__ import annotations

from roster_kernel.contract.dto import BlockHit

from . import entities

_BINDABLE = ("company", "technology", "sector", "paper", "patent", "product")


class TechGatingPolicy:
    def gate_applies(self, question: str, plan: dict) -> bool:
        if entities.looks_like_arxiv(question) or entities.looks_like_cik(question):
            return True
        return any(plan.get(k) for k in _BINDABLE)

    def claim_in_scope(self, claim: object, cited_hits: list[BlockHit]) -> bool:
        return bool(cited_hits)

    def coverage_gap(self, question: str, hits: list[BlockHit]) -> str | None:
        # Real subject-scope detection belongs to an LLM-extracted plan (Rule 18), not
        # free-text scanning — deferred. The live gap signal today is the compose
        # `directly_addresses` honesty judgment in the kernel, not this method.
        return None
