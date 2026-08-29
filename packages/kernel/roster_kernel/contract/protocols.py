"""Typed Protocols a vertical implements — the real plugin contract.

These replace the opaque `Any` manifest slots so a new domain (financial,
legislative) is added purely by shipping a package that satisfies these
Protocols — zero kernel edits. All are `runtime_checkable` so conformance can
assert structural conformance of a manifest's declared components.

Nothing here names a domain concept; the vocabulary is always the vertical's
`facets`/`scope`/typed `extra`.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .dto import BlockHit, Capability, DocumentRef, EntityRef, FacetFilter, Locator, RetrievalRequest


# ---- acquisition ---------------------------------------------------------

@runtime_checkable
class FetchStrategy(Protocol):
    """How a connector's requests reach the source. Encodes egress placement +
    anti-bot choreography as declared properties, not connector-buried logic."""
    egress_class: str           # "datacenter" | "residential"
    engine: str                 # "http" | "chromium" | "firefox"
    proxy_enabled: bool         # residential connectors force this off
    async def fetch(self, url: str, **opts: Any) -> bytes: ...


@runtime_checkable
class Connector(Protocol):
    """Source-specific selectors/API mapping + normalization ONLY.

    Scheduling/storage/retries/breaker belong to the kernel. Domain nouns live
    in the returned refs' `facets`/`extra`, never in the method surface.
    """
    key: str
    fetch_strategy: FetchStrategy
    async def discover_entities(self, window: dict) -> list[EntityRef]: ...
    async def list_documents(self, entity: EntityRef) -> list[DocumentRef]: ...
    async def fetch_artifact(self, doc: DocumentRef) -> bytes: ...
    # Optional: caption/collections enrichment + n:m membership (duck-typed).
    # async def fetch_entity_detail(self, entity: EntityRef) -> dict: ...


@runtime_checkable
class Parser(Protocol):
    """content_type → markdown/text. Kernel ships pdf/html; verticals add xbrl."""
    content_types: tuple[str, ...]
    def parse(self, raw: bytes, *, content_type: str) -> str: ...


# ---- retrieval + policy --------------------------------------------------

@runtime_checkable
class RetrievalSource(Protocol):
    key: str
    def capabilities(self) -> frozenset[Capability]: ...
    async def search(self, req: RetrievalRequest) -> list[BlockHit]: ...
    # The source owns the corpus, so it manufactures the tenant/workspace-scoped
    # block loader the provenance gate verifies against. Declared here (panel-
    # caught: run_react/precision require it) so conformance can't pass a source
    # that would crash the loop. Returns a BlockLoader (load(document_id, block_id)).
    def make_block_loader(self, tenant_id: str, workspace_id: str | None = None) -> object: ...
    # Source-declared scope coverage: the kernel routes by matching a request's
    # facets against this, so simple verticals need no ScopeRouter and no domain
    # noun enters the kernel. Empty = covers everything.
    def covers(self) -> "FacetFilter": ...


@runtime_checkable
class GatingPolicy(Protocol):
    """The domain-neutral seam for gating/coverage — so no domain-format regex or
    single-corpus geographic constant is ever inlined into the kernel loop. The
    semantic VERDICT (is this claim sufficient) stays LLM-run in the kernel,
    parameterized by the vertical's Persona/criteria — never a pure function here.
    """
    def gate_applies(self, question: str, plan: dict) -> bool: ...
    # Per-claim seam (panel): which claims face the hard gate — replaces the old
    # 'exclude web-only claims' rule so the kernel needn't inline a source policy.
    def claim_in_scope(self, claim: object, cited_hits: list[BlockHit]) -> bool: ...
    def coverage_gap(self, question: str, hits: list[BlockHit]) -> str | None: ...


@runtime_checkable
class ScopeRouter(Protocol):
    """OPTIONAL cross-source arbitration. The kernel default routes by source-
    declared `covers()`; a vertical supplies a ScopeRouter only when multiple
    sources need arbitration (the seam that keeps any 'default-OH' tree out of
    the kernel). Returns the source keys to query, best-first."""
    def select(self, *, scope: "FacetFilter", needed: frozenset[Capability],
               available: dict[str, RetrievalSource]) -> tuple[str, ...]: ...


@runtime_checkable
class CitationVerifier(Protocol):
    """Provenance check per locator kind (block_span / fact_coordinate / row_cell
    / registry_row / url). Kernel owns span_check; verticals add fact-coordinate."""
    supported_kinds: tuple[str, ...]
    def verify(self, quote: str, locator: Locator) -> bool: ...


# ---- language + presentation --------------------------------------------

@runtime_checkable
class Persona(Protocol):
    """System prompt + tool descriptions (kept consistent with tool schemas)."""
    def system_prompt(self) -> str: ...
    def tool_descriptions(self) -> dict[str, str]: ...


@runtime_checkable
class EntityView(Protocol):
    """UI schema for one entity type: how to list it and show its detail.
    Presentation is DECLARED data (field specs), not code, so the app shell
    renders any vertical's entities without kernel/app edits."""
    entity_type: str
    def list_columns(self) -> list[dict]: ...     # [{key,label,kind}]
    def detail_sections(self) -> list[dict]: ...   # [{title, fields:[...]}]


@runtime_checkable
class UIContract(Protocol):
    """What a vertical declares so the responsive, minimal app shell renders a
    coherent, domain-appropriate UI with zero app edits."""
    def navigation(self) -> list[dict]: ...                 # nav entries
    def search_facets(self) -> list[dict]: ...              # filter controls
    def entity_views(self) -> list[EntityView]: ...         # list/detail schemas
    def citation_renderers(self) -> dict[str, str]: ...     # locator.kind → renderer id
    def deliverable_renderers(self) -> dict[str, str]: ...  # kind → renderer id
