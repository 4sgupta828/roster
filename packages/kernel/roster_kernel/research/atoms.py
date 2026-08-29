"""Evidence atoms — the ReAct loop's working memory.

Each retrieved BlockHit becomes an Atom with a stable id the LLM cites. An atom
carries its locator so a claim citing it can be provenance-verified. Domain-free.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from roster_kernel.contract.dto import BlockHit, Facets, Locator

# Evidence Contract stage 1 (evidence-identity flag): the tag's title window. Long enough to keep a
# document's identity readable, short enough to stay ~10 tokens per atom on every surface.
_IDENTITY_TITLE_CAP = 80

# The ONE flag-gated claim-writing instruction shared by every surface that asks an LLM to write
# claims (planner answering discipline, claims-first extraction). Kernel-generic by construction:
# "document identity" / "subject" only — no domain vocabulary.
IDENTITY_INSTRUCTION = (
    "Each source is labeled with its document identity in ⟨⟩; when writing a claim, attribute it "
    "to that source's actual subject — never generalize a source to a different subject or class.")


def identity_tag(evidence) -> str:
    """Render a document-identity tag — e.g. ⟨SOME DOCUMENT TITLE — source⟩ — for any evidence
    carrier exposing `document_title` / `source_key` (Atom, VerifiedClaim, BlockHit).

    The title is VERBATIM (never parsed/regexed — Rule 18): only whitespace runs/newlines are
    collapsed and the length is capped. An empty title returns "" so an identity-less atom renders
    exactly as it does today (the caller must treat "" as "no tag")."""
    title = " ".join((getattr(evidence, "document_title", "") or "").split())
    if not title:
        return ""
    if len(title) > _IDENTITY_TITLE_CAP:
        title = title[:_IDENTITY_TITLE_CAP].rstrip() + "…"
    source = (getattr(evidence, "source_key", "") or "").strip()
    return f"⟨{title} — {source}⟩" if source else f"⟨{title}⟩"


@dataclass(frozen=True)
class Atom:
    atom_id: str
    document_id: str
    block_id: str
    text: str
    locator: Locator | None = None
    facets: Facets = field(default_factory=dict)
    source_key: str = ""            # which source produced it (corpus/web/workspace/…)
    document_title: str = ""


class AtomStore:
    def __init__(self) -> None:
        self._atoms: dict[str, Atom] = {}
        self._n = 0

    def add_hits(self, hits: list[BlockHit]) -> list[Atom]:
        added: list[Atom] = []
        for h in hits:
            # dedupe by (document_id, block_id) — same block cited once
            existing = next(
                (a for a in self._atoms.values()
                 if a.document_id == h.document_id and a.block_id == h.block_id),
                None,
            )
            if existing is not None:
                continue
            self._n += 1
            atom = Atom(
                atom_id=f"a{self._n}",
                document_id=h.document_id,
                block_id=h.block_id,
                text=h.text,
                locator=h.locator,
                facets=dict(h.facets),
                source_key=h.source_key,
                document_title=h.document_title,
            )
            self._atoms[atom.atom_id] = atom
            added.append(atom)
        return added

    def get(self, atom_id: str) -> Atom | None:
        return self._atoms.get(atom_id)

    def all(self) -> list[Atom]:
        return list(self._atoms.values())
