"""Corpus → claim-graph EXTRACTION JOB — Task 2b of the claim-graph build.

Source-agnostic, app-level (asyncpg) orchestrator that walks `rs_block` for a set of
`source_key`s, hands each block to the vertical's GROUNDED typed-claim extractor
(`roster_vertical.claim_extract.extract_typed_claims` — Task 2a), resolves the
subject/object entities, and upserts grounded claims + evidence into the claim graph
(`api.claimgraph.ClaimGraphStore` — Task 2a's store). It reuses BOTH landed pieces and
duplicates neither the extractor's three gates nor the store's write ethos.

DIVISION OF LABOR (Rule 18): the LLM owns every semantic call (it lives entirely inside
`extract_typed_claims`). This job owns only STRUCTURAL/COMPUTABLE work — block selection,
strong-id entity resolution, deterministic upserts, the run ledger, and cost caps. There is
NO keyword relevance heuristic here: for slice 1 the `source_key` filter (`yc`) IS the
structural relevance gate, and there is NO fuzzy ER — subjects resolve by strong id
(`document_id == f"{source_key}:{native_id}" == "yc:<slug>"`), objects by exact
normalized-name alias or a soft `__unresolved:<norm>` node (never a fuzzy merge).

CREDIT DISCIPLINE ([[noesis-credit-discipline]]): `dry_run` defaults TRUE, so nothing
spends unless a caller explicitly passes `dry_run=False`. Cost is projected up front and
the caps (`max_blocks` / `max_llm_calls` / `max_usd`) ABORT the run BEFORE any LLM call —
a request that would exceed a cap is refused outright, never partially run.

NOT wired into the worker auto-loop or app startup — the only trigger surface is the
explicit CLI `scripts/run_claim_extract.py` (dry-run by default). Default OFF = nothing
calls this automatically.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from api.claimgraph import normalize_name
from api.claimgraph_tech import make_tech_claim_store

# The extractor is imported as a MODULE (not the bound function) so a test can monkeypatch
# `roster_vertical.claim_extract.extract_typed_claims_counted` and this job picks up the
# patched attribute — keeping every test offline (no network / no live LLM call).
from roster_vertical import claim_extract
from roster_vertical.authority import TechAuthorityPolicy
from roster_vertical.evidence_kind import classify as classify_evidence_kind

_log = logging.getLogger(__name__)

# Stamped on every claim this job writes so a re-extraction under a newer extractor is
# distinguishable in the ledger / claim rows.
EXTRACTOR_VERSION = "tech-slice1-claim-extract-v1"

# Confidence assigned to a claim that already survived the extractor's three gates
# (predicate-in-active-set, verbatim span-verify, entailment). The extractor emits no
# score of its own; a survivor is a high-confidence grounded claim by construction.
GATED_CLAIM_CONFIDENCE = 0.9

# The set of object entity-kinds this job may MINT a deterministic node for is INJECTED on
# the store (`store.mintable_entity_kinds`) — vocabulary lives in the wiring, not the job, so
# adding a vertical entity-kind needs no code edit here. Any entity predicate whose configured
# kind is outside the store's set falls back to the store's subject-kind path (safe default)
# with a warning. STRUCTURAL routing (Rule 18): the kind is CONFIG on the predicate/store, the
# LLM still owns which predicate a span asserts.

_AUTHORITY = TechAuthorityPolicy()


def _entity_kind_by_predicate(active: list[dict]) -> dict[str, str]:
    """Data-driven object router: map each ENTITY predicate name → the rs_entity.kind to mint
    for its object, read from the predicate's `object_entity_kind` config (defaults to
    'company' when unset). Structural (Rule 18) — a per-predicate lookup built from the
    registry, never a keyword guess."""
    return {
        p["name"]: (p.get("object_entity_kind") or "company")
        for p in active
        if p.get("object_kind") == "entity"
    }


# --------------------------------------------------------------------------- #
# Pure helpers (NO DB / NO network — unit-tested directly)                     #
# --------------------------------------------------------------------------- #
def _parse_facets(f: Any) -> dict:
    """asyncpg may hand back a jsonb column as a dict OR a JSON string — normalize to dict
    (mirrors `app.py::_parse_facets`). Fail-safe → {}."""
    if isinstance(f, dict):
        return f
    try:
        return json.loads(f) if f else {}
    except Exception:  # noqa: BLE001
        return {}


def _slug_of(document_id: str) -> str:
    """The connector's `native_id` = the part after the `source_key:` prefix (structural
    string split, not a heuristic). For a YC block `yc:acme` → `acme`."""
    return document_id.split(":", 1)[1] if ":" in document_id else document_id


def _first_heading(text: str) -> str:
    """First `# ` markdown H1 in the block, or '' — a fallback subject name when the block
    carries no `document_title`."""
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    return ""


def _subject_name(document_title: str, text: str, slug: str) -> str:
    """Subject display name: the block's `document_title`, else its first H1 heading, else
    the slug. Never empty (the slug is always present)."""
    return (document_title or "").strip() or _first_heading(text) or slug


def project_cost(
    blocks_considered: int,
    *,
    max_blocks: int,
    max_llm_calls: int,
    max_usd: float,
    price_per_call_usd: float,
) -> dict:
    """Project the LLM cost of extracting `blocks_considered` blocks and evaluate the caps —
    PURE, no side effects, so the cap math is unit-testable with no DB.

    This extractor is ONE extract call PER BLOCK (it is not the 10-atom batcher), so
    `projected_calls == blocks_considered` exactly (ceil(n/1)). It ALSO makes up to ONE
    batched ENTAILMENT-judge call per block (all a block's span-verified survivors are
    judged in a single `entail_claims` call), so the entail spend that the old projection
    ignored is bounded by `blocks_considered` too. `est_entail_calls` uses that ceiling
    (assumption: ≥1 groundable claim per block → one entail call), `est_total_calls`/
    `est_total_usd` fold it in so the projection no longer UNDERCOUNTS the true LLM spend.

    Back-compat: `projected_calls`, `est_usd`, `over_caps`, and `caps` are UNCHANGED (still
    extract-call-only) — the abort caps are evaluated against the outer extract calls exactly
    as before; the entail figures are additional, informational fields. Returns the
    projection plus a list of cap violations (`over_caps`) — non-empty means the run must
    ABORT before any spend.
    """
    projected_calls = blocks_considered  # one extract call per block
    est_usd = round(projected_calls * price_per_call_usd, 6)
    # Entail projection (ceiling: ≤1 batched entail call per block). Informational — folded
    # into est_total_* but NOT into the extract-only caps (back-compat), so an existing run's
    # abort behavior is unchanged while the true spend is now visible.
    est_entail_calls = blocks_considered
    est_total_calls = projected_calls + est_entail_calls
    est_total_usd = round(est_total_calls * price_per_call_usd, 6)
    over: list[str] = []
    if blocks_considered > max_blocks:
        over.append(f"blocks_considered {blocks_considered} > max_blocks {max_blocks}")
    if projected_calls > max_llm_calls:
        over.append(f"projected_calls {projected_calls} > max_llm_calls {max_llm_calls}")
    if est_usd > max_usd:
        over.append(f"est_usd {est_usd} > max_usd {max_usd}")
    return {
        "blocks_considered": blocks_considered,
        "projected_calls": projected_calls,
        "est_usd": est_usd,
        "est_entail_calls": est_entail_calls,
        "est_total_calls": est_total_calls,
        "est_total_usd": est_total_usd,
        "over_caps": over,
        "caps": {"max_blocks": max_blocks, "max_llm_calls": max_llm_calls, "max_usd": max_usd},
    }


def _only_active(predicates: list[dict]) -> list[dict]:
    """Filter a predicate-dict list to status=='active' (missing status → active)."""
    return [p for p in predicates if p.get("status", "active") == "active"]


async def _resolve_active_predicates(store, predicates: list[dict] | None) -> list[dict]:
    """The predicates to constrain extraction to. When the caller passes an explicit list
    (as the diligence route does), use it. When it passes None, the DB registry
    (`rs_predicate`) is the AUTHORITATIVE source of truth — read the active rows from the
    store; only if the registry is still empty (a first run before any seed) fall back to
    the store's INJECTED seed list. Net effect: the registry becomes the default, explicit
    callers still override (H1 §A)."""
    if predicates is not None:
        return _only_active(predicates)
    registry = await store.active_predicate_names()
    if registry:
        return registry
    return _only_active(store.seed_predicates or [])


# --------------------------------------------------------------------------- #
# Block selection (read-only, its own short-lived connection)                 #
# --------------------------------------------------------------------------- #
async def _select_blocks(dsn: str, *, source_keys: list[str], tenant_id: str,
                         limit: int | None) -> list[dict]:
    """Select non-empty `rs_block` rows for the given `source_key`s (the structural relevance
    filter). Returns dicts with parsed facets; blocks with empty text are skipped."""
    import asyncpg

    sql = (
        "SELECT document_id, block_id, text, facets, document_title, source_key "
        "FROM rs_block WHERE tenant_id = $1 AND source_key = ANY($2) "
        "ORDER BY document_id, block_id"
    )
    args: list[Any] = [tenant_id, list(source_keys)]
    if limit is not None:
        args.append(int(limit))
        sql += f" LIMIT ${len(args)}"
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(sql, *args)
    finally:
        await conn.close()
    out: list[dict] = []
    for r in rows:
        text = r["text"] or ""
        if not text.strip():  # skip empty blocks (nothing groundable)
            continue
        out.append({
            "document_id": r["document_id"],
            "block_id": r["block_id"],
            "text": text,
            "facets": _parse_facets(r["facets"]),
            "document_title": r["document_title"] or "",
            "source_key": r["source_key"] or "",
        })
    return out


# --------------------------------------------------------------------------- #
# The job                                                                      #
# --------------------------------------------------------------------------- #
async def run_claim_extraction(
    *,
    dsn: str,
    source_keys: list[str],
    tenant_id: str = "demo",
    limit: int | None = None,
    dry_run: bool = True,
    max_blocks: int = 500,
    max_llm_calls: int = 200,
    max_usd: float = 5.0,
    price_per_call_usd: float = 0.01,
    client=None,
    model: str | None = None,
    predicates: list[dict] | None = None,
    store=None,
    object_policy: str = "create",
) -> dict:
    """Scan `rs_block` for `source_keys`, extract typed grounded claims PER BLOCK, resolve
    entities by strong id, and upsert them into the claim graph. Returns a run-summary dict.

    DEFAULT `dry_run=True` — the credit-safe default (shared prod credits). A dry run does
    the full selection + cost projection and records the run, but makes NO LLM call and
    writes NO entities/claims/evidence. Caps ABORT before any spend (see `project_cost`).
    """
    # Default: the tech-configured store (§D wiring). A caller (e.g. a second vertical, or a
    # vocabulary-neutrality test) may inject a differently-configured store; existing callers
    # pass nothing and get the tech store unchanged.
    store = store if store is not None else make_tech_claim_store(dsn)
    run_id = "run:" + uuid.uuid4().hex
    source_keys_str = ",".join(source_keys)
    try:
        await store.ensure_schema()
        # Predicate set: explicit caller list, else the AUTHORITATIVE DB registry (§A).
        active = await _resolve_active_predicates(store, predicates)
        kind_by_predicate = _entity_kind_by_predicate(active)  # data-driven object router
        # mention-first spine: which VALUE predicates' objects also accrete in the mention lane
        # (tech/product/segment names) so promotion-by-corroboration can mint canonical nodes.
        mention_kind_by_predicate = {p["name"]: p["mention_kind"]
                                     for p in active if p.get("mention_kind")}
        # Injected vocabulary (§D): subject kind + the mintable object entity-kinds.
        subject_kind = store.subject_kind
        mintable = frozenset(store.mintable_entity_kinds)

        # 1) Select eligible (non-empty) blocks — source_key IS the relevance gate (slice 1).
        blocks = await _select_blocks(dsn, source_keys=source_keys, tenant_id=tenant_id, limit=limit)
        blocks_considered = len(blocks)

        # 2) Cost projection + cap enforcement — BEFORE any LLM call, in BOTH modes.
        proj = project_cost(
            blocks_considered,
            max_blocks=max_blocks, max_llm_calls=max_llm_calls, max_usd=max_usd,
            price_per_call_usd=price_per_call_usd,
        )
        base_params = {
            "source_keys": source_keys, "tenant_id": tenant_id, "limit": limit,
            "dry_run": dry_run, "price_per_call_usd": price_per_call_usd,
            "projected_calls": proj["projected_calls"], "est_usd": proj["est_usd"],
            # entail-inclusive projection (§C) recorded for prod cost observability
            "est_entail_calls": proj["est_entail_calls"],
            "est_total_calls": proj["est_total_calls"], "est_total_usd": proj["est_total_usd"],
            "caps": proj["caps"],
        }

        if proj["over_caps"]:
            # ABORT: a request over any cap is refused outright — nothing is written.
            await store.record_run(run_id, source_keys=source_keys_str, status="aborted",
                                   params={**base_params, "abort_reason": proj["over_caps"]},
                                   tenant_id=tenant_id)
            await store.finish_run(run_id, status="aborted",
                                   blocks_considered=blocks_considered)
            return {
                "run_id": run_id, "status": "aborted", "dry_run": dry_run,
                "blocks_considered": blocks_considered,
                "projected_calls": proj["projected_calls"],
                "extract_calls": 0, "claims_emitted": 0,
                "est_cost_usd": proj["est_usd"], "abort_reason": proj["over_caps"],
            }

        # 3) DRY RUN (default): record the projection, spend nothing, write nothing.
        if dry_run:
            await store.record_run(run_id, source_keys=source_keys_str, status="dry_run",
                                   params=base_params, tenant_id=tenant_id)
            await store.finish_run(run_id, status="dry_run",
                                   blocks_considered=blocks_considered,
                                   est_cost_usd=proj["est_usd"])
            return {
                "run_id": run_id, "status": "dry_run", "dry_run": True,
                "blocks_considered": blocks_considered,
                "projected_calls": proj["projected_calls"],
                "extract_calls": 0, "claims_emitted": 0,
                "est_cost_usd": proj["est_usd"],
            }

        # 4) LIVE: one extract call per block; resolve + upsert grounded claims.
        await store.record_run(run_id, source_keys=source_keys_str, status="running",
                               params=base_params, tenant_id=tenant_id)
        extract_calls = 0
        entail_calls = 0
        claims_emitted = 0
        for b in blocks:
            document_id = b["document_id"]
            source_key = b["source_key"]
            facets = b["facets"]
            slug = _slug_of(document_id)
            subject_id = document_id  # STRONG id: `yc:<slug>` — no fuzzy ER
            subject_name = _subject_name(b["document_title"], b["text"], slug)

            await store.upsert_entity(subject_id, kind=subject_kind, name=subject_name,
                                      facets=facets, first_run_id=run_id, tenant_id=tenant_id)
            await store.add_alias(subject_name, subject_id, source=source_key)

            # Counted extractor so the batched entail-judge spend is tallied into the ledger
            # (the outer extract-call count alone undercounts — H1 §C).
            claims, n_entail = await claim_extract.extract_typed_claims_counted(
                block_text=b["text"], subject_name=subject_name,
                predicates=active, client=client, model=model)
            extract_calls += 1  # ONE call per block, counted whether or not claims came back
            entail_calls += n_entail

            kind = classify_evidence_kind(source_key, facets)
            tier = _AUTHORITY.rank(kind)

            for c in claims:
                predicate = c["predicate"]
                object_kind = c["object_kind"]
                quote = c["quote"]

                if object_kind == "value":
                    object_value = c["object_value"]
                    object_entity_id = ""
                    object_norm = normalize_name(object_value)
                    # tech-flow spine: a tech/product/segment VALUE also accretes as a mention
                    # (across companies) so it can be promoted-by-corroboration into a node later.
                    if object_policy == "mention" and object_value:
                        _mk = mention_kind_by_predicate.get(predicate)
                        if _mk:
                            await store.upsert_mention(name=object_value, kind=_mk,
                                                       tenant_id=tenant_id)
                else:  # entity object
                    obj_name = c["object_entity_name"]
                    object_value = ""
                    object_norm = normalize_name(obj_name)
                    # DATA-DRIVEN routing (Rule 18): the entity KIND to mint is CONFIG on the
                    # predicate (`object_entity_kind`), never inferred from the object text.
                    ekind = kind_by_predicate.get(predicate)
                    if ekind not in mintable:
                        # unknown/missing kind → safe default (subject-kind path), but
                        # surface it: a predicate mis-declared its object kind, not a silent
                        # mis-model.
                        _log.warning(
                            "claim_extract_job: predicate %r has no known object_entity_kind "
                            "(%r) — routing object to the %r (subject-kind) path",
                            predicate, ekind, subject_kind)
                        ekind = subject_kind

                    if object_policy == "mention":
                        # MENTION-FIRST ER (fresh graph, zero pollution): promote the object to a
                        # canonical entity edge ONLY on a real identity anchor (strong-id / exact
                        # single / confident merge). A bare/ambiguous name is NOT minted as a node —
                        # it degrades to a grounded VALUE (the cited name is kept, so the FACT
                        # survives) and is parked in the mention lane for later promotion.
                        from api.canonicalize import resolve_entity as _resolve_entity
                        _r = await _resolve_entity(
                            store, name=obj_name, kind=ekind, tenant_id=tenant_id,
                            source_key=source_key, on_new="mention")
                        if _r["method"] == "mention":
                            await store.upsert_mention(name=obj_name, kind=ekind,
                                                       tenant_id=tenant_id)
                            object_kind = "value"          # degrade: grounded cited value, no node
                            object_value = obj_name
                            object_entity_id = ""
                        else:
                            object_entity_id = _r["entity_id"]   # real canonical edge
                    elif ekind == subject_kind:
                        # LEGACY 'create' (existing/discarded graph): SUBJECT-KIND object (tech:
                        # company) → exact-alias resolve, else a soft `__unresolved:` node.
                        resolved = await store.resolve_alias(obj_name)
                        if resolved:
                            object_entity_id = resolved
                        else:
                            object_entity_id = "__unresolved:" + object_norm
                            await store.upsert_entity(object_entity_id, kind=subject_kind,
                                                      name=obj_name, tenant_id=tenant_id)
                    else:
                        # LEGACY 'create': category/person/investor → deterministic soft
                        # `<kind>:<norm>` node of the configured kind (minted fresh).
                        object_entity_id = f"{ekind}:{object_norm}"
                        await store.upsert_entity(object_entity_id, kind=ekind,
                                                  name=obj_name, tenant_id=tenant_id)

                claim_id = await store.upsert_claim(
                    subject_id=subject_id, predicate=predicate, object_kind=object_kind,
                    object_value=object_value, object_entity_id=object_entity_id,
                    object_norm=object_norm, confidence=GATED_CLAIM_CONFIDENCE,
                    extractor_version=EXTRACTOR_VERSION, tenant_id=tenant_id)
                await store.add_evidence(
                    claim_id, document_id, b["block_id"], quote, source_key=source_key,
                    authority_tier=tier, evidence_kind=kind, tenant_id=tenant_id)
                claims_emitted += 1

        # Cost = extract calls + tallied entail-judge calls (§C: the entail spend the old
        # count ignored). `entail_calls` is a whitelisted ledger counter.
        total_calls = extract_calls + entail_calls
        est_cost_usd = round(total_calls * price_per_call_usd, 6)
        await store.finish_run(
            run_id, status="done",
            blocks_considered=blocks_considered, blocks_relevant=blocks_considered,
            extract_calls=extract_calls, entail_calls=entail_calls,
            claims_emitted=claims_emitted, est_cost_usd=est_cost_usd)
        return {
            "run_id": run_id, "status": "done", "dry_run": False,
            "blocks_considered": blocks_considered,
            "projected_calls": proj["projected_calls"],
            "extract_calls": extract_calls, "entail_calls": entail_calls,
            "claims_emitted": claims_emitted,
            "est_cost_usd": est_cost_usd,
        }
    finally:
        await store.close()
