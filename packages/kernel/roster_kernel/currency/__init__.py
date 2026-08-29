"""Corpus currency (Evidence Pulse) — domain-neutral change-detection kernel.

Mechanism only (Rule 18 / spec A7): typed lineage relations between documents
(superseded_by · retracted · amended_by · clarified_by), an auditable event
store as the source of truth, and derived block stamps the retrieval/ranking
layers consume. All JUDGMENT (what supersedes what, materiality, subjects) is
supplied by the vertical — in P0 exclusively via curator-declared lineage
(highest confidence, zero LLM).
"""
from .store import RELATIONS, CurrencyStore

__all__ = ["CurrencyStore", "RELATIONS"]
