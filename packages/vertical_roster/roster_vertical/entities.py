"""Tech entity taxonomy (declared data). Vocabulary lives here, not the kernel."""
from __future__ import annotations

import re

ENTITY_TYPES: tuple[str, ...] = (
    "company", "technology", "patent", "paper", "repository",
    "funding_round", "product", "person",
)

# What a subject entity can relate to (declared; used by UI/graph later).
RELATIONSHIPS: dict[str, tuple[str, ...]] = {
    "company": ("technology", "product", "person", "funding_round", "patent"),
    "technology": ("paper", "patent", "repository"),
    "paper": (),
    "patent": (),
    "repository": (),
    "funding_round": ("company",),
    "product": ("company",),
    "person": ("company",),
}

# STRUCTURAL id formats (vertical-local, allowed — Rule 18: computable, not semantic).
CIK_RE = re.compile(r"\bCIK[-\s]?(\d{1,10})\b", re.IGNORECASE)   # SEC central index key
ARXIV_RE = re.compile(r"\b\d{4}\.\d{4,5}(v\d+)?\b")              # arXiv id e.g. 2401.00001
TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")                       # $NVDA style cashtag


def looks_like_arxiv(text: str) -> bool:
    return ARXIV_RE.search(text) is not None


def looks_like_cik(text: str) -> bool:
    return CIK_RE.search(text) is not None
