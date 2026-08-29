"""The corpus spine — domain-neutral document model.

Document → ParsedDoc → Block → BlockContent. Content-addressed throughout, with
a version/supersedes edge so a vertical can model amended/restated documents
(legislative bill versions, financial restatements) generically.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from roster_kernel.contract.dto import Facets


@dataclass
class Document:
    id: str                      # stable id (caller/repo assigned)
    sha256: str                  # content key of the raw bytes
    content_type: str
    source_key: str
    tenant_id: str = ""          # owning tenant (isolation boundary)
    workspace_id: str | None = None  # None = tenant-wide corpus; else BYOD scope
    title: str = ""              # human document name (provenance / display)
    facets: Facets = field(default_factory=dict)  # vertical narrowing dims (state/year/…)
    dates: dict[str, str] = field(default_factory=dict)
    entity_ids: tuple[str, ...] = ()       # n:m membership
    supersedes: str | None = None          # version lineage (prior document id)


@dataclass
class ParsedDoc:
    document_id: str
    text: str                    # markdown/plain
    parser_version: str


@dataclass
class Block:
    document_id: str
    index: int
    content_key: str             # sha256 of block text → dedupe identical blocks
    text: str
    char_start: int = 0
    char_end: int = 0
    page_start: int | None = None
    page_end: int | None = None
    section_path: tuple[str, ...] = ()
    facets: Facets = field(default_factory=dict)  # per-block tags (merged OVER doc facets)


@dataclass
class BlockContent:
    content_key: str
    text: str
    embedding: tuple[float, ...] | None = None
