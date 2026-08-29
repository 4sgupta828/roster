"""Generic, domain-neutral core DTOs — the anti-anchor.

These are the shapes that flow through the kernel pipeline. They carry a generic
`facets` bag (dimension→value) instead of named domain columns, so porting an
algorithm from a domain-specific system can never smuggle domain-specific column
names into the kernel. A vertical decides what facet keys mean; the kernel only
ever does `facets.get(k)` / `facets ⊇ filter`.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field

# A facet map on a stored item: caller/vertical-defined dimension → single value
# (a document is in exactly one region, of one kind, etc). Opaque to the kernel.
Facets = dict[str, str]

# A facet FILTER on a query: each dimension maps to one required value OR a set of
# acceptable values (IN semantics), so `region in {north, south}` is expressible.
# A hit matches iff, for every filter key, hit.facets[key] is in the allowed set.
FacetFilter = dict[str, "str | tuple[str, ...]"]


@dataclass(frozen=True)
class EntityRef:
    """A thing a source is *about* (a case, a bill, an issuer — vertical decides)."""
    source_key: str
    native_id: str
    title: str = ""
    facets: Facets = field(default_factory=dict)
    dates: dict[str, str] = field(default_factory=dict)  # role → ISO date
    extra: dict = field(default_factory=dict)            # vertical passthrough


@dataclass(frozen=True)
class DocumentRef:
    """A fetchable artifact belonging to zero or more entities (n:m)."""
    source_key: str
    native_id: str
    title: str = ""
    content_type: str = "application/pdf"
    entity_ids: tuple[str, ...] = ()
    facets: Facets = field(default_factory=dict)
    dates: dict[str, str] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Locator:
    """Where a quote/fact lives, so a claim can be provenance-verified.

    `kind` selects the verification path a vertical's CitationVerifier supports:
    "block_span" (PDF/HTML text), "fact_coordinate" (e.g. XBRL cell), "row_cell"
    (table), "registry_row" (structured store), "url" (web). `ref` is an opaque
    dict the matching verifier understands.
    """
    kind: str
    document_id: str
    ref: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BlockHit:
    """A retrieved chunk of evidence — domain-neutral.

    Carries generic document provenance (title/type/source, for display + scoping)
    and the `facets` bag (the vertical's narrowing dimensions) that the block was
    filtered on.
    """
    document_id: str
    block_id: str
    text: str
    score: float = 0.0
    facets: Facets = field(default_factory=dict)
    locator: Locator | None = None
    document_title: str = ""
    content_type: str = ""
    source_key: str = ""
    legs: tuple[str, ...] = ()          # which retrieval legs matched (lexical/dense/…)
    extra: dict = field(default_factory=dict)


@dataclass
class RetrievalRequest:
    """A retrieval query. Tenant/workspace isolation is a FIRST-CLASS, mandatory
    boundary (security — never a soft facet). query_embedding is supplied by the
    kernel's Embedder so a source doesn't re-embed and fusion stays app-level.
    """
    query: str
    tenant_id: str
    workspace_id: str | None = None
    query_embedding: list[float] | None = None
    facets: FacetFilter = field(default_factory=dict)
    # EXCLUSION facets: a block is dropped if it HAS the key AND its value is in the set (untagged
    # blocks pass — no null-exclusion trap). Used e.g. to keep modality=alternative out of the
    # default Allopathic view without re-tagging the whole corpus. Empty → no exclusion.
    exclude_facets: FacetFilter = field(default_factory=dict)
    k: int = 20
    fetch_pool: int = 60                 # per-leg candidate pool before fusion
    # Per-request WEB controls (answer-contract): a source that queries the live web MAY honor these.
    # `web_open` True → ignore the vertical's trusted-domain whitelist for THIS query (open-web
    # discovery for a currency question); `web_recency_days` → per-request recency floor override.
    # Corpus sources ignore them. Both default to the source's own configuration (byte-identical).
    web_open: bool = False
    web_recency_days: int | None = None
    # flag ROSTER_WEB_OPEN_DENOISE: apply the open-web denoising gates (structural + cosine floor +
    # authority boost) in web retrieval; corpus ignores it.
    web_denoise: bool = False
    # Optional web-only overrides for additive high-recall legs. Defaults are inert and preserve the
    # source/provider defaults exactly.
    web_max_results: int | None = None
    web_max_chars: int | None = None
    web_max_chunks_per_page: int | None = None
    web_extra_facets: dict | None = None


class Capability(str, enum.Enum):
    RETRIEVAL = "retrieval"
    PRECISION = "precision"
    STRUCTURED = "structured"
    MONITOR = "monitor"
