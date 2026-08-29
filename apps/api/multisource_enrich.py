"""Multi-source enrichment PIPELINE for the canonical claim graph (Task S2b).

DEEPEN one already-canonical company (`company_id`) from a SECOND wave of sources
(targeted news + EDGAR Form D) — the step that both enriches the grounded profile AND
forces canonicalization. S2a built the CORE (`resolve_entity` / `resolve_conflicts`);
this module is the ORCHESTRATOR that stitches the CORE, the vertical's grounded
extractor, and the claim-graph store into one fetch → ingest → resolve-subject →
extract → upsert → resolve-conflicts pass.

DIVISION OF LABOR (Rule 18 — code owns structure, the LLM owns meaning):
  * The LLM owns every SEMANTIC call, and it lives ENTIRELY inside the injected functions:
    `extract_fn` (which predicate a span asserts) and `resolve_entity_fn` (is this doc's
    issuer the SAME real-world company?). This orchestrator adds NO keyword/regex
    heuristic for meaning.
  * CODE owns the STRUCTURAL routing only: is a doc a filing or news (it reads the
    `source_kind`/`source_key` tags the connector stamped — a computable tag read, not a
    semantic judgement), the value-vs-entity object routing (config on the predicate), the
    deterministic block/claim/evidence writes, and the authority tier lookup.

THE CANONICALIZATION PAYOFF: a targeted NEWS doc was fetched for `company_name`, so its
subject IS `company_id` — attach directly. A FILING/Form-D doc names an ISSUER that may be
an SPV or a same-name NAMESAKE ("Cognition Therapeutics" the biotech vs the AI lab), so its
issuer is run through `resolve_entity_fn` and its claims attach ONLY if the issuer resolves
to `company_id` via a real match method (strong_id / exact_norm / llm_merge). An issuer that
mints a NEW or unresolved entity is REJECTED (recorded, never attached) — the ER prevents a
namesake's filing from polluting the targeted company.

CREDIT DISCIPLINE ([[noesis-credit-discipline]]): `dry_run` defaults TRUE — nothing spends
or writes unless a caller explicitly passes `dry_run=False`. A dry run returns the doc plan
+ a rough cost note and makes NO LLM call. Rejected filing docs are skipped BEFORE
extraction, so a namesake never costs an extract call.

FAIL-SAFE: a broken fetcher, a bad block insert, or an extract/upsert error on one doc is
caught and SKIPPED — the run never aborts on a single doc.

VOCABULARY-NEUTRAL STORE: the `authority_policy` and `predicates` are INJECTED (the store
imports no vertical vocabulary). `fetchers` / `extract_fn` / `resolve_entity_fn` /
`resolve_conflicts_fn` / `llm` are ALL injectable so this pipeline is offline-testable with
fakes — S2c injects the real web-search + EDGAR fetchers, the real extractor, and the S2a
functions. NOT wired into any live route yet (S2c does the wiring + the live run).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from api.claimgraph import normalize_name
# app → vertical is the allowed dependency direction (mirrors claim_extract_job): the
# STRUCTURAL facets→evidence-kind classifier. It reads the tags the connector stamped and
# judges no meaning (Rule 18). `authority_policy` (kind→tier) is INJECTED, not imported.
from roster_vertical.evidence_kind import classify as classify_evidence_kind

_log = logging.getLogger(__name__)

# Stamped on every claim this pipeline writes so a re-enrichment under a newer pipeline is
# distinguishable in the claim rows. Mirrors claim_extract_job.EXTRACTOR_VERSION.
ENRICH_VERSION = "tech-s2b-multisource-enrich-v1"

# Confidence for a claim that already survived the extractor's three gates (predicate,
# span-verify, entail) — a survivor is a high-confidence grounded claim by construction
# (identical rationale to claim_extract_job.GATED_CLAIM_CONFIDENCE).
GATED_CLAIM_CONFIDENCE = 0.9

# A resolved-issuer match method that means "this filing's issuer IS the targeted company".
# 'new' / 'unresolved' are deliberately EXCLUDED — they mint a fresh (SPV/namesake) node.
_ACCEPT_METHODS = frozenset({"strong_id", "exact_norm", "llm_merge"})

# Rough per-extract-call cost note surfaced in the dry-run plan (informational only — the
# real caps live in the extraction job; S2b's plan just makes the projected spend visible).
_PRICE_PER_EXTRACT_USD = 0.01


# --------------------------------------------------------------------------- #
# Pure structural helpers (NO DB / NO network)                                 #
# --------------------------------------------------------------------------- #
def _is_filing_doc(source_key: str, facets: dict) -> bool:
    """STRUCTURAL routing (Rule 18 — reads the tag the connector stamped, judges no
    meaning): a doc is a FILING (issuer must be ER-checked) when its `source_kind` facet is
    'filing'/'patent' or its `source_key` is an EDGAR/SEC feed. Everything else (news,
    analysis, reference, …) is a targeted second-source doc whose subject IS the company we
    searched for."""
    src_kind = (facets.get("source_kind") or "").lower()
    sk = (source_key or "").lower()
    return src_kind in ("filing", "patent") or sk in ("edgar", "sec")


def _active_from(predicates: list[dict]) -> list[dict]:
    """Filter to status=='active' (missing status → active) — matches the extractor's own
    active-set semantics so the object router and the extractor agree on the predicate set."""
    return [p for p in predicates if p.get("status", "active") == "active"]


def _entity_kind_by_predicate(active: list[dict]) -> dict[str, str]:
    """Data-driven object router (identical rule to claim_extract_job): ENTITY predicate name
    → the rs_entity.kind to mint for its object, from the predicate's `object_entity_kind`
    config (default 'company'). Structural (Rule 18) — a per-predicate registry lookup, never
    a keyword guess. Value predicates never appear."""
    return {
        p["name"]: (p.get("object_entity_kind") or "company")
        for p in active
        if p.get("object_kind") == "entity"
    }


# --------------------------------------------------------------------------- #
# rs_block ingest (direct insert — no embedding needed for extraction)         #
# --------------------------------------------------------------------------- #
async def _ensure_block_schema(dsn: str) -> None:
    """Ensure the shared `rs_block` table exists (reuse the retrieval source's DDL so the
    schema stays identical to the rest of the system). Idempotent."""
    from roster_kernel.retrieval.postgres import PostgresRetrievalSource

    src = PostgresRetrievalSource(dsn)
    try:
        await src.ensure_schema()
    finally:
        await src.close()


async def _ingest_block(conn, *, tenant_id: str, document_id: str, block_id: str,
                        text: str, facets: dict, document_title: str, source_key: str) -> None:
    """INSERT one doc as an `rs_block` row (no embedding — extraction reads text/facets, not
    the vector). ON CONFLICT DO NOTHING: re-ingesting the same (tenant, doc, block) is a
    no-op (never clobbers an existing block). Mirrors the block shape
    `claim_extract_job._select_blocks` reads."""
    import json

    await conn.execute(
        """INSERT INTO rs_block
             (tenant_id, document_id, block_id, text, facets, document_title, source_key)
           VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)
           ON CONFLICT (tenant_id, document_id, block_id) DO NOTHING""",
        tenant_id, document_id, block_id, text, json.dumps(facets or {}),
        document_title, source_key)


# --------------------------------------------------------------------------- #
# Claim object routing + upsert (DRY replica of claim_extract_job's routing)   #
# --------------------------------------------------------------------------- #
async def _upsert_claim_with_object(
    store, *, subject_id: str, claim: dict, kind_by_predicate: dict[str, str],
    mintable: frozenset, subject_kind: str, document_id: str, block_id: str,
    source_key: str, authority_tier: int, evidence_kind: str, tenant_id: str,
    object_policy: str = "create",
) -> None:
    """Route a single extracted claim's object (value vs entity) and upsert claim + evidence
    onto `subject_id`. This is the SAME value/entity routing as
    `claim_extract_job.run_claim_extraction` (minted-node kind is CONFIG on the predicate,
    Rule 18) — replicated minimally rather than importing, since the job keeps it inline."""
    predicate = claim["predicate"]
    object_kind = claim["object_kind"]
    quote = claim["quote"]

    if object_kind == "value":
        object_value = claim.get("object_value", "")
        object_entity_id = ""
        object_norm = normalize_name(object_value)
    else:  # entity object
        obj_name = claim.get("object_entity_name", "")
        object_value = ""
        object_norm = normalize_name(obj_name)
        # DATA-DRIVEN routing (Rule 18): the entity KIND to mint is CONFIG on the predicate,
        # never inferred from the object text.
        ekind = kind_by_predicate.get(predicate)
        if ekind not in mintable:
            _log.warning(
                "multisource_enrich: predicate %r has no known object_entity_kind (%r) — "
                "routing object to the %r (subject-kind) path", predicate, ekind, subject_kind)
            ekind = subject_kind

        if object_policy == "mention":
            # MENTION-FIRST ER (fresh graph, zero pollution) — SAME contract as
            # claim_extract_job: promote to a canonical edge ONLY on a real identity anchor;
            # a bare/ambiguous name degrades to a grounded VALUE claim + a parked mention.
            from api.canonicalize import resolve_entity as _resolve_entity
            _r = await _resolve_entity(store, name=obj_name, kind=ekind, tenant_id=tenant_id,
                                       source_key=source_key, on_new="mention")
            if _r["method"] == "mention":
                await store.upsert_mention(name=obj_name, kind=ekind, tenant_id=tenant_id)
                object_kind = "value"
                object_value = obj_name
                object_entity_id = ""
            else:
                object_entity_id = _r["entity_id"]
        elif ekind == subject_kind:
            # LEGACY 'create': SUBJECT-KIND object → exact-alias resolve, else soft `__unresolved:`.
            resolved = await store.resolve_alias(obj_name)
            if resolved:
                object_entity_id = resolved
            else:
                object_entity_id = "__unresolved:" + object_norm
                await store.upsert_entity(object_entity_id, kind=subject_kind,
                                          name=obj_name, tenant_id=tenant_id)
        else:
            # LEGACY 'create': category/person/investor → deterministic soft `<kind>:<norm>` node.
            object_entity_id = f"{ekind}:{object_norm}"
            await store.upsert_entity(object_entity_id, kind=ekind, name=obj_name,
                                      tenant_id=tenant_id)

    claim_id = await store.upsert_claim(
        subject_id=subject_id, predicate=predicate, object_kind=object_kind,
        object_value=object_value, object_entity_id=object_entity_id,
        object_norm=object_norm, confidence=GATED_CLAIM_CONFIDENCE,
        extractor_version=ENRICH_VERSION, tenant_id=tenant_id)
    await store.add_evidence(
        claim_id, document_id, block_id, quote, source_key=source_key,
        authority_tier=authority_tier, evidence_kind=evidence_kind, tenant_id=tenant_id)


# --------------------------------------------------------------------------- #
# The pipeline                                                                 #
# --------------------------------------------------------------------------- #
async def enrich_company(
    *,
    store,
    dsn: str,
    company_id: str,
    company_name: str,
    fetchers: list[Callable],
    extract_fn: Callable,
    resolve_entity_fn: Callable,
    resolve_conflicts_fn: Callable,
    authority_policy,
    llm=None,
    tenant_id: str = "demo",
    predicates: list[dict] | None = None,
    dry_run: bool = True,
    conflict_predicates: Sequence[str] = ("raised_funding", "headcount", "company_status"),
    object_policy: str = "create",
) -> dict:
    """Enrich ONE canonical company (`company_id`) from 2nd sources. Returns a summary:
    `{company_id, docs_fetched, blocks_ingested, claims_added, per_source, resolutions,
    rejected, dry_run}` (dry runs additionally carry `plan`).

    Pipeline (see module docstring for the WHY):
      1. Collect candidate docs from each injected `fetcher(company_name)` (fail-safe: a
         broken fetcher is skipped, never aborts).
      2. `dry_run` → stop, return the plan (docs it WOULD ingest + a rough cost note). No
         LLM, no writes.
      3. LIVE, per doc (each doc fully fail-safe):
         a. INGEST as an rs_block (direct insert, ON CONFLICT DO NOTHING).
         b. RESOLVE the SUBJECT: a NEWS doc → `company_id` directly; a FILING/Form-D doc →
            `resolve_entity_fn(issuer_name)`, attach ONLY if it resolves to `company_id`
            via strong_id/exact_norm/llm_merge, else RECORD under `rejected` (the ER payoff).
         c. EXTRACT grounded claims via `extract_fn` (skipped for rejected docs — no spend).
         d. UPSERT each claim onto the resolved subject + evidence at the source's authority
            tier (`classify(source_key, facets)` → `authority_policy.rank`).
      4. For each `conflict_predicates` predicate that got ≥1 new claim on `company_id`, run
         `resolve_conflicts_fn` and collect the resolutions (Form-D controlling wins over news;
         losers stay queryable).
      5. Return the summary.
    """
    await store.ensure_schema()

    # Predicate set: explicit caller list, else the AUTHORITATIVE store registry
    # (`active_predicate_names` — vocabulary-neutral; no vertical import for the vocabulary).
    if predicates is not None:
        active = _active_from(predicates)
    else:
        active = await store.active_predicate_names()
    kind_by_predicate = _entity_kind_by_predicate(active)
    mintable = frozenset(store.mintable_entity_kinds)
    subject_kind = store.subject_kind

    # --- 1) COLLECT (fail-safe per fetcher) --------------------------------------------- #
    docs: list[dict] = []
    for fetcher in fetchers:
        try:
            fetched = await fetcher(company_name)
        except Exception:  # noqa: BLE001 — a broken fetcher never aborts the run
            _log.warning("multisource_enrich: fetcher %r errored — skipping its docs",
                         getattr(fetcher, "__name__", fetcher), exc_info=True)
            continue
        for d in fetched or []:
            if isinstance(d, dict) and (d.get("text") or "").strip():
                docs.append(d)

    per_source: dict[str, dict] = {}

    def _bump(sk: str, field: str, n: int = 1) -> None:
        row = per_source.setdefault(
            sk or "", {"docs": 0, "blocks_ingested": 0, "claims_added": 0, "rejected": 0})
        row[field] += n

    for d in docs:
        _bump(d.get("source_key", ""), "docs")

    # --- 2) DRY RUN (default): the plan, no LLM, no writes ------------------------------ #
    if dry_run:
        plan = [{"source_key": d.get("source_key", ""),
                 "document_id": d.get("document_id", ""),
                 "is_filing": _is_filing_doc(d.get("source_key", ""), d.get("facets") or {}),
                 "issuer_name": d.get("issuer_name", "")}
                for d in docs]
        return {
            "company_id": company_id,
            "docs_fetched": len(docs),
            "blocks_ingested": 0,
            "claims_added": 0,
            "per_source": per_source,
            "resolutions": [],
            "rejected": [],
            "dry_run": True,
            "plan": {
                "docs": plan,
                "cost_note": (f"~{len(docs)} extract call(s) if every doc's subject resolves; "
                              f"~${round(len(docs) * _PRICE_PER_EXTRACT_USD, 6)} at "
                              f"${_PRICE_PER_EXTRACT_USD}/call (rejected filings cost nothing)"),
            },
        }

    # --- 3) LIVE ------------------------------------------------------------------------ #
    await _ensure_block_schema(dsn)
    import asyncpg

    conn = await asyncpg.connect(dsn)
    blocks_ingested = 0
    claims_added = 0
    rejected: list[dict] = []
    touched_on_company: set[str] = set()
    # Cache the same-entity judge across docs (identical issuer names resolve once).
    judge_cache: dict = {}
    try:
        for d in docs:
            source_key = d.get("source_key", "")
            document_id = d.get("document_id", "")
            text = d.get("text", "") or ""
            facets = d.get("facets") or {}
            block_id = "b0"
            try:
                # a) INGEST the block
                await _ingest_block(
                    conn, tenant_id=tenant_id, document_id=document_id, block_id=block_id,
                    text=text, facets=facets, document_title=company_name,
                    source_key=source_key)
                blocks_ingested += 1
                _bump(source_key, "blocks_ingested")

                # b) RESOLVE THE SUBJECT
                if _is_filing_doc(source_key, facets):
                    res = await resolve_entity_fn(
                        store, name=d.get("issuer_name") or company_name, kind="company",
                        tenant_id=tenant_id, strong_ids=d.get("strong_ids"),
                        source_key=source_key, context=text[:1200], llm=llm,
                        judge_cache=judge_cache,
                        on_new=("mention" if object_policy == "mention" else "create"))
                    entity_id = res.get("entity_id")
                    method = res.get("method")
                    # mention-first: an SPV/namesake issuer resolves to a MENTION (entity_id=None),
                    # so the match fails and it is rejected WITHOUT minting a stray canonical entity.
                    if not (entity_id == company_id and method in _ACCEPT_METHODS):
                        # SPV / namesake → DO NOT pollute the company; record + skip (no spend).
                        rejected.append({
                            "document_id": document_id, "source_key": source_key,
                            "issuer_name": d.get("issuer_name") or company_name,
                            "resolved_entity_id": entity_id, "method": method,
                            "reason": (f"issuer resolved to {entity_id!r} via {method!r}, "
                                       f"not the targeted company {company_id!r}"),
                        })
                        _bump(source_key, "rejected")
                        continue
                    subject_id = company_id
                else:
                    # a targeted NEWS/second-source doc — subject IS the searched company.
                    subject_id = company_id

                # c) EXTRACT grounded claims
                claims = await extract_fn(
                    block_text=text, subject_name=company_name,
                    predicates=active, client=llm) or []

                # d) UPSERT each claim + evidence at the source's authority tier
                kind = classify_evidence_kind(source_key, facets)
                tier = authority_policy.rank(kind)
                for c in claims:
                    await _upsert_claim_with_object(
                        store, subject_id=subject_id, claim=c,
                        kind_by_predicate=kind_by_predicate, mintable=mintable,
                        subject_kind=subject_kind, document_id=document_id,
                        block_id=block_id, source_key=source_key, authority_tier=tier,
                        evidence_kind=kind, tenant_id=tenant_id, object_policy=object_policy)
                    claims_added += 1
                    _bump(source_key, "claims_added")
                    if subject_id == company_id:
                        touched_on_company.add(c["predicate"])
            except Exception:  # noqa: BLE001 — one bad doc is skipped, never aborts the run
                _log.warning("multisource_enrich: doc %r (%s) errored — skipping",
                             document_id, source_key, exc_info=True)
                continue
    finally:
        await conn.close()

    # --- 4) CONFLICT RESOLUTION on the touched conflict predicates ---------------------- #
    resolutions: list[dict] = []
    for p in conflict_predicates:
        if p not in touched_on_company:
            continue
        try:
            written = await resolve_conflicts_fn(
                store, subject_id=company_id, predicate=p, tenant_id=tenant_id,
                authority_policy=authority_policy)
            resolutions.extend(written or [])
        except Exception:  # noqa: BLE001 — resolution of one predicate never aborts the run
            _log.warning("multisource_enrich: resolve_conflicts errored for predicate %r", p,
                         exc_info=True)
            continue

    return {
        "company_id": company_id,
        "docs_fetched": len(docs),
        "blocks_ingested": blocks_ingested,
        "claims_added": claims_added,
        "per_source": per_source,
        "resolutions": resolutions,
        "rejected": rejected,
        "dry_run": False,
    }
