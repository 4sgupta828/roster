"""Provenance hard gate — the domain-free span-check (the grounding crown jewel).

A claim is admissible only if its cited quote actually exists at its locator.
This is PROVENANCE, not correctness (it proves the system didn't fabricate the
span; it does NOT prove the right span was chosen — that's the eval's job).

Kernel owns the `block_span` verifier (verbatim-in-spirit port of the old
locator + normalized-substring check). Other locator kinds (fact_coordinate for
XBRL, row_cell, registry_row, url) are verticals' CitationVerifiers, dispatched
by locator.kind. Tenant isolation is enforced by the loader: it can only load
blocks for the tenant/workspace it was built for — so a quote can never be
"verified" against another tenant's document (the cross-tenant FALSE-PASS guard).
"""
from __future__ import annotations

import re
from typing import Callable, Protocol, runtime_checkable

from roster_kernel.contract.dto import Locator

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Whitespace-collapsed, lowercased — tolerant of reflow, not of content."""
    return _WS.sub(" ", text.strip().lower())


@runtime_checkable
class BlockLoader(Protocol):
    """Loads a block's text by locator. MUST be tenant/workspace-scoped by
    construction — returns None for anything outside its scope (fail-closed)."""
    def load(self, document_id: str, block_id: str) -> str | None: ...


class BlockSpanVerifier:
    """CitationVerifier for locator.kind == 'block_span'."""

    supported_kinds = ("block_span",)

    def __init__(self, loader: BlockLoader | Callable[[str, str], "str | None"]):
        self._load = loader.load if isinstance(loader, BlockLoader) else loader

    def verify(self, quote: str, locator: Locator) -> bool:
        if locator.kind != "block_span":
            return False
        block_id = locator.ref.get("block_id")
        if not block_id:
            return False
        block_text = self._load(locator.document_id, block_id)
        if block_text is None:            # out of scope / missing → fail closed
            return False
        q = normalize(quote)
        return bool(q) and q in normalize(block_text)
