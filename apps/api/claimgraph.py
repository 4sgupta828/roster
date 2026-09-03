"""Claim-graph store — Layer 1 of the grounded, temporal claim graph.

The atomic unit is a GROUNDED, BITEMPORAL CLAIM
`(subject_entity, predicate, object[value|entity], valid_time, evidence{document_id,
block_id, quote, authority_tier}, confidence)`. Every product surface (startups
table, landscape map, competitor set) is a query over the accumulated graph. See
`docs/factgraph_slice1_plan.md` for the panel synthesis and the reused-vs-net-new
split.

App-level (NOT kernel) Postgres module, same DSN `ROSTER_CORPUS_DSN` and same style
as `apps/api/glossary.py` / `apps/api/gap_queue.py` (asyncpg pool, module-level
`_DDL`, lazy `_ensure()`). The store MECHANICS are domain-neutral; the vocabulary
(predicate seed list, the placement predicate that PLACES a subject, the subject
entity-kind, and the mintable object entity-kinds) arrives as DATA **injected via the
constructor** (`seed_predicates` / `placement_predicate` / `subject_kind` /
`mintable_entity_kinds`), mirroring `roster_kernel.graph.store.GraphStore(relations=…)`.
This module imports NOTHING from any vertical — a second vertical (legal/medical)
constructs the same store with its own vocabulary. The tech-configured store is built
by the wiring helper `api.claimgraph_tech.make_tech_claim_store`.

WRITE ETHOS (mirrors `packages/kernel/roster_kernel/people/store.py`):
  * append-only — a claim is written once; conflicting/newer facts are NEW claims.
  * never-resurrect — suppression/staleness is a STATUS FLIP, never a DELETE.
    Entities go active→suppressed; evidence goes active→stale/retracted; losing
    claims stay queryable (resolution names the winner, doesn't erase the losers).
  * per-claim provenance — every user-visible fact is a claim citing a block+quote;
    the canonical entity id is an internal resolution artifact, not itself a quote.

Deterministic ids (pure stdlib hashlib) make writes idempotent: the same logical
claim always hashes to the same `claim_id`, so re-extraction upserts, never dups.

NOTE: not wired into app startup or any live path yet — that is a later task.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date
from typing import Any, Sequence

_log = logging.getLogger("roster.claimgraph")


def _ef_search(cap: int) -> int:
    """HNSW candidate depth for a `cap`-row vector query: at least 100, at most Postgres's hard
    limit of 1000 (a larger value makes SET LOCAL fail → the whole query fails → an empty slate)."""
    return max(100, min(int(cap) + 40, 1000))

_WS = re.compile(r"\s+")

# Legal-form suffix tokens stripped when normalizing a name for alias resolution.
# These are structural (a company's legal form), NOT a semantic judgement — the
# named four (inc/llc/ltd/corp) are the brief's floor; the rest are common forms.
_LEGAL_SUFFIXES = frozenset({
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "lp", "llp", "sa", "ag", "nv", "bv",
    "srl", "oy", "ab", "as", "pty",
})


# --------------------------------------------------------------------------- #
# Pure helpers (NO DB — unit-tested directly)                                 #
# --------------------------------------------------------------------------- #
def normalize_quote(s: str) -> str:
    """Whitespace-collapsed, lowercased — IDENTICAL to
    `roster_kernel.research.provenance.normalize` so a `quote_hash` computed here
    lines up with the span gate's normalized-substring check. Keep in lockstep."""
    return _WS.sub(" ", (s or "").strip().lower())


def normalize_name(s: str) -> str:
    """Normalize an entity name/alias for exact (non-fuzzy) resolution: lowercase,
    drop punctuation, collapse whitespace, and strip trailing legal-form suffixes
    (`Acme, Inc.` / `Acme LLC` / `Acme Corp` → `acme`). Structural only (Rule 18):
    this is a computable string normalization, not a semantic merge — slice-1 does
    NO fuzzy ER."""
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)          # punctuation → space (keeps digits/underscore)
    tokens = _WS.sub(" ", s).strip().split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def quote_hash(quote: str) -> str:
    """sha256 of the NORMALIZED quote (so trivial reflow/case never breaks dedup)."""
    return hashlib.sha256(normalize_quote(quote).encode("utf-8")).hexdigest()


