"""Tech authority policy — the evidence pyramid for deep-tech research.

The analogue of the medical evidence hierarchy. `rank()` is a bounded, boost-only tier
signal consumed by the research loop's relevance-selection step — a stronger-tier finding
surfaces into the compose cap ahead of a similarly-relevant weaker one. It NEVER gates the
span/entailment provenance checks (those are tier-agnostic).

The load-bearing decision: MARKET SENTIMENT is the LOWEST tier and can never be
`is_controlling` — the code-level guarantee that sentiment is labeled signal, not fact.
"""
from __future__ import annotations

# higher = stronger evidence
_RANK: dict[str, int] = {
    "sentiment_signal": 1,     # HN / social / forum tone — perception, NOT fact
    "technical_signal": 2,     # arXiv preprints, GitHub activity (unreviewed / self-reported)
    "expert_analysis": 3,      # a NAMED expert's interpretation/foresight — essays, newsletters,
    #                            recorded expert discussion. Above an unreviewed preprint's self-claim,
    #                            below fact-checked press and verified/primary evidence. Opinion, not
    #                            fact (never controlling); the "wisdom/foresight" retrieval lens
    #                            promotes it for the queries that ask for it (see answer_modes).
    "analysis": 4,             # major reputable press, analyst notes
    "verified_structured": 5,  # funding-DB records, reproducible benchmarks, peer-reviewed papers
    "primary_filing": 6,       # audited SEC filings, GRANTED patents, Form D — attested/legal record
}


class TechAuthorityPolicy:
    def rank(self, evidence_kind: str) -> int:
        return _RANK.get(evidence_kind, 0)

    def outranks(self, a: str, b: str) -> bool:
        return self.rank(a) > self.rank(b)

    def is_controlling(self, evidence_kind: str) -> bool:
        # Only an audited filing / granted patent is normative for a factual claim.
        # Sentiment is deliberately excluded — it is never controlling.
        return evidence_kind == "primary_filing"