def _sha(*parts: str) -> str:
    """sha256 over unit-separator-joined parts (0x1f can't appear in the inputs)."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def make_claim_id(*, tenant_id: str, subject_id: str, predicate: str, object_kind: str,
                  object_norm: str, object_entity_id: str, valid_from: str,
                  schema_version: int) -> str:
    """Deterministic claim id = sha256 of the DEDUP KEY. The object discriminator is
    the referenced entity for entity-claims, else the normalized value. `valid_from`
    is the ISO date string (or '' when unknown/static) — a different valid_from is a
    DIFFERENT logical claim (bitemporal), so it hashes differently."""
    obj = object_entity_id if object_kind == "entity" else object_norm
    return _sha(tenant_id, subject_id, predicate, object_kind, obj,
                valid_from or "", str(schema_version))


def make_evidence_id(*, claim_id: str, document_id: str, block_id: str,
                     quote_hash_hex: str) -> str:
    """Deterministic evidence id = sha256(claim_id|document_id|block_id|quote_hash)."""
    return _sha(claim_id, document_id, block_id, quote_hash_hex)


def _iso(d: "date | None") -> str:
    return d.isoformat() if d is not None else ""


def _as_dict(f: Any) -> dict:
    """asyncpg may hand back a jsonb column as a dict OR a JSON string — normalize to a dict
    (mirrors `app.py::_parse_facets`). Fail-safe → {}."""
    if isinstance(f, dict):
        return f
    try:
        return json.loads(f) if f else {}
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- #
# DDL — 7 tables, CREATE TABLE IF NOT EXISTS, on ROSTER_CORPUS_DSN             #
# --------------------------------------------------------------------------- #
_DDL = """
-- Typed entities. The row is a RESOLUTION ARTIFACT; identity is backed by identity
-- claims. Every USER-VISIBLE fact about the entity is a claim (below), not a column.
CREATE TABLE IF NOT EXISTS rs_entity (
    entity_id      text PRIMARY KEY,          -- natural key: 'domain:acme.com'|'yc:slug'|'cik:..'|'__unresolved:<norm>'
    tenant_id      text NOT NULL DEFAULT 'demo',
    kind           text NOT NULL,             -- company|person|investor|product|category|technology|market
    name           text NOT NULL,
    canonical_name text NOT NULL DEFAULT '',
    primary_domain text NOT NULL DEFAULT '',
    status         text NOT NULL DEFAULT 'active',   -- active|suppressed  (never DELETE)
    first_run_id   text NOT NULL DEFAULT '',
    facets         jsonb NOT NULL DEFAULT '{}',
    retrieved_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rs_entity_kind   ON rs_entity (tenant_id, kind);
CREATE INDEX IF NOT EXISTS ix_rs_entity_name   ON rs_entity (lower(name));
CREATE INDEX IF NOT EXISTS ix_rs_entity_domain ON rs_entity (primary_domain) WHERE primary_domain <> '';

-- alias -> entity resolution (strong-id + normalized-name bootstrap; no fuzzy ER in slice 1)
CREATE TABLE IF NOT EXISTS rs_entity_alias (
    alias_norm  text NOT NULL,
    entity_id   text NOT NULL REFERENCES rs_entity(entity_id),
    alias       text NOT NULL,
    source      text NOT NULL DEFAULT '',
    retrieved_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (alias_norm, entity_id)
);

-- The atomic bitemporal grounded claim. Object is EITHER a value or an entity ref.
CREATE TABLE IF NOT EXISTS rs_claim (
    claim_id        text PRIMARY KEY,          -- deterministic sha256 of the dedup key (see below)
    tenant_id       text NOT NULL DEFAULT 'demo',
    subject_id      text NOT NULL REFERENCES rs_entity(entity_id),
    predicate       text NOT NULL,             -- must be an rs_predicate.name with status='active' at write
    object_kind     text NOT NULL,             -- 'value' | 'entity'
    object_value    text NOT NULL DEFAULT '',  -- when object_kind='value'
    object_norm     text NOT NULL DEFAULT '',  -- normalized object for dedup/grouping
    object_entity_id text NOT NULL DEFAULT '', -- when object_kind='entity'
    unit            text NOT NULL DEFAULT '',
    valid_from      date,                       -- valid-time (nullable = unknown/static)
    valid_to        date,
    valid_granularity text NOT NULL DEFAULT '', -- 'year'|'quarter'|'month'|'day'|''
    observed_at     date,                        -- as-stated observation date if any
    ingested_at     timestamptz NOT NULL DEFAULT now(),
    superseded_at   timestamptz,                 -- set when a newer claim supersedes (never overwrite)
    retracted_at    timestamptz,                 -- set when source retracted
    confidence      numeric NOT NULL DEFAULT 0,
    schema_version  int NOT NULL DEFAULT 1,
    extractor_version text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_rs_claim_subj_pred ON rs_claim (tenant_id, subject_id, predicate);
CREATE INDEX IF NOT EXISTS ix_rs_claim_pred_obj  ON rs_claim (predicate, object_norm);
CREATE INDEX IF NOT EXISTS ix_rs_claim_pred_oent ON rs_claim (predicate, object_entity_id) WHERE object_entity_id <> '';
-- "current" fast-path: non-retracted, non-superseded
CREATE INDEX IF NOT EXISTS ix_rs_claim_current ON rs_claim (tenant_id, subject_id, predicate)
    WHERE retracted_at IS NULL AND superseded_at IS NULL;
-- Category-rollup covering index (design-improvement.md §2): covers
-- `distinct_categories`' `count(DISTINCT subject_id) GROUP BY object_norm` predicate-first
-- aggregate (the subject-leading "current" index above does NOT help a predicate-first scan).
CREATE INDEX IF NOT EXISTS ix_rs_claim_cat_rollup
    ON rs_claim (tenant_id, predicate, object_norm, subject_id);

-- Per-claim provenance, 1..N per claim. Each row is span-gate-ready (carries block_id + quote).
CREATE TABLE IF NOT EXISTS rs_claim_evidence (
    evidence_id    text PRIMARY KEY,           -- sha256(claim_id|document_id|block_id|quote_hash)
    claim_id       text NOT NULL REFERENCES rs_claim(claim_id),
    tenant_id      text NOT NULL DEFAULT 'demo',
    workspace_id   text NOT NULL DEFAULT '',
    document_id    text NOT NULL,
    block_id       text NOT NULL,
    quote          text NOT NULL,
    quote_hash     text NOT NULL,
    source_key     text NOT NULL DEFAULT '',
    authority_tier int NOT NULL DEFAULT 0,
    evidence_kind  text NOT NULL DEFAULT '',
    evidence_status text NOT NULL DEFAULT 'active',  -- active|stale|retracted (GC coupling sets 'stale')
    retrieved_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (claim_id, block_id, quote_hash)
);
CREATE INDEX IF NOT EXISTS ix_rs_claim_ev_doc   ON rs_claim_evidence (document_id);
CREATE INDEX IF NOT EXISTS ix_rs_claim_ev_block ON rs_claim_evidence (block_id);
-- Winning-evidence lateral join (design-improvement.md §2): a PARTIAL index on the
-- active-evidence subset keyed by claim_id serves the `WHERE claim_id=… AND
-- evidence_status='active' ORDER BY authority_tier DESC, retrieved_at` sub-select. Key
-- carries authority_tier/retrieved_at so the ORDER BY is index-ordered (portable form —
-- no INCLUDE, which older PG lacks on partials).
CREATE INDEX IF NOT EXISTS ix_rs_claim_ev_active
    ON rs_claim_evidence (claim_id, authority_tier DESC, retrieved_at)
    WHERE evidence_status = 'active';

-- Winner per (subject, predicate, valid-bucket); losers stay queryable in rs_claim.
CREATE TABLE IF NOT EXISTS rs_claim_resolution (
    tenant_id        text NOT NULL DEFAULT 'demo',
    subject_id       text NOT NULL,
    predicate        text NOT NULL,
    valid_bucket     text NOT NULL DEFAULT '',   -- '' for static; else e.g. '2025' for time-bucketed
    winning_claim_id text NOT NULL REFERENCES rs_claim(claim_id),
    conflict_claim_ids jsonb NOT NULL DEFAULT '[]',
    winner_authority_tier int NOT NULL DEFAULT 0,
    rationale        text NOT NULL DEFAULT '',
    resolved_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, subject_id, predicate, valid_bucket)
);

-- Predicate registry: growable-hybrid. Production extraction emits only status='active'.
CREATE TABLE IF NOT EXISTS rs_predicate (
    name            text PRIMARY KEY,
    status          text NOT NULL DEFAULT 'candidate',  -- active|candidate|retired
    object_kind     text NOT NULL DEFAULT 'value',      -- value|entity|either
    object_entity_kind text NOT NULL DEFAULT '',        -- (entity predicates) rs_entity.kind to MINT for the object
    cardinality     text NOT NULL DEFAULT 'multi',      -- single|multi
    unit_hint       text NOT NULL DEFAULT '',
    temporal_policy text NOT NULL DEFAULT 'static',     -- point|interval|static
    description     text NOT NULL DEFAULT '',
    added_at        timestamptz NOT NULL DEFAULT now()
);
-- ALTER-ensure: CREATE TABLE IF NOT EXISTS never migrates an EXISTING prod rs_predicate,
-- so add the registry-authoritative column idempotently (mirrors the kernel GraphStore
-- ADD COLUMN IF NOT EXISTS pattern). No-op on a fresh DB (column already present above).
ALTER TABLE rs_predicate ADD COLUMN IF NOT EXISTS object_entity_kind text NOT NULL DEFAULT '';

-- Extraction-run audit + cost ledger.
CREATE TABLE IF NOT EXISTS rs_extraction_run (
    run_id           text PRIMARY KEY,
    tenant_id        text NOT NULL DEFAULT 'demo',
    schema_version   int NOT NULL DEFAULT 1,
    source_keys      text NOT NULL DEFAULT '',
    blocks_considered int NOT NULL DEFAULT 0,
    blocks_relevant   int NOT NULL DEFAULT 0,
    extract_calls     int NOT NULL DEFAULT 0,
    entail_calls      int NOT NULL DEFAULT 0,
    claims_emitted    int NOT NULL DEFAULT 0,
    claims_gated_out  int NOT NULL DEFAULT 0,
    est_cost_usd     numeric NOT NULL DEFAULT 0,
    params           jsonb NOT NULL DEFAULT '{}',
    status           text NOT NULL DEFAULT 'running',   -- running|done|failed|dry_run
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);

-- MENTION LANE (mention-first ER). An evidence-bound name that has NO safe canonical identity
-- (no strong id, no exact single match, no confident merge). It is NOT a canonical rs_entity —
-- graph VIEWS never read this table — so a bare name can never pollute the entity space
-- (no duplicate, no wrong merge). A mention is PROMOTED to a real rs_entity later, on stronger
-- evidence (a strong id, an adjudicated match). Never DELETE; promotion sets status+promoted_to.
CREATE TABLE IF NOT EXISTS rs_mention (
    mention_id   text PRIMARY KEY,        -- deterministic 'mention:<kind>:<norm>' (idempotent per name)
    tenant_id    text NOT NULL DEFAULT 'demo',
    kind         text NOT NULL,           -- company|person|investor|product|technology|market
    name         text NOT NULL,
    norm         text NOT NULL,
    status       text NOT NULL DEFAULT 'unresolved',  -- unresolved|promoted
    promoted_to  text NOT NULL DEFAULT '',             -- canonical entity_id once promoted
    n_evidence   int  NOT NULL DEFAULT 0,
    first_seen   timestamptz NOT NULL DEFAULT now(),
    last_seen    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rs_mention_norm ON rs_mention (tenant_id, kind, norm);

-- GROUNDED FACET READ-MODEL (people-population enumeration). A materialized projection of the
-- claim graph's per-attribute VALUE claims into flat, indexed, filterable facet rows — each row
-- CARRIES the source claim + evidence it was derived from, so a facet-filtered enumeration is still
-- grounded (the row links back to a span-verified claim). Domain-neutral: `facet_key` values
-- (title/seniority/function/metro/company/...) are vertical-supplied vocabulary, not kernel semantics.
-- This is a READ MODEL (option-c per the design panel), NOT a second truth graph — rs_claim remains
-- authoritative; a projector (or a direct grounded write at seed time) fills this from active claims.
CREATE TABLE IF NOT EXISTS roster_entity_facet (
    tenant_id          text NOT NULL DEFAULT 'demo',
    entity_id          text NOT NULL,           -- the person (rs_entity.entity_id, kind='person')
    facet_key          text NOT NULL,           -- vertical vocab: title|seniority|function|metro|company
    facet_value_norm   text NOT NULL,           -- normalized filter value (e.g. 'director','bay_area')
    display_value      text NOT NULL DEFAULT '',-- human display (e.g. 'Director of Machine Learning')
    source_claim_id    text NOT NULL DEFAULT '',-- the rs_claim this facet was derived from (grounding)
    source_document_id text NOT NULL DEFAULT '',-- + the evidence span (document_id, block_id) for citation
    source_block_id    text NOT NULL DEFAULT '',
    confidence         real NOT NULL DEFAULT 0,
    valid_as_of        date,
    PRIMARY KEY (tenant_id, entity_id, facet_key, facet_value_norm)
);
-- Facet-filter index: the hot enumeration path is `WHERE facet_key=$ AND facet_value_norm=$` per facet,
-- intersected on entity_id — this covers each leg of the intersection.
CREATE INDEX IF NOT EXISTS ix_roster_facet_kv ON roster_entity_facet (tenant_id, facet_key, facet_value_norm);

-- Committed DDL for the JOBS + PEOPLE-VECTOR + INGEST-CHECKPOINT tables. These exist in prod but were
-- created ad-hoc (no committed DDL); this mirrors the LIVE schema exactly so `CREATE ... IF NOT EXISTS`
-- is a no-op on prod and a fresh DB is reproducible. (Verified via /admin/schema.)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS rs_job (
    id          bigserial PRIMARY KEY,
    company     text NOT NULL,
    title       text NOT NULL,
    location    text,
    department  text,
    url         text,
    source      text,
    title_norm  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    embedding   vector(1536)
);
CREATE UNIQUE INDEX IF NOT EXISTS rs_job_company_title_location_source_key ON rs_job (company, title, location, source);
CREATE INDEX IF NOT EXISTS idx_rs_job_company   ON rs_job (company);
CREATE INDEX IF NOT EXISTS idx_rs_job_titlenorm ON rs_job (title_norm);
CREATE INDEX IF NOT EXISTS idx_job_vec ON rs_job USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS rs_person_vec (
    entity_id text PRIMARY KEY,
    embedding vector(1536)
);
CREATE INDEX IF NOT EXISTS idx_person_vec ON rs_person_vec USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS rs_ingest_checkpoint (
    source     text NOT NULL,          -- engine: 'jobs' | 'people'
    cursor_key text NOT NULL,          -- board 'greenhouse:stripe' or a people search window
    status     text NOT NULL DEFAULT 'done',
    n_seen     int  NOT NULL DEFAULT 0,
    n_written  int  NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, cursor_key)
);
"""

# Whitelisted numeric counters finish_run() may update (guards the dynamic SET).
_RUN_COUNTERS = frozenset({
    "schema_version", "blocks_considered", "blocks_relevant", "extract_calls",
    "entail_calls", "claims_emitted", "claims_gated_out", "est_cost_usd",
})


class ClaimGraphStore:
    """Async Postgres-backed grounded claim graph. Schema + predicate seed ensured
    lazily via `ensure_schema()`; all writes are deterministic-id idempotent."""

    def __init__(self, dsn: str, *,
                 seed_predicates: "list[dict] | None" = None,
                 placement_predicate: str = "operates_in_category",
                 subject_kind: str = "company",
                 mintable_entity_kinds: "tuple[str, ...]" = (
                     "category", "person", "investor", "company")):
        """Injected vocabulary (mirrors `GraphStore(relations=…)`), backward-compatible
        defaults so a bare `ClaimGraphStore(dsn)` keeps the tech reads/mint literals:
          * `seed_predicates`  — predicate dicts seeded into `rs_predicate` by
            `ensure_schema()`; None seeds nothing (the caller injects the registry).
          * `placement_predicate` — the predicate that PLACES a subject in a category
            (drives `distinct_categories` / `population_claims` scope); tech default
            'operates_in_category'.
          * `subject_kind` — the primary subject entity-kind for reads/mint; tech 'company'.
          * `mintable_entity_kinds` — object entity-kinds the extraction job may mint a
            deterministic node for."""
        self._dsn = dsn
        self._pool = None
        self._ready = False
        self._seed_predicates = list(seed_predicates) if seed_predicates is not None else None
        self._placement_predicate = placement_predicate
        self._subject_kind = subject_kind
        self._mintable_entity_kinds = tuple(mintable_entity_kinds)

    # Injected vocabulary — read-only accessors the extraction job reads (so a new vertical
    # entity-kind / subject-kind needs no store or job code edit, only different wiring).
    @property
    def placement_predicate(self) -> str:
        return self._placement_predicate

    @property
    def subject_kind(self) -> str:
        return self._subject_kind

    @property
    def mintable_entity_kinds(self) -> "tuple[str, ...]":
        return self._mintable_entity_kinds

    @property
    def seed_predicates(self) -> "list[dict] | None":
        return self._seed_predicates

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=4)
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._ready = False

    async def ensure_schema(self) -> None:
        """Create the 7 tables (idempotent) THEN seed the INJECTED predicate registry
        (`self._seed_predicates`; None → seed nothing, the caller injects). The store
        imports no vertical vocabulary — it seeds exactly what it was constructed with.

        Re-seed makes `rs_predicate` the authoritative config: `ON CONFLICT (name) DO
        UPDATE` refreshes the DESCRIPTIVE columns (object_kind/object_entity_kind/
        cardinality/unit_hint/temporal_policy/description) so a predicate's config can
        evolve — but NEVER resurrects a manually-retired predicate (status is only
        updated when the stored status is not already 'retired'), preserving the
        append/never-resurrect ethos."""
        if self._ready:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
            for p in (self._seed_predicates or []):
                await conn.execute(
                    """INSERT INTO rs_predicate
                         (name, status, object_kind, object_entity_kind, cardinality,
                          unit_hint, temporal_policy, description)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                       ON CONFLICT (name) DO UPDATE SET
                         object_kind        = EXCLUDED.object_kind,
                         object_entity_kind = EXCLUDED.object_entity_kind,
                         cardinality        = EXCLUDED.cardinality,
                         unit_hint          = EXCLUDED.unit_hint,
                         temporal_policy    = EXCLUDED.temporal_policy,
                         description        = EXCLUDED.description,
                         -- never un-retire a manually-retired predicate (never-resurrect)
                         status = CASE WHEN rs_predicate.status = 'retired'
                                       THEN rs_predicate.status ELSE EXCLUDED.status END""",
                    p["name"], p.get("status", "candidate"),
                    p.get("object_kind", "value"), p.get("object_entity_kind", ""),
                    p.get("cardinality", "multi"), p.get("unit_hint", ""),
                    p.get("temporal_policy", "static"), p.get("description", ""))
        self._ready = True

    async def active_predicate_names(self) -> list[dict]:
        """The AUTHORITATIVE active-predicate read: the `status='active'` rows of
        `rs_predicate`, each as a dict carrying the config the extraction job needs
        (name/status/object_kind/object_entity_kind/cardinality/temporal_policy/
        unit_hint/description). Tenant-agnostic (`rs_predicate` is global). Empty when
        the registry has not been seeded yet (first run)."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, status, object_kind, object_entity_kind, cardinality, "
                "temporal_policy, unit_hint, description FROM rs_predicate "
                "WHERE status = 'active' ORDER BY name")
        return [dict(r) for r in rows]

    # ---- entities & aliases ------------------------------------------------ #
    async def upsert_entity(self, entity_id: str, kind: str, name: str, *,
                            canonical_name: str = "", primary_domain: str = "",
                            facets: dict | None = None, first_run_id: str = "",
                            tenant_id: str = "demo") -> str:
        """Upsert the resolution-artifact row. Refreshes name/canonical/domain/facets/
        retrieved_at on conflict; NEVER resets `status` (suppression is sticky)."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_entity
                     (entity_id, tenant_id, kind, name, canonical_name, primary_domain,
                      first_run_id, facets)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                   ON CONFLICT (entity_id) DO UPDATE SET
                     name = EXCLUDED.name,
                     canonical_name = EXCLUDED.canonical_name,
                     primary_domain = EXCLUDED.primary_domain,
                     facets = EXCLUDED.facets,
                     retrieved_at = now()""",
                entity_id, tenant_id, kind, name, canonical_name, primary_domain,
                first_run_id, json.dumps(facets or {}))
        return entity_id

    async def upsert_mention(self, *, name: str, kind: str, tenant_id: str = "demo") -> str:
        """Park an evidence-bound name in the MENTION LANE (mention-first ER). FULLY IDEMPOTENT per
        (kind, norm): re-seeing the same name only touches `last_seen`, never a new row and never an
        inflated counter (corroboration is DERIVED from the distinct grounded claims at promotion
        time — see `promote_corroborated_mentions` — so a re-run can never falsely promote). Returns
        the deterministic `mention:<kind>:<norm>` id. NOT a canonical entity — graph views never read
        `rs_mention`, so a mention can never pollute the entity space."""
        await self.ensure_schema()
        norm = normalize_name(name)
        mention_id = f"mention:{kind}:{norm}"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_mention (mention_id, tenant_id, kind, name, norm, n_evidence)
                   VALUES ($1,$2,$3,$4,$5,0)
                   ON CONFLICT (mention_id) DO UPDATE SET last_seen = now()""",
                mention_id, tenant_id, kind, name, norm)
        return mention_id

    async def promote_mention(self, mention_id: str, entity_id: str, *,
                              tenant_id: str = "demo") -> None:
        """Mark a mention PROMOTED to a canonical entity (once it earned a real identity anchor).
        Append-only status flip; the mention row is retained for provenance/audit."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE rs_mention SET status='promoted', promoted_to=$2, last_seen=now()
                   WHERE mention_id=$1 AND tenant_id=$3""",
                mention_id, entity_id, tenant_id)

    async def promote_corroborated_mentions(self, *, tenant_id: str = "demo",
                                            min_corroboration: int = 3,
                                            kinds: tuple[str, ...] | None = None) -> list[dict]:
        """PROMOTION BY CORROBORATION — the zero-pollution way a tech/product/investor/segment NODE is
        born. Corroboration is DERIVED (idempotently) from the distinct grounded claims: a mention
        whose `norm` is the object of active-evidence claims from `>= min_corroboration` DISTINCT
        subjects (companies) is a REAL thing (many independent grounded companies name it), so it earns
        a canonical `rs_entity` at `<kind>:<norm>` — NOT soft-minted from one bare name (the pollution),
        and NOT counted from a raw upsert counter (which a re-run would inflate). Fully idempotent:
        re-running never changes the distinct-subject count, and an already-promoted mention is skipped;
        upsert_entity is idempotent. `kinds` optionally restricts which node kinds may be promoted.
        Returns [{mention_id, entity_id, kind, name, corroboration}]."""
        await self.ensure_schema()
        pool = await self._get_pool()
        params: list = [tenant_id]
        kind_clause = ""
        if kinds:
            kind_clause = " AND kind = ANY($2)"
            params.append(list(kinds))
        async with pool.acquire() as conn:
            mentions = await conn.fetch(
                f"""SELECT mention_id, kind, name, norm FROM rs_mention
                    WHERE tenant_id=$1 AND status='unresolved'{kind_clause}""",
                *params)
            promoted: list[dict] = []
            for m in mentions:
                # DERIVED corroboration: distinct subjects whose active-evidence claim has this norm
                # as its object. Idempotent — a re-run cannot change the distinct count.
                corr = await conn.fetchval(
                    """SELECT count(DISTINCT c.subject_id) FROM rs_claim c
                       WHERE c.tenant_id=$1 AND c.object_norm=$2
                         AND EXISTS (SELECT 1 FROM rs_claim_evidence e
                                     WHERE e.claim_id=c.claim_id AND e.evidence_status='active')""",
                    tenant_id, m["norm"]) or 0
                if corr < int(min_corroboration):
                    continue
                entity_id = f"{m['kind']}:{m['norm']}"
                await self.upsert_entity(entity_id, kind=m["kind"], name=m["name"],
                                         tenant_id=tenant_id)
                await self.promote_mention(m["mention_id"], entity_id, tenant_id=tenant_id)
                promoted.append({"mention_id": m["mention_id"], "entity_id": entity_id,
                                 "kind": m["kind"], "name": m["name"], "corroboration": corr})
        return promoted

    async def promote_corroborated_claims(self, *, tenant_id: str = "demo",
                                          min_corroboration: int = 2,
                                          predicate_kinds: dict[str, str]) -> list[dict]:
        """CLAIM-DERIVED promotion of HUB nodes (technology / product / market / category /
        investor / competitor-company). Supersedes `promote_corroborated_mentions` for these
        kinds: it derives candidates from the grounded VALUE claims themselves — NOT from
        `rs_mention` rows — so a norm that has claims but never got a mention row (older ingest;
        e.g. the 32 `uses_technology` values with zero technology mentions) still promotes.

        `predicate_kinds` maps a hub predicate → the node kind to mint (config, Rule 18 — the LLM
        already chose the predicate; this only says what kind of node its object is). For each
        (predicate, object_norm) named by `>= min_corroboration` DISTINCT subjects via active
        evidence, upsert a canonical `<kind>:<norm>` entity, then upsert+mark a mention row
        `promoted` (the render-join in `/graph/explore` keys off `rs_mention.status='promoted'`).
        Fully idempotent. Does NOT collapse aliases (a16z ≈ Andreessen Horowitz) — that is a
        SEMANTIC merge the LLM owns (Rule 18), deliberately deferred, never regex-hacked here.
        Returns [{entity_id, kind, name, corroboration}]."""
        await self.ensure_schema()
        pool = await self._get_pool()
        promoted: list[dict] = []
        async with pool.acquire() as conn:
            for predicate, kind in predicate_kinds.items():
                rows = await conn.fetch(
                    """SELECT cl.object_norm,
                              mode() WITHIN GROUP (ORDER BY cl.object_value) name,
                              count(DISTINCT cl.subject_id) corr
                       FROM rs_claim cl
                       WHERE cl.tenant_id=$1 AND cl.predicate=$2 AND cl.object_kind='value'
                         AND cl.object_norm <> ''
                         AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                     WHERE ev.claim_id=cl.claim_id AND ev.evidence_status='active')
                       GROUP BY cl.object_norm
                       HAVING count(DISTINCT cl.subject_id) >= $3""",
                    tenant_id, predicate, int(min_corroboration))
                for r in rows:
                    entity_id = f"{kind}:{r['object_norm']}"
                    await self.upsert_entity(entity_id, kind=kind, name=r["name"] or r["object_norm"],
                                             tenant_id=tenant_id)
                    mid = await self.upsert_mention(name=r["name"] or r["object_norm"], kind=kind,
                                                    tenant_id=tenant_id)
                    await self.promote_mention(mid, entity_id, tenant_id=tenant_id)
                    promoted.append({"entity_id": entity_id, "kind": kind,
                                     "name": r["name"] or r["object_norm"], "corroboration": r["corr"]})
        return promoted

    async def promote_person_claims(self, *, tenant_id: str = "demo",
                                    person_predicates: tuple[str, ...] = ("has_founder", "key_person")
                                    ) -> list[dict]:
        """PER-COMPANY person promotion (People nodes). Founders/key-people are 1:1 with their
        company — they NEVER reach corroboration, so the hub path (`promote_corroborated_claims`)
        can never surface them; this promotes each grounded `has_founder`/`key_person` VALUE claim
        DIRECTLY (threshold=1 by design — a named founder IS an identified person relative to that
        company). SPLIT-FIRST, merge-later: the entity id is scoped to the subject company
        (`person:<subject_id>:<person_norm>`), so two different 'John Smith' founders at two
        different companies can NEVER wrongly merge into one global person node. Idempotent —
        re-run yields the same ids. The claim rows are left immutable (StateSnapshot spirit); the
        person NODES + edges are materialized in `/graph/explore` from the same value claims with
        the identical id derivation. Returns [{entity_id, name, company}]."""
        await self.ensure_schema()
        pool = await self._get_pool()
        promoted: list[dict] = []
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT cl.subject_id, s.name sname, cl.object_value, cl.object_norm
                   FROM rs_claim cl JOIN rs_entity s ON s.entity_id=cl.subject_id
                   WHERE cl.tenant_id=$1 AND cl.predicate = ANY($2) AND cl.object_kind='value'
                     AND cl.object_norm <> ''
                     AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                 WHERE ev.claim_id=cl.claim_id AND ev.evidence_status='active')""",
                tenant_id, list(person_predicates))
            for r in rows:
                entity_id = f"person:{r['subject_id']}:{r['object_norm']}"
                await self.upsert_entity(entity_id, kind="person", name=r["object_value"],
                                         tenant_id=tenant_id)
                promoted.append({"entity_id": entity_id, "name": r["object_value"],
                                 "company": r["sname"]})
        return promoted

    async def distinct_object_forms(self, predicates: tuple[str, ...] | list[str], *,
                                    tenant_id: str = "demo") -> list[tuple[str, str, str]]:
        """Distinct grounded-value object forms for the given predicates → [(representative_surface,
        object_norm, representative_quote)]. Surface = the MOST FREQUENT `object_value` for each norm;
        quote = one active-evidence quote for that norm (structural, Rule 18). Feeds the LLM
        alias-resolver — the surface forms are clustered, and the quote gives the pairwise same-entity
        VERIFIER real context (panel: never merge on bare strings)."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT cl.object_norm norm,
                          mode() WITHIN GROUP (ORDER BY cl.object_value) surface,
                          min(ev.quote) quote
                   FROM rs_claim cl
                   JOIN rs_claim_evidence ev
                     ON ev.claim_id = cl.claim_id AND ev.evidence_status = 'active'
                   WHERE cl.tenant_id = $1 AND cl.predicate = ANY($2) AND cl.object_kind = 'value'
                     AND cl.object_norm <> ''
                   GROUP BY cl.object_norm""",
                tenant_id, list(predicates))
        return [(r["surface"] or r["norm"], r["norm"], r["quote"] or "") for r in rows]

    async def add_alias(self, alias: str, entity_id: str, source: str = "") -> str:
        """Register a normalized alias → entity mapping (exact-match resolution only)."""
        await self.ensure_schema()
        alias_norm = normalize_name(alias)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_entity_alias (alias_norm, entity_id, alias, source)
                   VALUES ($1,$2,$3,$4)
                   ON CONFLICT (alias_norm, entity_id) DO UPDATE SET
                     alias = EXCLUDED.alias, source = EXCLUDED.source,
                     retrieved_at = now()""",
                alias_norm, entity_id, alias, source)
        return alias_norm

    async def resolve_alias(self, alias: str) -> str | None:
        """Exact normalized-alias lookup only (NO fuzzy ER in slice 1)."""
        await self.ensure_schema()
        alias_norm = normalize_name(alias)
        if not alias_norm:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT entity_id FROM rs_entity_alias WHERE alias_norm = $1 LIMIT 1",
                alias_norm)

    async def get_entity(self, entity_id: str, *, tenant_id: str = "demo") -> dict | None:
        """Fetch one ACTIVE entity's resolution row (id/name/kind) by strong id. Returns
        `{entity_id, name, kind}` or None. Used by the diligence route to get a resolved
        subject's display name (`entity_claims` returns claims, not the entity name)."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT entity_id, name, kind FROM rs_entity "
                "WHERE entity_id = $1 AND tenant_id = $2 AND status = 'active'",
                entity_id, tenant_id)
        return None if r is None else {
            "entity_id": r["entity_id"], "name": r["name"], "kind": r["kind"]}

    async def find_company(self, name: str, *, tenant_id: str = "demo") -> dict | None:
        """Resolve a company by EXACT NORMALIZED-NAME match (Rule 18: computable string
        normalization, NOT fuzzy ER). Compares `normalize_name(query)` against the
        `normalize_name` of each active company's name; returns the first match's
        `{entity_id, name, kind}` (deterministic id order) or None. Restricted to the
        store's `subject_kind` (tech default 'company'), so a person/investor/category
        never shadows a company diligence request."""
        await self.ensure_schema()
        target = normalize_name(name)
        if not target:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT entity_id, name, kind FROM rs_entity "
                "WHERE tenant_id = $1 AND status = 'active' AND kind = $2 "
                "ORDER BY entity_id",
                tenant_id, self._subject_kind)
        for r in rows:
            if normalize_name(r["name"]) == target:
                return {"entity_id": r["entity_id"], "name": r["name"], "kind": r["kind"]}
        return None

    async def company_norm_map(self, *, tenant_id: str = "demo") -> dict[str, dict]:
        """The whole active-company registry as `{normalize_name(name): {entity_id, name}}` in ONE
        query — for resolving many prose mentions at once (vs find_company's per-name full scan).
        Exact normalized match only (Rule 18: computable, non-fuzzy). First id wins on a norm clash."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT entity_id, name FROM rs_entity "
                "WHERE tenant_id = $1 AND status = 'active' AND kind = $2 ORDER BY entity_id",
                tenant_id, self._subject_kind)
        out: dict[str, dict] = {}
        for r in rows:
            key = normalize_name(r["name"])
            if key and key not in out:
                out[key] = {"entity_id": r["entity_id"], "name": r["name"]}
        return out

    # ---- claims & evidence ------------------------------------------------- #
    async def upsert_claim(self, *, subject_id: str, predicate: str, object_kind: str,
                           object_value: str = "", object_entity_id: str = "",
                           object_norm: str | None = None, unit: str = "",
                           valid_from: "date | None" = None, valid_to: "date | None" = None,
                           valid_granularity: str = "", observed_at: "date | None" = None,
                           confidence: float = 0, schema_version: int = 1,
                           extractor_version: str = "", tenant_id: str = "demo") -> str:
        """Append-only, deterministic-id upsert of one grounded claim. Same logical
        claim → same `claim_id` → idempotent (re-extraction refreshes confidence/
        ingested_at only). NEVER flips supersede/retract here — those are separate
        lifecycle transitions."""
        await self.ensure_schema()
        if object_norm is None:
            object_norm = object_entity_id if object_kind == "entity" else normalize_name(object_value)
        claim_id = make_claim_id(
            tenant_id=tenant_id, subject_id=subject_id, predicate=predicate,
            object_kind=object_kind, object_norm=object_norm,
            object_entity_id=object_entity_id, valid_from=_iso(valid_from),
            schema_version=schema_version)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_claim
                     (claim_id, tenant_id, subject_id, predicate, object_kind,
                      object_value, object_norm, object_entity_id, unit, valid_from,
                      valid_to, valid_granularity, observed_at, confidence,
                      schema_version, extractor_version)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                   ON CONFLICT (claim_id) DO UPDATE SET
                     confidence = EXCLUDED.confidence,
                     ingested_at = now()""",
                claim_id, tenant_id, subject_id, predicate, object_kind,
                object_value, object_norm, object_entity_id, unit, valid_from,
                valid_to, valid_granularity, observed_at, confidence,
                schema_version, extractor_version)
        return claim_id

    async def add_evidence(self, claim_id: str, document_id: str, block_id: str,
                           quote: str, *, source_key: str = "", authority_tier: int = 0,
                           evidence_kind: str = "", workspace_id: str = "",
                           tenant_id: str = "demo") -> str:
        """Attach a span-gate-ready provenance row to a claim. Deduped on
        (claim_id, block_id, quote_hash) — re-citing the same span is a no-op."""
        await self.ensure_schema()
        qh = quote_hash(quote)
        evidence_id = make_evidence_id(
            claim_id=claim_id, document_id=document_id, block_id=block_id,
            quote_hash_hex=qh)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_claim_evidence
                     (evidence_id, claim_id, tenant_id, workspace_id, document_id,
                      block_id, quote, quote_hash, source_key, authority_tier,
                      evidence_kind)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                   ON CONFLICT (claim_id, block_id, quote_hash) DO NOTHING""",
                evidence_id, claim_id, tenant_id, workspace_id, document_id,
                block_id, quote, qh, source_key, authority_tier, evidence_kind)
        return evidence_id

    async def mark_evidence_stale(self, document_ids: Sequence[str]) -> int:
        """GC-coupling hook: when `rs_block` hard-deletes docs, flip their still-active
        evidence to 'stale' (never delete) so answers exclude claims whose span can no
        longer load until re-extracted. Returns the number of rows flipped."""
        if not document_ids:
            return 0
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                "UPDATE rs_claim_evidence SET evidence_status = 'stale' "
                "WHERE document_id = ANY($1) AND evidence_status = 'active'",
                list(document_ids))
        try:
            return int((res or "UPDATE 0").split()[-1])
        except ValueError:
            return 0

    # ---- extraction-run ledger -------------------------------------------- #
    async def record_run(self, run_id: str, *, source_keys: str = "",
                         schema_version: int = 1, params: dict | None = None,
                         status: str = "running", tenant_id: str = "demo") -> str:
        """Open (or refresh) an extraction-run audit/cost row."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_extraction_run
                     (run_id, tenant_id, schema_version, source_keys, params, status)
                   VALUES ($1,$2,$3,$4,$5::jsonb,$6)
                   ON CONFLICT (run_id) DO UPDATE SET
                     source_keys = EXCLUDED.source_keys,
                     params = EXCLUDED.params,
                     status = EXCLUDED.status""",
                run_id, tenant_id, schema_version, source_keys,
                json.dumps(params or {}), status)
        return run_id

    async def finish_run(self, run_id: str, *, status: str = "done", **counts: Any) -> None:
        """Close a run: set status + finished_at and update any whitelisted counters
        passed as kwargs (blocks_considered, extract_calls, claims_emitted, est_cost_usd, …)."""
        await self.ensure_schema()
        sets = ["status = $2", "finished_at = now()"]
        args: list[Any] = [run_id, status]
        for k, v in counts.items():
            if k not in _RUN_COUNTERS:
                raise ValueError(f"unknown run counter {k!r}")
            args.append(v)
            sets.append(f"{k} = ${len(args)}")
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"UPDATE rs_extraction_run SET {', '.join(sets)} WHERE run_id = $1", *args)

    # ---- grounded reads (used by later tasks) ------------------------------ #
    async def population(self, predicate: str, object_norm: str, *,
                         tenant_id: str = "demo") -> list[dict]:
        """Subjects (entity rows) with an ACTIVE claim `predicate=… AND object_norm=…`,
        each joined to that claim + one active evidence row. Ordered by name. This is
        the 'who is in category C' population read the landscape map is built from."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT e.entity_id, e.name, e.kind, e.primary_domain,
                          c.claim_id, c.predicate, c.object_kind, c.object_value,
                          c.object_norm, c.object_entity_id, c.confidence,
                          ev.document_id, ev.block_id, ev.quote, ev.authority_tier
                   FROM rs_claim c
                   JOIN rs_entity e ON e.entity_id = c.subject_id AND e.status = 'active'
                   LEFT JOIN LATERAL (
                       SELECT document_id, block_id, quote, authority_tier
                       FROM rs_claim_evidence ev
                       WHERE ev.claim_id = c.claim_id AND ev.evidence_status = 'active'
                       ORDER BY ev.authority_tier DESC, ev.retrieved_at
                       LIMIT 1
                   ) ev ON true
                   WHERE c.tenant_id = $1 AND c.predicate = $2 AND c.object_norm = $3
                     AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                     AND ev.document_id IS NOT NULL   -- grounding+GC: exclude claims with no ACTIVE evidence
                   ORDER BY e.name""",
                tenant_id, predicate, object_norm)
        return [self._row_to_claim_dict(r) for r in rows]

    async def entity_claims(self, subject_id: str, *, predicates: Sequence[str] | None = None,
                            tenant_id: str = "demo") -> list[dict]:
        """Active claims for one entity (optionally filtered to `predicates`), each with
        its winning (highest-authority active) evidence — the per-cell grounded read
        compose cites. Ordered by predicate."""
        await self.ensure_schema()
        args: list[Any] = [tenant_id, subject_id]
        pred_clause = ""
        if predicates:
            args.append(list(predicates))
            pred_clause = f"AND c.predicate = ANY(${len(args)})"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT c.subject_id AS entity_id, '' AS name, '' AS kind,
                           '' AS primary_domain,
                           c.claim_id, c.predicate, c.object_kind, c.object_value,
                           c.object_norm, c.object_entity_id, c.confidence,
                           c.unit, c.valid_from, c.valid_to,
                           ev.document_id, ev.block_id, ev.quote, ev.authority_tier
                    FROM rs_claim c
                    LEFT JOIN LATERAL (
                        SELECT document_id, block_id, quote, authority_tier
                        FROM rs_claim_evidence ev
                        WHERE ev.claim_id = c.claim_id AND ev.evidence_status = 'active'
                        ORDER BY ev.authority_tier DESC, ev.retrieved_at
                        LIMIT 1
                    ) ev ON true
                    WHERE c.tenant_id = $1 AND c.subject_id = $2
                      AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                      AND ev.document_id IS NOT NULL   -- grounding+GC: only claims with ACTIVE evidence
                      {pred_clause}
                    ORDER BY c.predicate""",
                *args)
        out = []
        for r in rows:
            d = self._row_to_claim_dict(r)
            d["unit"] = r["unit"]
            d["valid_from"] = _iso(r["valid_from"]) or None
            d["valid_to"] = _iso(r["valid_to"]) or None
            out.append(d)
        return out

    # ---- canonicalization support (Task S2a) ------------------------------- #
    async def find_entities_by_norm(self, norm: str, kind: str, *,
                                    tenant_id: str = "demo") -> list[dict]:
        """Active entities of `kind` (tenant-scoped) whose canonical name OR any registered
        alias normalizes (`normalize_name`) to `norm`. Returns `{entity_id, name, facets}`
        per match, deterministic id order. Structural exact-normalized lookup only (Rule 18:
        a computable string match, NOT fuzzy ER) — this is the ambiguity surface the entity
        resolver hands to the LLM when it returns >1."""
        await self.ensure_schema()
        if not norm:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # Candidate set = entities of the kind that either (a) have an alias whose stored
            # alias_norm equals `norm`, or (b) are a superset we re-check by name in Python
            # (normalize_name is not an SQL function — mirror `find_company`'s Python re-check).
            alias_ids = {r["entity_id"] for r in await conn.fetch(
                """SELECT a.entity_id FROM rs_entity_alias a
                   JOIN rs_entity e ON e.entity_id = a.entity_id
                   WHERE a.alias_norm = $1 AND e.tenant_id = $2
                     AND e.status = 'active' AND e.kind = $3""",
                norm, tenant_id, kind)}
            rows = await conn.fetch(
                "SELECT entity_id, name, facets FROM rs_entity "
                "WHERE tenant_id = $1 AND status = 'active' AND kind = $2 ORDER BY entity_id",
                tenant_id, kind)
        out: list[dict] = []
        for r in rows:
            if r["entity_id"] in alias_ids or normalize_name(r["name"]) == norm:
                out.append({"entity_id": r["entity_id"], "name": r["name"],
                            "facets": _as_dict(r["facets"])})
        return out

    async def subject_predicate_claims(self, subject_id: str, predicate: str, *,
                                       tenant_id: str = "demo") -> list[dict]:
        """ACTIVE (non-retracted, non-superseded) claims for one `(subject, predicate)`, each
        carrying the fields conflict resolution orders on: `claim_id, object_kind,
        object_value, object_entity_id, object_norm, confidence, valid_from, ingested_at`.
        Evidence is fetched separately via `claim_evidence_tiers` (a claim may cite N spans)."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT claim_id, object_kind, object_value, object_entity_id, object_norm,
                          confidence, valid_from, ingested_at
                   FROM rs_claim
                   WHERE tenant_id = $1 AND subject_id = $2 AND predicate = $3
                     AND retracted_at IS NULL AND superseded_at IS NULL
                   ORDER BY claim_id""",
                tenant_id, subject_id, predicate)
        return [{
            "claim_id": r["claim_id"], "object_kind": r["object_kind"],
            "object_value": r["object_value"], "object_entity_id": r["object_entity_id"],
            "object_norm": r["object_norm"],
            "confidence": float(r["confidence"]) if r["confidence"] is not None else 0.0,
            "valid_from": _iso(r["valid_from"]) or None, "ingested_at": r["ingested_at"],
        } for r in rows]

    async def claim_evidence_tiers(self, claim_id: str) -> list[dict]:
        """The ACTIVE evidence rows' `(authority_tier, evidence_kind)` for a claim, strongest
        first — the signal conflict resolution uses to detect controlling evidence and the
        claim's best authority tier."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT authority_tier, evidence_kind FROM rs_claim_evidence "
                "WHERE claim_id = $1 AND evidence_status = 'active' "
                "ORDER BY authority_tier DESC, retrieved_at",
                claim_id)
        return [{"authority_tier": int(r["authority_tier"]),
                 "evidence_kind": r["evidence_kind"] or ""} for r in rows]

    async def record_resolution(self, *, subject_id: str, predicate: str, valid_bucket: str,
                                winning_claim_id: str, conflict_claim_ids: Sequence[str],
                                winner_authority_tier: int, rationale: str,
                                tenant_id: str = "demo") -> None:
        """Upsert the WINNER row for a `(subject, predicate, valid_bucket)` group into
        `rs_claim_resolution`. Losers are named in `conflict_claim_ids` but NEVER retracted —
        they stay queryable in `rs_claim`. Idempotent via ON CONFLICT DO UPDATE, so re-running
        the resolver rewrites the same winner."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_claim_resolution
                     (tenant_id, subject_id, predicate, valid_bucket, winning_claim_id,
                      conflict_claim_ids, winner_authority_tier, rationale)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8)
                   ON CONFLICT (tenant_id, subject_id, predicate, valid_bucket) DO UPDATE SET
                     winning_claim_id = EXCLUDED.winning_claim_id,
                     conflict_claim_ids = EXCLUDED.conflict_claim_ids,
                     winner_authority_tier = EXCLUDED.winner_authority_tier,
                     rationale = EXCLUDED.rationale,
                     resolved_at = now()""",
                tenant_id, subject_id, predicate, valid_bucket, winning_claim_id,
                json.dumps(list(conflict_claim_ids)), winner_authority_tier, rationale)

    async def resolved_entity_claims(self, subject_id: str, *,
                                     predicates: Sequence[str] | None = None,
                                     tenant_id: str = "demo") -> list[dict]:
        """Winner-PREFERRED read: like `entity_claims`, but where a `rs_claim_resolution`
        winner exists for a `(subject, predicate, valid_bucket)` group, return ONLY the winner
        for that group with `is_resolved=True` and `alternates` (the loser claim summaries)
        attached; groups with no resolution return all active claims unchanged. This is the
        read a diligence brief uses to show 'sources agree / disagree'."""
        await self.ensure_schema()
        claims = await self.entity_claims(subject_id, predicates=predicates, tenant_id=tenant_id)
        resolutions = await self._fetch_resolutions(subject_id, predicates, tenant_id)

        # Group the active claims by (predicate, valid_bucket) — bucket = valid_from's year.
        groups: dict[tuple[str, str], list[dict]] = {}
        order: list[tuple[str, str]] = []
        for c in claims:
            vf = c.get("valid_from")
            bucket = vf[:4] if isinstance(vf, str) and len(vf) >= 4 else ""
            key = (c["predicate"], bucket)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(c)

        out: list[dict] = []
        for key in order:
            group = groups[key]
            res = resolutions.get(key)
            if res is None:
                out.extend(group)  # no resolution → all active claims unchanged
                continue
            winner = next((c for c in group if c["claim_id"] == res["winning_claim_id"]), None)
            if winner is None:
                # winner not currently grounded/queryable (its span was GC'd) → surface the
                # group unchanged rather than hide everything (fail-safe visibility).
                out.extend(group)
                continue
            alternates = [self._claim_summary(c) for c in group
                          if c["claim_id"] != winner["claim_id"]]
            w = {**winner, "is_resolved": True, "alternates": alternates,
                 "winner_authority_tier": int(res["winner_authority_tier"]),
                 "rationale": res["rationale"]}
            out.append(w)
        return out

    async def _fetch_resolutions(self, subject_id: str, predicates: Sequence[str] | None,
                                 tenant_id: str) -> dict:
        """This subject's `rs_claim_resolution` rows (optionally predicate-filtered), keyed by
        `(predicate, valid_bucket)`. Split out so the winner-preferred assembly in
        `resolved_entity_claims` is testable offline (a fake subclass overrides this)."""
        args: list[Any] = [tenant_id, subject_id]
        pred_clause = ""
        if predicates:
            args.append(list(predicates))
            pred_clause = f"AND predicate = ANY(${len(args)})"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res_rows = await conn.fetch(
                f"""SELECT predicate, valid_bucket, winning_claim_id, winner_authority_tier,
                           rationale
                    FROM rs_claim_resolution
                    WHERE tenant_id = $1 AND subject_id = $2 {pred_clause}""",
                *args)
        return {(r["predicate"], r["valid_bucket"]): dict(r) for r in res_rows}

    @staticmethod
    def _claim_summary(c: dict) -> dict:
        """Compact loser summary attached as an `alternate` (kept visible, never dropped)."""
        return {
            "claim_id": c["claim_id"], "object_kind": c["object_kind"],
            "object_value": c["object_value"], "object_entity_id": c["object_entity_id"],
            "object_norm": c["object_norm"], "confidence": c["confidence"],
            "evidence": c.get("evidence"),
        }

    # ---- aggregation reads (population → market map, Task 3) ---------------- #
    async def distinct_categories(self, *, tenant_id: str = "demo",
                                  min_members: int = 1) -> list[dict]:
        """DISTINCT market categories from ACTIVE, grounded placement-predicate claims
        (`self._placement_predicate`), each with a distinct-member (subject) count. Returns
        `[{object_norm, name, members}]` ordered by members DESC (name tie-break).

        Grounding-exclusion (same discipline as `population`): a claim is counted
        only when it has an ACTIVE evidence row — a company placed by a claim whose
        span was GC'd/stale never inflates a category. `name` is the category
        entity's `name` (join `rs_entity` on `object_entity_id`) or `object_norm`
        when no entity row exists. `members >= min_members` filters small clusters."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.object_norm AS object_norm,
                          COALESCE(NULLIF(max(cat.name), ''), c.object_norm) AS name,
                          count(DISTINCT c.subject_id) AS members
                   FROM rs_claim c
                   JOIN rs_entity e ON e.entity_id = c.subject_id AND e.status = 'active'
                   LEFT JOIN rs_entity cat ON cat.entity_id = c.object_entity_id
                   WHERE c.tenant_id = $1 AND c.predicate = $3
                     AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                     AND EXISTS (            -- grounding+GC: only claims with ACTIVE evidence
                         SELECT 1 FROM rs_claim_evidence ev
                         WHERE ev.claim_id = c.claim_id
                           AND ev.evidence_status = 'active')
                   GROUP BY c.object_norm
                   HAVING count(DISTINCT c.subject_id) >= $2
                   ORDER BY members DESC, name, object_norm""",
                tenant_id, min_members, self._placement_predicate)
        return [{"object_norm": r["object_norm"], "name": r["name"],
                 "members": int(r["members"])} for r in rows]

    async def population_claims(self, *, tenant_id: str = "demo",
                               category_norms: Sequence[str] | None = None,
                               company_cap: int = 400,
                               claims_per_company_cap: int = 40,
                               predicates: Sequence[str] | None = None) -> dict:
        """Grounded population read the market map is built from: SUBJECT rows
        (`self._subject_kind`, tech 'company'), each with its ACTIVE grounded claims —
        restricted to `predicates` when given, else ALL active grounded claims — every
        claim carrying its winning (highest-authority active) evidence.

        Scope: subjects with ≥1 active grounded placement-predicate
        (`self._placement_predicate`) claim whose `object_norm` ∈ `category_norms` (when
        given); otherwise the whole grounded subject population (any active grounded
        claim). Companies are capped at
        `company_cap` (ordered by name) and each company's claims at
        `claims_per_company_cap`. NO silent truncation — the returned `meta` carries
        `companies_truncated` / `claims_truncated` (+ the clipped company ids) so a
        clip is always surfaced.

        Grounding-exclusion: an INNER lateral join on ACTIVE evidence means a claim
        with no active evidence row is never returned (same rule as `population`).

        Returns `{"companies": [...], "meta": {...}}` where each company is
        `{entity_id, name, kind, primary_domain, claims:[{predicate, object_kind,
        object_value, object_entity_id, object_norm, confidence,
        evidence:{document_id, block_id, quote, authority_tier}}]}`."""
        await self.ensure_schema()
        # Vocabulary is INJECTED (no vertical import): the subject kind, the placement
        # predicate that defines category scope, and the optional per-claim predicate set.
        subject_kind = self._subject_kind
        placement = self._placement_predicate
        pred_names = list(predicates) if predicates else None

        norms = list(category_norms) if category_norms else None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # 1) The subject set — active subjects grounded into scope. Fetch cap+1
            #    so a clip is DETECTABLE (no separate count query, no silent drop).
            if norms is not None:
                comp_rows = await conn.fetch(
                    """SELECT e.entity_id, e.name, e.kind, e.primary_domain
                        FROM rs_entity e
                        WHERE e.tenant_id = $1 AND e.status = 'active' AND e.kind = $4
                          AND EXISTS (
                              SELECT 1 FROM rs_claim c
                              WHERE c.tenant_id = $1 AND c.subject_id = e.entity_id
                                AND c.predicate = $5
                                AND c.object_norm = ANY($2::text[])
                                AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                                AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                            WHERE ev.claim_id = c.claim_id
                                              AND ev.evidence_status = 'active'))
                        ORDER BY e.name, e.entity_id
                        LIMIT $3""",
                    tenant_id, norms, company_cap + 1, subject_kind, placement)
            else:
                comp_rows = await conn.fetch(
                    """SELECT e.entity_id, e.name, e.kind, e.primary_domain
                       FROM rs_entity e
                       WHERE e.tenant_id = $1 AND e.status = 'active' AND e.kind = $3
                         AND EXISTS (
                             SELECT 1 FROM rs_claim c
                             WHERE c.tenant_id = $1 AND c.subject_id = e.entity_id
                               AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                               AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                           WHERE ev.claim_id = c.claim_id
                                             AND ev.evidence_status = 'active'))
                       ORDER BY e.name, e.entity_id
                       LIMIT $2""",
                    tenant_id, company_cap + 1, subject_kind)

            companies_truncated = len(comp_rows) > company_cap
            comp_rows = comp_rows[:company_cap]
            company_ids = [r["entity_id"] for r in comp_rows]

            companies: list[dict] = [{
                "entity_id": r["entity_id"], "name": r["name"], "kind": r["kind"],
                "primary_domain": r["primary_domain"], "claims": [],
            } for r in comp_rows]
            by_id = {c["entity_id"]: c for c in companies}

            claim_rows: list = []
            if company_ids:
                # 2) One query for their claims + winning evidence. INNER lateral join =>
                #    grounding-exclusion; window ranks + caps per subject and exposes the
                #    pre-cap total so a per-subject clip is reportable. The per-claim
                #    predicate filter is applied ONLY when `predicates` was given (else all
                #    active grounded claims are returned — vocabulary-neutral default).
                claim_args: list[Any] = [tenant_id, company_ids]   # $1, $2
                pred_clause = ""
                if pred_names:
                    claim_args.append(pred_names)
                    pred_clause = f"AND c.predicate = ANY(${len(claim_args)}::text[])"
                claim_args.append(claims_per_company_cap)
                cap_param = f"${len(claim_args)}"
                claim_rows = await conn.fetch(
                    f"""SELECT t.entity_id, t.predicate, t.object_kind, t.object_value,
                              t.object_norm, t.object_entity_id, t.confidence,
                              t.document_id, t.block_id, t.quote, t.authority_tier,
                              t.total_claims
                       FROM (
                           SELECT c.subject_id AS entity_id, c.predicate,
                                  c.object_kind, c.object_value, c.object_norm,
                                  c.object_entity_id, c.confidence,
                                  ev.document_id, ev.block_id, ev.quote, ev.authority_tier,
                                  row_number() OVER (
                                      PARTITION BY c.subject_id
                                      ORDER BY c.predicate, c.object_norm, c.claim_id) AS rn,
                                  count(*) OVER (PARTITION BY c.subject_id) AS total_claims
                           FROM rs_claim c
                           JOIN LATERAL (
                               SELECT document_id, block_id, quote, authority_tier
                               FROM rs_claim_evidence ev
                               WHERE ev.claim_id = c.claim_id
                                 AND ev.evidence_status = 'active'
                               ORDER BY ev.authority_tier DESC, ev.retrieved_at
                               LIMIT 1
                           ) ev ON true
                           WHERE c.tenant_id = $1 AND c.subject_id = ANY($2::text[])
                             {pred_clause}
                             AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                       ) t
                       WHERE t.rn <= {cap_param}
                       ORDER BY t.entity_id, t.predicate, t.object_norm""",
                    *claim_args)

        clipped_company_ids: list[str] = []
        for r in claim_rows:
            comp = by_id.get(r["entity_id"])
            if comp is None:
                continue
            comp["claims"].append({
                "predicate": r["predicate"],
                "object_kind": r["object_kind"],
                "object_value": r["object_value"],
                "object_entity_id": r["object_entity_id"],
                "object_norm": r["object_norm"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else 0.0,
                "evidence": {
                    "document_id": r["document_id"],
                    "block_id": r["block_id"],
                    "quote": r["quote"],
                    "authority_tier": r["authority_tier"],
                },
            })
            if r["total_claims"] > claims_per_company_cap:
                if r["entity_id"] not in clipped_company_ids:
                    clipped_company_ids.append(r["entity_id"])

        meta = {
            "company_count": len(companies),
            "companies_truncated": companies_truncated,
            "company_cap": company_cap,
            "claims_truncated": bool(clipped_company_ids),
            "claims_per_company_cap": claims_per_company_cap,
            "clipped_company_ids": clipped_company_ids,
        }
        return {"companies": companies, "meta": meta}

    # ---- CROSSVIEWS reads (Task CV1 — grid data layer) --------------------- #
    async def predicate_coverage(self, *, subject_kind: str | None = None,
                                 category_norm: str | None = None,
                                 tenant_id: str = "demo") -> list[dict]:
        """Coverage read — "which columns have data + how many rows each fills".
        For active subjects of `subject_kind` (default `self._subject_kind`), returns
        `[{predicate, rows_covered}]` where `rows_covered = count(DISTINCT subject_id)`
        of that predicate's ACTIVE-evidence claims, ordered by rows_covered DESC
        (predicate tie-break). Optional `category_norm` restricts to subjects with an
        active grounded `self._placement_predicate`=norm placement claim.

        Same grounding-exclusion discipline as `distinct_categories`: a claim is
        counted only when it has an ACTIVE evidence row (a GC'd/stale span never
        inflates coverage). Vocabulary-neutral — `subject_kind`/`placement_predicate`
        come from the instance, never a vertical import."""
        await self.ensure_schema()
        kind = subject_kind or self._subject_kind
        args: list[Any] = [tenant_id, kind]          # $1, $2
        placement_clause = ""
        if category_norm:
            args.append(self._placement_predicate)   # $3
            args.append(category_norm)                # $4
            placement_clause = """
                     AND EXISTS (
                         SELECT 1 FROM rs_claim pc
                         WHERE pc.tenant_id = $1 AND pc.subject_id = c.subject_id
                           AND pc.predicate = $3 AND pc.object_norm = $4
                           AND pc.retracted_at IS NULL AND pc.superseded_at IS NULL
                           AND EXISTS (SELECT 1 FROM rs_claim_evidence ev2
                                       WHERE ev2.claim_id = pc.claim_id
                                         AND ev2.evidence_status = 'active'))"""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT c.predicate AS predicate,
                          count(DISTINCT c.subject_id) AS rows_covered
                   FROM rs_claim c
                   JOIN rs_entity e ON e.entity_id = c.subject_id
                        AND e.status = 'active' AND e.kind = $2
                   WHERE c.tenant_id = $1
                     AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                     AND EXISTS (         -- grounding+GC: only claims with ACTIVE evidence
                         SELECT 1 FROM rs_claim_evidence ev
                         WHERE ev.claim_id = c.claim_id
                           AND ev.evidence_status = 'active')
                     {placement_clause}
                   GROUP BY c.predicate
                   ORDER BY rows_covered DESC, c.predicate""",
                *args)
        return [{"predicate": r["predicate"], "rows_covered": int(r["rows_covered"])}
                for r in rows]

    async def founder_rows(self, *, founder_predicate: str = "has_founder",
                           tenant_id: str = "demo", cap: int = 400) -> list[dict]:
        """The REVERSE edge (founders are OBJECTS of the founder predicate): person
        entities that are the `object_entity_id` of an active-evidence
        `founder_predicate` claim, each with the companies they founded. Returns
        `[{person_id, name, companies:[{company_id, company_name, quote, document_id,
        block_id, authority_tier}]}]`, persons ordered by name (capped at `cap`).

        `founder_predicate` is a PARAMETER (default 'has_founder'), never hardcoded in
        the store SQL — so the store stays vocabulary-neutral (a second vertical passes
        its own reverse-edge predicate). Grounding-exclusion: an INNER lateral join on
        ACTIVE evidence means a founder edge with no active span never surfaces, and the
        per-company citation is that claim's winning (highest-authority) evidence."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.object_entity_id AS person_id,
                          COALESCE(NULLIF(p.name, ''), c.object_entity_id) AS person_name,
                          c.subject_id AS company_id,
                          COALESCE(NULLIF(comp.name, ''), c.subject_id) AS company_name,
                          ev.document_id, ev.block_id, ev.quote, ev.authority_tier
                   FROM rs_claim c
                   JOIN rs_entity comp ON comp.entity_id = c.subject_id
                        AND comp.status = 'active'
                   LEFT JOIN rs_entity p ON p.entity_id = c.object_entity_id
                   JOIN LATERAL (          -- INNER => grounding-exclusion (active evidence only)
                       SELECT document_id, block_id, quote, authority_tier
                       FROM rs_claim_evidence ev
                       WHERE ev.claim_id = c.claim_id AND ev.evidence_status = 'active'
                       ORDER BY ev.authority_tier DESC, ev.retrieved_at
                       LIMIT 1
                   ) ev ON true
                   WHERE c.tenant_id = $1 AND c.predicate = $2
                     AND c.object_kind = 'entity' AND c.object_entity_id <> ''
                     AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                   ORDER BY person_name, c.object_entity_id, company_name, c.subject_id""",
                tenant_id, founder_predicate)
        by_person: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            pid = r["person_id"]
            person = by_person.get(pid)
            if person is None:
                if len(order) >= cap:
                    continue          # person cap reached — deterministic name order
                person = {"person_id": pid, "name": r["person_name"], "companies": []}
                by_person[pid] = person
                order.append(pid)
            person["companies"].append({
                "company_id": r["company_id"],
                "company_name": r["company_name"],
                "quote": r["quote"],
                "document_id": r["document_id"],
                "block_id": r["block_id"],
                "authority_tier": r["authority_tier"],
            })
        return [by_person[pid] for pid in order]

    async def neighbors(self, entity_id: str, *, tenant_id: str = "demo",
                        relations: tuple[str, ...] | None = None,
                        cap: int = 400) -> list[dict]:
        """Slice-1 edge model: the BIDIRECTIONAL 1-hop grounded edges INCIDENT to `entity_id`
        (as subject OR object of an active-evidence entity-edge claim). The pathfinder
        (`api.graph_path.find_paths`) traverses these; the store only reads them.

        Returns `[{subject_id, predicate, object_id, claim_id, citation:{document_id, block_id,
        quote, authority_tier}}]`. `relations` is an OPTIONAL predicate allowlist (per-intent
        scoping) — never hardcoded here, so the store stays vocabulary-neutral. Grounding-exclusion:
        an INNER lateral join on ACTIVE evidence means an edge with no live span never surfaces, and
        the citation is that claim's winning (highest-authority) evidence. Capped at `cap` edges."""
        await self.ensure_schema()
        pool = await self._get_pool()
        rel_list = list(relations) if relations else None
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.subject_id, c.predicate, c.object_entity_id, c.claim_id,
                          ev.document_id, ev.block_id, ev.quote, ev.authority_tier
                   FROM rs_claim c
                   JOIN LATERAL (          -- INNER => grounding-exclusion (active evidence only)
                       SELECT document_id, block_id, quote, authority_tier
                       FROM rs_claim_evidence ev
                       WHERE ev.claim_id = c.claim_id AND ev.evidence_status = 'active'
                       ORDER BY ev.authority_tier DESC, ev.retrieved_at
                       LIMIT 1
                   ) ev ON true
                   WHERE c.tenant_id = $1
                     AND c.object_kind = 'entity' AND c.object_entity_id <> ''
                     AND (c.subject_id = $2 OR c.object_entity_id = $2)
                     AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                     AND ($3::text[] IS NULL OR c.predicate = ANY($3))
                   ORDER BY ev.authority_tier DESC
                   LIMIT $4""",
                tenant_id, entity_id, rel_list, cap)
        return [{
            "subject_id": r["subject_id"], "predicate": r["predicate"],
            "object_id": r["object_entity_id"], "claim_id": r["claim_id"],
            "citation": {"document_id": r["document_id"], "block_id": r["block_id"],
                         "quote": r["quote"], "authority_tier": r["authority_tier"]},
        } for r in rows]

    async def find_entity(self, ref: str, *, tenant_id: str = "demo") -> dict | None:
        """Resolve an entity REFERENCE (an `entity_id`, or a name) to `{entity_id, name, kind}` for
        the Slice-1 connection endpoints — exact id/name first, then a best-effort name contains.
        Slice 1 only: real cross-source identity resolution is Slice 2 (strong-id ER). Active only."""
        ref = (ref or "").strip()
        if not ref:
            return None
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                """SELECT entity_id, name, kind FROM rs_entity
                   WHERE tenant_id = $1 AND status = 'active'
                     AND (entity_id = $2 OR lower(name) = lower($2))
                   ORDER BY (entity_id = $2) DESC LIMIT 1""",
                tenant_id, ref)
            if r is None:
                r = await conn.fetchrow(
                    """SELECT entity_id, name, kind FROM rs_entity
                       WHERE tenant_id = $1 AND status = 'active' AND name ILIKE $2
                       ORDER BY length(name) LIMIT 1""",
                    tenant_id, f"%{ref}%")
        return dict(r) if r else None

    async def add_person_facet(self, *, entity_id: str, facet_key: str, facet_value_norm: str,
                               display_value: str = "", source_claim_id: str = "",
                               source_document_id: str = "", source_block_id: str = "",
                               confidence: float = 0.0, valid_as_of=None,
                               tenant_id: str = "demo") -> None:
        """Write ONE grounded facet row (people-population read-model). Each facet carries the source
        claim + evidence span it was derived from, so a facet-filtered enumeration stays grounded.
        Upsert (idempotent per (tenant,entity,facet_key,value))."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO roster_entity_facet (tenant_id, entity_id, facet_key, facet_value_norm,
                     display_value, source_claim_id, source_document_id, source_block_id, confidence,
                     valid_as_of)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                   ON CONFLICT (tenant_id, entity_id, facet_key, facet_value_norm) DO UPDATE SET
                     display_value = EXCLUDED.display_value, source_claim_id = EXCLUDED.source_claim_id,
                     source_document_id = EXCLUDED.source_document_id,
                     source_block_id = EXCLUDED.source_block_id, confidence = EXCLUDED.confidence""",
                tenant_id, entity_id, facet_key, facet_value_norm, display_value, source_claim_id,
                source_document_id, source_block_id, float(confidence), valid_as_of)

    async def enumerate_by_facets(self, facets: dict[str, list[str]], *, tenant_id: str = "demo",
                                  cap: int = 200) -> list[dict]:
        """The people-enumeration core (LLM-as-query-COMPILER; code owns the filter). `facets` maps a
        facet_key → list of acceptable normalized values (OR within a key). A person matches when, for
        EVERY facet_key, it has >= 1 matching facet value (AND across keys). Returns the matched persons
        with the display + a grounding citation for each queried facet:
        `[{entity_id, name, facets:[{facet_key, display_value, document_id, block_id, source_claim_id}]}]`
        (name order, capped). Empty `facets` → [] (no blanket population dump)."""
        keys = [k for k, v in facets.items() if v]
        if not keys:
            return []
        await self.ensure_schema()
        pool = await self._get_pool()
        # Build the per-key OR predicate: (facet_key=$k AND facet_value_norm = ANY($vals)).
        clauses, args = [], [tenant_id]
        for k in keys:
            args.append(k); ki = len(args)
            args.append(list(facets[k])); vi = len(args)
            clauses.append(f"(facet_key = ${ki} AND facet_value_norm = ANY(${vi}))")
        args.append(len(keys)); nkeys_i = len(args)
        args.append(cap); cap_i = len(args)
        async with pool.acquire() as conn:
            # 1) entity_ids matching ALL keys (INTERSECT via count of DISTINCT matched keys == n_keys).
            ids = await conn.fetch(
                f"""SELECT f.entity_id
                    FROM roster_entity_facet f
                    JOIN rs_entity e ON e.entity_id = f.entity_id AND e.status = 'active'
                    WHERE f.tenant_id = $1 AND ({' OR '.join(clauses)})
                    GROUP BY f.entity_id
                    HAVING count(DISTINCT f.facet_key) = ${nkeys_i}
                    ORDER BY f.entity_id
                    LIMIT ${cap_i}""", *args)
            matched = [r["entity_id"] for r in ids]
            if not matched:
                return []
            # 2) ALL facets (with citation) for each matched person + the person name. We return every
            #    facet (not just the queried keys) so DISPLAY facets — title, and `link_*` profile links
            #    (linkedin/x/website/…) enriched from the source — reach the answer card, each grounded.
            rows = await conn.fetch(
                """SELECT f.entity_id, COALESCE(NULLIF(e.name,''), f.entity_id) AS name,
                          f.facet_key, f.display_value, f.facet_value_norm,
                          f.source_document_id, f.source_block_id, f.source_claim_id
                   FROM roster_entity_facet f
                   JOIN rs_entity e ON e.entity_id = f.entity_id
                   WHERE f.tenant_id = $1 AND f.entity_id = ANY($2)
                   ORDER BY name, f.facet_key""",
                tenant_id, matched)
        by_ent: dict[str, dict] = {}
        order: list[str] = []
        for r in rows:
            eid = r["entity_id"]
            p = by_ent.get(eid)
            if p is None:
                p = {"entity_id": eid, "name": r["name"], "facets": []}
                by_ent[eid] = p; order.append(eid)
            p["facets"].append({
                "facet_key": r["facet_key"], "display_value": r["display_value"],
                "value_norm": r["facet_value_norm"], "document_id": r["source_document_id"],
                "block_id": r["source_block_id"], "source_claim_id": r["source_claim_id"]})
        return [by_ent[e] for e in order]

    async def people_index_stats(self, *, tenant_id: str = "demo") -> dict:
        """Coverage facts for the honest coverage_basis: how many persons the index holds, the distinct
        source documents behind the facets, and per-facet-key coverage counts. Never implies the index
        is the whole world — it reports exactly what has been ingested."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(
                "SELECT count(DISTINCT entity_id) FROM roster_entity_facet WHERE tenant_id=$1", tenant_id)
            nsrc = await conn.fetchval(
                "SELECT count(DISTINCT source_document_id) FROM roster_entity_facet "
                "WHERE tenant_id=$1 AND source_document_id <> ''", tenant_id)
            per_key = await conn.fetch(
                "SELECT facet_key, count(DISTINCT entity_id) AS n FROM roster_entity_facet "
                "WHERE tenant_id=$1 GROUP BY facet_key ORDER BY facet_key", tenant_id)
        return {"persons_indexed": int(total or 0), "source_documents": int(nsrc or 0),
                "facet_coverage": {r["facet_key"]: int(r["n"]) for r in per_key}}

    async def search_jobs(self, *, terms=None, company=None, location=None, cap: int = 60) -> list[dict]:
        """Search rs_job (public ATS postings — Greenhouse/Ashby/Lever) by title keywords / company /
        location; each row carries an apply URL. company/location are AND filters; the title terms must
        ALL appear (precision). Returns [] if rs_job doesn't exist yet (jobs ingest not run)."""
        pool = await self._get_pool()
        conds, args, i = [], [], 1
        if company:
            # normalize BOTH sides — rs_job.company is stored as a display name ("Stripe") but the
            # query compiler emits canonical slugs ("stripe"); compare on lower(replace(' ','_')).
            conds.append(f"lower(replace(company,' ','_')) = ANY(${i})")
            args.append([str(c).lower().replace(" ", "_") for c in company]); i += 1
        if location:
            conds.append(f"lower(location) ILIKE ${i}"); args.append(f"%{str(location).lower()}%"); i += 1
        for t in (terms or [])[:4]:
            t = str(t).strip().lower()
            if t:
                conds.append(f"title_norm ILIKE ${i}"); args.append(f"%{t}%"); i += 1
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        # updated_at rides as a date STRING so rows stay JSON-serializable everywhere (sessions,
        # /jobs, Q&A) — the freshness disclosure ("postings as of …") depends on it.
        sql = (f"SELECT company,title,location,department,url,source,"
               f"to_char(updated_at,'YYYY-MM-DD') AS updated_at FROM rs_job{where} "
               f"ORDER BY updated_at DESC LIMIT {int(cap)}")
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
        except Exception:
            return []
        return [dict(r) for r in rows]

    async def jobs_for_companies(self, norm_companies: list[str], *, terms=None, cap: int = 600) -> list[dict]:
        """Jobs at a SET of companies keyed by the alphanumeric-only company key (the same key the
        F500 / startup sets use), optionally narrowed by title terms. Newest first."""
        pool = await self._get_pool()
        conds, args, i = ["regexp_replace(lower(company),'[^a-z0-9]','','g') = ANY($1)"], [list(norm_companies)], 2
        for t in (terms or [])[:4]:
            t = str(t).strip().lower()
            if t:
                conds.append(f"title_norm ILIKE ${i}"); args.append(f"%{t}%"); i += 1
        sql = (f"SELECT id,company,title,location,department,url,source,to_char(updated_at,'YYYY-MM-DD') AS updated_at "
               f"FROM rs_job WHERE {' AND '.join(conds)} ORDER BY updated_at DESC LIMIT {int(cap)}")
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
        except Exception:
            return []
        return [dict(r) for r in rows]

    async def people_coverage(self) -> dict:
        """A coverage summary for the Coverage UI: total people, breakdown by SOURCE (github/openalex/
        npi/yc/sec/theorg/aifund/ef), and how many people each searchable DIMENSION covers. Honest —
        exactly what has been ingested."""
        pool = await self._get_pool()
        SRC = [("GitHub engineers", "github:%"), ("OpenAlex researchers", "openalex:%"),
               ("NPI clinicians", "npi:%"), ("YC founders", "yc:%"), ("SEC execs", "sec:%"),
               ("The Org (business)", "theorg:%"), ("AI Fund founders", "aifund:%"),
               ("Entrepreneur First", "ef:%"), ("Pear VC", "pear:%"), ("SOSV", "sosv:%")]
        DIMS = ["role", "seniority", "function", "skill", "industry", "stage", "accelerator",
                "company", "worked_at", "metro", "country", "link_linkedin"]
        try:
            async with pool.acquire() as conn:
                total = await conn.fetchval("SELECT count(*) FROM rs_entity WHERE kind='person'")
                sources = []
                for label, pat in SRC:
                    n = await conn.fetchval("SELECT count(*) FROM rs_entity WHERE entity_id LIKE $1", pat)
                    if n:
                        sources.append({"label": label, "count": int(n)})
                dims = []
                for k in DIMS:
                    n = await conn.fetchval(
                        "SELECT count(DISTINCT entity_id) FROM roster_entity_facet WHERE facet_key=$1", k)
                    dims.append({"key": k, "people": int(n or 0)})
                distinct_co = await conn.fetchval(
                    "SELECT count(DISTINCT facet_value_norm) FROM roster_entity_facet WHERE facet_key='company'")
                try:
                    jobs = await conn.fetchval("SELECT count(*) FROM rs_job")
                    jobco = await conn.fetchval("SELECT count(DISTINCT company) FROM rs_job")
                except Exception:
                    jobs, jobco = 0, 0
        except Exception:
            return {"total": 0, "sources": [], "dimensions": [], "distinct_companies": 0, "jobs": 0, "job_companies": 0}
        return {"total": int(total or 0), "sources": sources, "dimensions": dims,
                "distinct_companies": int(distinct_co or 0),
                "jobs": int(jobs or 0), "job_companies": int(jobco or 0)}

    async def people_by_ids(self, entity_ids, *, tenant_id: str = "demo") -> list[dict]:
        """Fetch people (with ALL their facets) for a given ORDERED list of entity_ids, preserving that
        order — the same row shape enumerate_by_facets returns, so the pure-semantic path renders cards
        identically. Used when semantic search supplies the ranking (no facet-narrowed candidate set)."""
        if not entity_ids:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT f.entity_id, COALESCE(NULLIF(e.name,''), f.entity_id) AS name,
                          f.facet_key, f.display_value, f.facet_value_norm,
                          f.source_document_id, f.source_block_id, f.source_claim_id
                   FROM roster_entity_facet f JOIN rs_entity e ON e.entity_id = f.entity_id
                   WHERE f.tenant_id = $1 AND f.entity_id = ANY($2)""", tenant_id, list(entity_ids))
        by_ent: dict[str, dict] = {}
        for r in rows:
            p = by_ent.get(r["entity_id"])
            if p is None:
                p = {"entity_id": r["entity_id"], "name": r["name"], "facets": []}
                by_ent[r["entity_id"]] = p
            p["facets"].append({
                "facet_key": r["facet_key"], "display_value": r["display_value"],
                "value_norm": r["facet_value_norm"], "document_id": r["source_document_id"],
                "block_id": r["source_block_id"], "source_claim_id": r["source_claim_id"]})
        return [by_ent[e] for e in entity_ids if e in by_ent]   # preserve semantic rank order

    async def similarity_for(self, qvec: str, entity_ids) -> dict[str, float]:
        """Cosine similarity of the query embedding to SPECIFIC people (topic-anchored candidates
        outside the global top-N) — so they can be ranked on the same scale as everyone else."""
        ids = [e for e in (entity_ids or []) if e]
        if not ids:
            return {}
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT entity_id, 1 - (embedding <=> $1::vector) AS sim FROM rs_person_vec "
                    "WHERE entity_id = ANY($2)", qvec, ids)
        except Exception:
            return {}
        return {r["entity_id"]: float(r["sim"]) for r in rows}

    async def people_by_geo(self, *, metro: str = "", state: str = "", tenant_id: str = "demo",
                            limit: int = 4000) -> list[str]:
        """Entity ids CONFIRMED in a metro (canonical + aliases) or a US state — the local-scope
        recall path, so the default view leads with people we can actually place there."""
        from roster_vertical.people_facets import METRO_ALIAS, US_METROS
        vals: list[str] = []
        if metro:
            vals = [metro] + [a for a, canon in METRO_ALIAS.items() if canon == metro]
        conds, args = [], [tenant_id]
        if vals:
            args.append(vals); conds.append(f"(facet_key = 'metro' AND facet_value_norm = ANY(${len(args)}))")
        if state:
            args.append(state.lower()); conds.append(f"(facet_key = 'state' AND facet_value_norm = ${len(args)})")
            in_state = [k for k, m in US_METROS.items() if m["state"] == state.lower()]
            in_state += [a for a, canon in METRO_ALIAS.items() if canon in in_state]
            if in_state:
                args.append(in_state); conds.append(f"(facet_key = 'metro' AND facet_value_norm = ANY(${len(args)}))")
        if not conds:
            return []
        args.append(int(limit))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT DISTINCT entity_id FROM roster_entity_facet WHERE tenant_id = $1 AND ({' OR '.join(conds)}) "
                f"LIMIT ${len(args)}", *args)
        return [r["entity_id"] for r in rows]

    async def people_with_artifacts(self, kinds: list[str], *, limit: int = 4000) -> list[str]:
        """Entity ids holding at least one linked artifact of any of `kinds` (paper/repo/post/talk/
        patent/org) — the recall path for the evidence filter."""
        ks = [str(k) for k in (kinds or []) if str(k)]
        if not ks:
            return []
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT entity_id FROM rs_person_artifact WHERE kind = ANY($1) LIMIT $2", ks, int(limit))
        except Exception:
            return []
        return [r["entity_id"] for r in rows]

    async def people_by_text(self, terms: list[str], *, tenant_id: str = "demo", limit: int = 300) -> list[str]:
        """Entity ids whose facet DISPLAY text (bio/title, skills, function, industry, role, company)
        mentions any of `terms` (ILIKE). The topic-anchor recall path for sparse subjects."""
        pats = ["%" + " ".join(str(t).split()) + "%" for t in (terms or []) if str(t).strip()][:8]
        if not pats:
            return []
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT entity_id, count(*) AS n FROM roster_entity_facet
                   WHERE tenant_id = $1 AND facet_key IN ('title','skill','function','industry','role','company','worked_at')
                     AND display_value ILIKE ANY($2::text[])
                   GROUP BY entity_id ORDER BY n DESC LIMIT $3""", tenant_id, pats, int(limit))
        return [r["entity_id"] for r in rows]

    async def people_by_name(self, name: str, *, tenant_id: str = "demo", limit: int = 12) -> list[dict]:
        """People whose NAME matches (person lookup): exact case-insensitive matches first, then
        token-ordered contains ('mukul gupta' → '%mukul%gupta%') to catch middle names/initials.
        Same row shape as people_by_ids. Never merges — every same-named person is a separate row."""
        name = " ".join((name or "").split())
        if not name:
            return []
        toks = [t for t in re.split(r"\s+", name.lower()) if t]
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            exact = await conn.fetch(
                """SELECT entity_id FROM rs_entity WHERE tenant_id = $1 AND kind = 'person'
                   AND status = 'active' AND lower(name) = lower($2) ORDER BY retrieved_at DESC LIMIT $3""",
                tenant_id, name, int(limit))
            ids = [r["entity_id"] for r in exact]
            if len(ids) < limit and len(toks) >= 2:
                # whole-word tokens in order: 'tom brown' matches 'Tom B. Brown', not 'Tomer Brown'
                pat = ".*".join(r"\m" + re.escape(t) + r"\M" for t in toks)
                loose = await conn.fetch(
                    """SELECT entity_id FROM rs_entity WHERE tenant_id = $1 AND kind = 'person'
                       AND status = 'active' AND lower(name) LIKE $2 AND lower(name) ~ $3
                       AND lower(name) <> lower($4)
                       ORDER BY length(name), retrieved_at DESC LIMIT $5""",
                    tenant_id, "%" + "%".join(toks) + "%", pat, name, int(limit) - len(ids))
                ids += [r["entity_id"] for r in loose]
        return await self.people_by_ids(ids, tenant_id=tenant_id) if ids else []

    # ---- LinkedIn snippet-scan memory (a person is queried once; outcomes remembered) ----
    _LI_DDL = """CREATE TABLE IF NOT EXISTS rs_linkedin_scan (
        entity_id text PRIMARY KEY, status text NOT NULL, url text NOT NULL DEFAULT '',
        headline text NOT NULL DEFAULT '', scanned_at timestamptz NOT NULL DEFAULT now())"""
    _li_ready = False

    async def _li_ensure(self, conn) -> None:
        if not self._li_ready:
            await conn.execute(self._LI_DDL)
            self._li_ready = True

    async def linkedin_scans(self, entity_ids) -> dict[str, dict]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._li_ensure(conn)
            rows = await conn.fetch(
                "SELECT entity_id, status, url, headline FROM rs_linkedin_scan WHERE entity_id = ANY($1)",
                list(entity_ids))
        return {r["entity_id"]: dict(r) for r in rows}

    async def linkedin_scans_today(self) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._li_ensure(conn)
            return int(await conn.fetchval(
                "SELECT count(*) FROM rs_linkedin_scan WHERE scanned_at > now() - interval '24 hours' "
                "AND status <> 'already'") or 0)

    async def record_linkedin_scan(self, entity_id: str, status: str, *, url: str = "",
                                   headline: str = "") -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await self._li_ensure(conn)
            await conn.execute(
                """INSERT INTO rs_linkedin_scan (entity_id, status, url, headline, scanned_at)
                   VALUES ($1,$2,$3,$4, now())
                   ON CONFLICT (entity_id) DO UPDATE SET status=EXCLUDED.status, url=EXCLUDED.url,
                     headline=EXCLUDED.headline, scanned_at=now()""",
                entity_id, status, (url or "")[:500], (headline or "")[:300])

    async def semantic_people(self, qvec: str, *, candidate_ids=None, cap: int = 200) -> list[str]:
        """Rank people by cosine similarity to the query embedding. If candidate_ids is given (the
        facet-filtered set), rank WITHIN it (hybrid: exact filter + semantic order); else pure-semantic
        over everyone (a 'vibe' query). Returns entity_ids best-first. [] if no vectors yet."""
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(f"SET LOCAL hnsw.ef_search = {_ef_search(cap)}")  # HNSW cap fix
                    if candidate_ids:
                        rows = await conn.fetch(
                            "SELECT entity_id FROM rs_person_vec WHERE entity_id = ANY($1) "
                            "ORDER BY embedding <=> $2::vector LIMIT $3", list(candidate_ids), qvec, int(cap))
                    else:
                        rows = await conn.fetch(
                            "SELECT entity_id FROM rs_person_vec ORDER BY embedding <=> $1::vector LIMIT $2",
                            qvec, int(cap))
        except Exception:
            return []
        return [r["entity_id"] for r in rows]

    async def semantic_jobs(self, qvec: str, *, company=None, cap: int = 80) -> list[dict]:
        """Rank jobs by cosine similarity to the query embedding (optionally within a company set)."""
        pool = await self._get_pool()
        conds, args, i = ["embedding IS NOT NULL"], [], 1
        if company:
            conds.append(f"lower(replace(company,' ','_')) = ANY(${i})"); args.append([str(c).lower().replace(' ', '_') for c in company]); i += 1
        args.append(qvec); qi = i; i += 1
        args.append(int(cap))
        sql = (f"SELECT company,title,location,department,url,source,"
               f"to_char(updated_at,'YYYY-MM-DD') AS updated_at FROM rs_job "
               f"WHERE {' AND '.join(conds)} ORDER BY embedding <=> ${qi}::vector LIMIT ${i}")
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)
        except Exception:
            return []
        return [dict(r) for r in rows]

    async def jobs_local(self, *, location_regex: str, qvec: str | None = None, terms=None,
                         cap: int = 150) -> list[dict]:
        """Roles whose LOCATION text matches the scope regex (the local-recall path): ranked by
        similarity to `qvec` when given (with sim), else newest first. Optional title terms narrow."""
        if not location_regex:
            return []
        pool = await self._get_pool()
        conds, args = ["location ~* $1"], [location_regex]
        for t in (terms or [])[:6]:
            t = str(t).strip().lower()
            if t:
                args.append("%" + t + "%"); conds.append(f"title_norm ILIKE ${len(args)}")
        if qvec:
            args.append(qvec)
            sql = (f"SELECT id, company, title, location, department, url, source, "
                   f"1 - (embedding <=> ${len(args)}::vector) AS sim FROM rs_job "
                   f"WHERE embedding IS NOT NULL AND {' AND '.join(conds)} "
                   f"ORDER BY embedding <=> ${len(args)}::vector LIMIT {int(cap)}")
        else:
            sql = (f"SELECT id, company, title, location, department, url, source, NULL::float AS sim "
                   f"FROM rs_job WHERE {' AND '.join(conds)} ORDER BY updated_at DESC LIMIT {int(cap)}")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL hnsw.ef_search = 200")
                    rows = await conn.fetch(sql, *args)
        except Exception:
            return []
        return [dict(r) for r in rows]

    async def match_jobs_scored(self, qvec: str, *, cap: int = 400) -> list[dict]:
        """Top `cap` jobs by cosine similarity to the résumé embedding, WITH the similarity score and
        id, so the caller can re-rank by user preferences (location/seniority/company-type/…)."""
        pool = await self._get_pool()
        sql = ("SELECT id, company, title, location, department, url, source, "
               "1 - (embedding <=> $1::vector) AS sim "
               "FROM rs_job WHERE embedding IS NOT NULL ORDER BY embedding <=> $1::vector LIMIT $2")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # HNSW's ef_search (default 40) caps how many candidates the index returns REGARDLESS
                    # of LIMIT — raise it so a large `cap` actually yields a real candidate pool. (int-only,
                    # so string interpolation is safe; SET LOCAL doesn't accept bound parameters.)
                    await conn.execute(f"SET LOCAL hnsw.ef_search = {_ef_search(cap)}")
                    rows = await conn.fetch(sql, qvec, int(cap))
        except Exception as e:  # noqa: BLE001
            _log.warning("match_jobs_scored failed (cap=%s): %s", cap, e)
            return []
        return [dict(r) for r in rows]

    async def match_people_scored(self, qvec: str, *, cap: int = 400) -> "list[dict]":
        """Top `cap` people by cosine similarity to a job-description embedding, WITH the sim score
        (for recruiter reverse-match: JD → ranked candidates). ef_search raised so cap is real."""
        pool = await self._get_pool()
        sql = ("SELECT entity_id, 1 - (embedding <=> $1::vector) AS sim "
               "FROM rs_person_vec ORDER BY embedding <=> $1::vector LIMIT $2")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(f"SET LOCAL hnsw.ef_search = {_ef_search(cap)}")
                    rows = await conn.fetch(sql, qvec, int(cap))
        except Exception:
            return []
        return [{"entity_id": r["entity_id"], "sim": float(r["sim"])} for r in rows]

    async def companies_with_facet(self, facet_keys: "tuple[str, ...]", values=None) -> set:
        """Set of company slugs that carry any of `facet_keys` (optionally restricted to `values`) on
        the people index — used to tag jobs as startup/accelerator-backed for match ranking."""
        pool = await self._get_pool()
        conds, args, i = ["facet_key = ANY($1)"], [list(facet_keys)], 2
        if values:
            conds.append(f"facet_value_norm = ANY(${i})"); args.append(list(values)); i += 1
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT DISTINCT facet_value_norm FROM roster_entity_facet WHERE facet_key='company' "
                    f"AND entity_id IN (SELECT entity_id FROM roster_entity_facet WHERE {' AND '.join(conds)})",
                    *args)
        except Exception:
            return set()
        return {r["facet_value_norm"] for r in rows if r["facet_value_norm"]}

    # ---- Analytics (Insights Q&A): code-owned GROUP-BY aggregation over the grounded index. The LLM
    # only picks group_by/filter keys from these ALLOWLISTS — never interpolated as SQL identifiers.
    _ANALYTICS_PEOPLE_KEYS = frozenset({"role", "seniority", "function", "industry", "metro", "company",
                                        "worked_at", "country", "state", "stage", "accelerator", "skill"})
    _ANALYTICS_JOB_COLS = frozenset({"company", "location", "source", "department"})

    async def aggregate_people_facets(self, *, group_by: str, filters: "dict[str, list[str]] | None" = None,
                                      top_n: int = 20, tenant_id: str = "demo") -> list[dict]:
        """Count DISTINCT people per `group_by` facet value, optionally within an AND-filter of other
        facets — the analytics core (tenant-scoped, ACTIVE people only, so takedowns are respected).
        Allowlisted keys only; parameterized values. Returns [{value, display, n}] desc. []=bad key/error."""
        if group_by not in self._ANALYTICS_PEOPLE_KEYS:
            return []
        filters = {k: v for k, v in (filters or {}).items()
                   if k in self._ANALYTICS_PEOPLE_KEYS and k != group_by and v}
        top_n = max(1, min(int(top_n or 20), 50))
        await self.ensure_schema()
        pool = await self._get_pool()
        args = [tenant_id, group_by]
        filt_sql = ""
        if filters:
            clauses = []
            for k, vals in filters.items():
                args.append(k); ki = len(args)
                args.append([str(x) for x in vals]); vi = len(args)
                clauses.append(f"(facet_key = ${ki} AND facet_value_norm = ANY(${vi}))")
            args.append(len(filters)); nk = len(args)
            filt_sql = (f" AND g.entity_id IN (SELECT entity_id FROM roster_entity_facet "
                        f"WHERE tenant_id=$1 AND ({' OR '.join(clauses)}) "
                        f"GROUP BY entity_id HAVING count(DISTINCT facet_key) = ${nk})")
        args.append(top_n); li = len(args)
        sql = (f"SELECT g.facet_value_norm AS value, max(g.display_value) AS display, "
               f"count(DISTINCT g.entity_id) AS n FROM roster_entity_facet g "
               f"JOIN rs_entity e ON e.entity_id = g.entity_id AND e.status='active' "
               f"WHERE g.tenant_id=$1 AND g.facet_key=$2{filt_sql} "
               f"GROUP BY g.facet_value_norm ORDER BY n DESC LIMIT ${li}")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '20s'")
                    rows = await conn.fetch(sql, *args)
        except Exception:   # noqa: BLE001 — bad query / timeout → empty (caller reports honestly)
            return []
        return [{"value": r["value"], "display": r["display"] or r["value"], "n": int(r["n"])} for r in rows]

    async def aggregate_jobs(self, *, group_by: str, filters: "dict | None" = None, top_n: int = 20) -> list[dict]:
        """Count jobs per `group_by` over rs_job (code-owned column allowlist). company is grouped on the
        NORMALIZED slug (lower+underscore) so display-name variants don't fragment counts. filters:
        company:list / location:str / terms:list (title ILIKE). Returns [{value, display, n}] desc."""
        if group_by not in self._ANALYTICS_JOB_COLS:
            return []
        top_n = max(1, min(int(top_n or 20), 50))
        val_expr = "lower(replace(company,' ','_'))" if group_by == "company" else f"lower({group_by})"
        pool = await self._get_pool()
        conds, args, i = ["title IS NOT NULL"], [], 1
        filters = filters or {}
        if filters.get("company"):
            conds.append(f"lower(replace(company,' ','_')) = ANY(${i})")
            args.append([str(c).lower().replace(" ", "_") for c in filters["company"]]); i += 1
        if filters.get("location"):
            conds.append(f"lower(location) ILIKE ${i}"); args.append(f"%{str(filters['location']).lower()}%"); i += 1
        for t in (filters.get("terms") or [])[:4]:
            t = str(t).strip().lower()
            if t:
                conds.append(f"title_norm ILIKE ${i}"); args.append(f"%{t}%"); i += 1
        args.append(top_n); li = i
        sql = (f"SELECT {val_expr} AS value, max({group_by}) AS display, count(*) AS n FROM rs_job "
               f"WHERE {' AND '.join(conds)} GROUP BY {val_expr} ORDER BY n DESC LIMIT ${li}")
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL statement_timeout = '20s'")
                    rows = await conn.fetch(sql, *args)
        except Exception:   # noqa: BLE001
            return []
        return [{"value": r["value"], "display": r["display"] or r["value"], "n": int(r["n"])} for r in rows]

    async def jobs_stats(self) -> dict:
        pool = await self._get_pool()
        try:
            async with pool.acquire() as conn:
                n = await conn.fetchval("SELECT count(*) FROM rs_job")
                co = await conn.fetchval("SELECT count(DISTINCT company) FROM rs_job")
        except Exception:
            return {"jobs": 0, "companies": 0}
        return {"jobs": int(n or 0), "companies": int(co or 0)}

    async def category_member_companies(self, *, category_norm: str,
                                        tenant_id: str = "demo",
                                        cap: int = 8) -> list[dict]:
        """Representative (exemplar) companies for a domain row: subjects of active
        grounded `self._placement_predicate`=`category_norm` claims. Returns
        `[{company_id, name, quote, document_id, block_id, authority_tier}]` (name
        order, capped at `cap`); the evidence is the placement claim's winning span so
        an aggregate cell that lists these companies can still cite each one.
        `distinct_categories` already gives the member COUNT — this gives the exemplars.

        Grounding-exclusion is identical to `distinct_categories`/`population_claims`
        (INNER lateral join on active evidence)."""
        await self.ensure_schema()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT c.subject_id AS company_id,
                          COALESCE(NULLIF(e.name, ''), c.subject_id) AS name,
                          ev.document_id, ev.block_id, ev.quote, ev.authority_tier
                   FROM rs_claim c
                   JOIN rs_entity e ON e.entity_id = c.subject_id
                        AND e.status = 'active' AND e.kind = $3
                   JOIN LATERAL (          -- INNER => grounding-exclusion
                       SELECT document_id, block_id, quote, authority_tier
                       FROM rs_claim_evidence ev
                       WHERE ev.claim_id = c.claim_id AND ev.evidence_status = 'active'
                       ORDER BY ev.authority_tier DESC, ev.retrieved_at
                       LIMIT 1
                   ) ev ON true
                   WHERE c.tenant_id = $1 AND c.predicate = $4 AND c.object_norm = $2
                     AND c.retracted_at IS NULL AND c.superseded_at IS NULL
                   ORDER BY name, c.subject_id
                   LIMIT $5""",
                tenant_id, category_norm, self._subject_kind,
                self._placement_predicate, cap)
        return [{"company_id": r["company_id"], "name": r["name"],
                 "quote": r["quote"], "document_id": r["document_id"],
                 "block_id": r["block_id"], "authority_tier": r["authority_tier"]}
                for r in rows]

    @staticmethod
    def _row_to_claim_dict(r) -> dict:
        return {
            "entity_id": r["entity_id"],
            "name": r["name"],
            "kind": r["kind"],
            "primary_domain": r["primary_domain"],
            "claim_id": r["claim_id"],
            "predicate": r["predicate"],
            "object_kind": r["object_kind"],
            "object_value": r["object_value"],
            "object_norm": r["object_norm"],
            "object_entity_id": r["object_entity_id"],
            "confidence": float(r["confidence"]) if r["confidence"] is not None else 0.0,
            "evidence": None if r["document_id"] is None else {
                "document_id": r["document_id"],
                "block_id": r["block_id"],
                "quote": r["quote"],
                "authority_tier": r["authority_tier"],
            },
        }
