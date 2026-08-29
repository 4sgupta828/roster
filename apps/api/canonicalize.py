"""Canonicalization CORE — entity resolution + conflict resolution (Task S2a).

The two primitives a CANONICAL claim graph needs the moment a SECOND source ingests
(EDGAR Form D + news alongside YC): the same company/founder arriving from multiple
sources under different names must resolve to ONE canonical entity (entity resolution),
and contradictory facts (Form D "$10M" vs news "$12M") must resolve to an authoritative
winner with the losers kept visible (conflict resolution). See
`learnings/design-improvement.md` §3 (conflict) + §5 (ER).

DIVISION OF LABOR (Rule 18 — code owns structure, the LLM owns meaning):
  * CODE owns every STRUCTURAL / COMPUTABLE step — strong-id lookups, exact normalized-name
    lookups, the winner ORDERING (is_controlling → authority_tier → confidence → recency),
    and the writes. There is NO fuzzy-string same-entity heuristic anywhere.
  * The LLM owns the ONE semantic call — "are these two mentions the SAME real-world entity?"
    — and only when the structural lookups leave genuine ambiguity (>1 normalized-name
    candidate). It is injectable/monkeypatchable for tests.

FAIL-SAFE: an ambiguous case with no LLM, or a judge error, or an unconfident judge NEVER
produces a merge. It creates a NEW entity flagged `facets.resolution='unresolved_candidate'`
(human-reviewable) — keeping the entities separate is always safe; a false merge is not.

APPEND-ONLY: conflict resolution NAMES the winner in `rs_claim_resolution`; it never
retracts or deletes the losers — they stay fully queryable.

The store stays VOCABULARY-NEUTRAL: the `authority_policy` (which evidence kind is
controlling, the tier ranking) is INJECTED into `resolve_conflicts`, never imported by the
store. NOT wired into any live route yet (S2c does that).
"""
from __future__ import annotations

import logging
import re

from api.claimgraph import _sha, normalize_name, normalize_quote
from roster_kernel.research.claims_first import ENTAIL_MODEL, _client, _json

_log = logging.getLogger(__name__)

# The same-entity judge is a correctness-critical semantic call (a false merge collapses two
# real companies), so it defaults to the STRONGER entail-tier model. Env/param-overridable.
JUDGE_MODEL = ENTAIL_MODEL

# Only a confident SAME verdict merges — anything below is fail-safe "keep separate".
_MERGE_CONFIDENCE = 0.8

_SAME_ENTITY_SYSTEM = (
    "You are a strict entity-resolution judge. You are given MENTION A (a name plus any "
    "surrounding context and its source) and CANDIDATE B (an existing entity's name plus "
    "known facts). Decide whether A and B refer to the SAME real-world entity of the stated "
    "kind — the SAME company / person / organization, not merely a similar name or the same "
    "industry. Distinct entities that share a common word (e.g. two different 'Apex' "
    "startups) are NOT the same. Output STRICT JSON: "
    '{"same": true|false, "confidence": 0.0-1.0, "why": "<one short reason>"}. '
    "Be conservative: when the evidence does not clearly establish identity, same=false."
)


def _norm_strong_id(val: str) -> str:
    """Structural normalization for a STRONG-ID value (cik/domain/yc_slug/…): lowercase +
    trim + collapse internal whitespace. NOT `normalize_name` — a strong id is an opaque
    token (a domain's dots, a CIK's digits) that legal-suffix stripping would corrupt."""
    return re.sub(r"\s+", "", (val or "").strip().lower())


def _preferred_strong_id(strong_ids: dict | None) -> str:
    """The canonical id a NEW entity should take when a strong id is present (a strong id is
    a better global key than `kind:norm`). First non-empty (scheme, val) in insertion order."""
    for scheme, val in (strong_ids or {}).items():
        nv = _norm_strong_id(str(val))
        if scheme and nv:
            return f"{scheme}:{nv}"
    return ""


async def _judge_same_entity(client, *, name: str, kind: str, context: str, source_key: str,
                             candidate: dict) -> dict | None:
    """One same-entity LLM call. Returns the parsed verdict dict `{same, confidence, why}`
    or None on any error (fail-safe → the caller treats None as 'not a confident merge')."""
    cand_name = candidate.get("name", "")
    facets = candidate.get("facets") or {}
    facts = ", ".join(f"{k}={v}" for k, v in facets.items() if k != "resolution") or "(none)"
    user = (
        f"MENTION A:\n  name: {name}\n  kind: {kind}\n"
        f"  context: {context or '(none)'}\n  source: {source_key or '(unknown)'}\n\n"
        f"CANDIDATE B:\n  name: {cand_name}\n  kind: {kind}\n  known facts: {facts}\n\n"
        f"Do A and B refer to the SAME real-world {kind}? Return ONLY the JSON."
    )
    try:
        raw = await _json(client, JUDGE_MODEL, _SAME_ENTITY_SYSTEM, user)
    except Exception:  # noqa: BLE001  (fail-safe: any judge error → no merge)
        _log.warning("resolve_entity: same-entity judge errored for %r vs %r — treating as "
                     "not-same (fail-safe)", name, cand_name)
        return None
    if not isinstance(raw, dict):
        return None
    return {
        "same": bool(raw.get("same")),
        "confidence": float(raw.get("confidence") or 0.0),
        "why": str(raw.get("why") or ""),
    }


def _mention_result(*, name: str, kind: str, norm: str, candidates: list[dict]) -> dict:
    """A MENTION decision (mention-first ER, `on_new='mention'`): an evidence-bound name that has
    NO canonical identity anchor (no strong id, no exact single match, no confident merge). It is
    NOT written as a canonical `rs_entity` — the caller parks it in the mention lane (a separate
    layer graph views never read) and may promote it later on stronger evidence. This is the
    contract that makes zero-pollution achievable: a bare name never spawns a canonical node
    (never a duplicate) and is never force-merged (never a wrong merge)."""
    return {"entity_id": None, "is_new": False, "method": "mention",
            "mention": {"name": name, "norm": norm, "kind": kind},
            "candidates": candidates}


async def resolve_entity(store, *, name: str, kind: str, tenant_id: str = "demo",
                         strong_ids: dict | None = None, source_key: str = "",
                         context: str = "", llm=None, judge_cache: dict | None = None,
                         on_new: str = "create") -> dict:
    """Resolve a subject/object MENTION to a CANONICAL `entity_id`.

    `on_new` (mention-first ER, default 'create' = byte-identical legacy behavior):
      - 'create': an unanchored name (0 candidates + no strong id) or an ambiguous no-confident-
        merge creates a flagged canonical entity (legacy — used by the existing/​discarded graph).
      - 'mention': those SAME cases instead return a MENTION decision (`method='mention'`,
        `entity_id=None`) and create NOTHING — only a STRONG-ID / EXACT-single / CONFIDENT-merge
        promotes to canonical. This is the FRESH-graph contract (resolve-before-mint; zero pollution).

    Returns `{entity_id, is_new, method, candidates}` where `method` is one of
    `strong_id | exact_norm | new | llm_merge | unresolved | mention`. `candidates` is the list of
    normalized-name candidate entities that were considered (for observability).

    Order (Rule 18 — code owns the structural lookups, the LLM owns the ambiguous
    same-entity judgment):
      1. STRONG-ID: if any strong id resolves to an EXISTING entity → resolve to it.
      2. EXACT-NORM: `normalize_name(name)` → `find_entities_by_norm`:
           - exactly 1 → resolve to it.
           - 0 → create a NEW entity (id = the strong id if one was given, else `kind:norm`).
           - >1 → with a strong id present the strong id disambiguates (new entity at the
             strong id, no LLM); otherwise LLM ADJUDICATION over the candidates. A confident
             SAME (same AND confidence>=0.8) with exactly one candidate → merge; none/uncertain
             or no LLM or a judge error → a flagged `unresolved_candidate` entity (NEVER a
             false merge). The judge is cached by (norm, candidate_id)."""
    strong_ids = strong_ids or {}
    if judge_cache is None:
        judge_cache = {}

    # 1) STRONG-ID — an existing entity keyed by a strong id resolves outright.
    for scheme, val in strong_ids.items():
        nv = _norm_strong_id(str(val))
        if not scheme or not nv:
            continue
        cid = f"{scheme}:{nv}"
        existing = await store.get_entity(cid, tenant_id=tenant_id)
        if existing is None:
            # a prior mention may have registered the strong-id string as an alias
            aliased = await store.resolve_alias(cid)
            if aliased:
                existing = await store.get_entity(aliased, tenant_id=tenant_id)
                cid = aliased
        if existing is not None:
            await store.add_alias(name, cid, source=source_key)
            return {"entity_id": cid, "is_new": False, "method": "strong_id", "candidates": []}

    preferred_id = _preferred_strong_id(strong_ids)
    norm = normalize_name(name)

    # A name that normalizes to empty (e.g. a bare legal form) with no strong id is not
    # groundable to a canonical key — fail safe to a flagged unresolved node.
    if not norm and not preferred_id:
        if on_new == "mention":
            return _mention_result(name=name, kind=kind, norm=normalize_quote(name), candidates=[])
        return await _make_unresolved(store, name=name, kind=kind, tenant_id=tenant_id,
                                      source_key=source_key, norm=normalize_quote(name),
                                      candidates=[])

    candidates = await store.find_entities_by_norm(norm, kind, tenant_id=tenant_id) if norm else []

    # 2a) EXACT-NORM single hit → resolve.
    if len(candidates) == 1:
        eid = candidates[0]["entity_id"]
        await store.add_alias(name, eid, source=source_key)
        return {"entity_id": eid, "is_new": False, "method": "exact_norm",
                "candidates": candidates}

    # 2b) 0 candidates → NEW canonical entity (strong id gives a better global key). Under
    # on_new='mention' a name WITHOUT a strong id is NOT canonicalized (a bare name isn't an
    # identity) — it becomes a mention; only a strong id anchors a new canonical node.
    if not candidates:
        if not preferred_id and on_new == "mention":
            return _mention_result(name=name, kind=kind, norm=norm, candidates=[])
        new_id = preferred_id or f"{kind}:{norm}"
        await store.upsert_entity(new_id, kind=kind, name=name, tenant_id=tenant_id)
        await store.add_alias(name, new_id, source=source_key)
        return {"entity_id": new_id, "is_new": True, "method": "new", "candidates": []}

    # 2c) >1 candidates (AMBIGUOUS). A strong id is a definitive discriminator — none of the
    # name-collision candidates carry it (step 1 would have hit), so mint a confident NEW
    # entity at the strong id rather than risk a name-based merge.
    if preferred_id:
        await store.upsert_entity(preferred_id, kind=kind, name=name, tenant_id=tenant_id)
        await store.add_alias(name, preferred_id, source=source_key)
        return {"entity_id": preferred_id, "is_new": True, "method": "new",
                "candidates": candidates}

    # No strong id → the ambiguity is a SEMANTIC same-entity question (Rule 18): the LLM owns it.
    client = _client(llm)
    if client is not None:
        matched = None
        for cand in candidates:
            key = (norm, cand["entity_id"])
            if key in judge_cache:
                verdict = judge_cache[key]
            else:
                verdict = await _judge_same_entity(
                    client, name=name, kind=kind, context=context,
                    source_key=source_key, candidate=cand)
                judge_cache[key] = verdict
            if verdict and verdict["same"] and verdict["confidence"] >= _MERGE_CONFIDENCE:
                if matched is not None:
                    # confidently-same to TWO candidates — that is itself ambiguous; the
                    # fail-safe is to NOT merge (don't pick arbitrarily).
                    matched = None
                    break
                matched = cand
        if matched is not None:
            await store.add_alias(name, matched["entity_id"], source=source_key)
            return {"entity_id": matched["entity_id"], "is_new": False,
                    "method": "llm_merge", "candidates": candidates}

    # No client, judge error/uncertain, or ambiguous double-match → FAIL SAFE: never a false
    # merge. Under on_new='mention' the ambiguous name stays a MENTION (parked, not canonical);
    # legacy 'create' mints a flagged human-reviewable unresolved-candidate entity.
    if on_new == "mention":
        return _mention_result(name=name, kind=kind, norm=norm, candidates=candidates)
    return await _make_unresolved(store, name=name, kind=kind, tenant_id=tenant_id,
                                  source_key=source_key, norm=norm, candidates=candidates)


async def _make_unresolved(store, *, name: str, kind: str, tenant_id: str, source_key: str,
                           norm: str, candidates: list[dict]) -> dict:
    """Create a NEW entity flagged for human review (`facets.resolution='unresolved_candidate'`),
    carrying the candidate ids it could NOT be safely merged into. Id is deterministic per
    (kind, norm, source, name) so re-running the same mention is idempotent, yet two genuinely
    different mentions that merely share a norm never collide onto one node (never a false merge)."""
    disc = _sha(source_key, name)[:12]
    uid = f"{kind}:__unresolved:{norm or 'unknown'}:{disc}"
    facets = {
        "resolution": "unresolved_candidate",
        "unresolved_norm": norm,
        "unresolved_source": source_key,
        "candidate_ids": [c["entity_id"] for c in candidates],
    }
    await store.upsert_entity(uid, kind=kind, name=name, facets=facets, tenant_id=tenant_id)
    await store.add_alias(name, uid, source=source_key)
    return {"entity_id": uid, "is_new": True, "method": "unresolved", "candidates": candidates}


# --------------------------------------------------------------------------- #
# Conflict resolution                                                          #
# --------------------------------------------------------------------------- #
def _valid_bucket(valid_from) -> str:
    """The valid-time bucket for grouping conflicting claims: the ISO valid_from's YEAR, or
    '' for a static/unknown-time claim. Structural (a substring/attribute of the date)."""
    if not valid_from:
        return ""
    s = valid_from if isinstance(valid_from, str) else valid_from.isoformat()
    return s[:4] if len(s) >= 4 else ""


async def resolve_conflicts(store, *, subject_id: str, predicate: str, tenant_id: str,
                            authority_policy) -> list[dict]:
    """Resolve contradictory claims for one `(subject, predicate)`: group ACTIVE grounded
    claims by valid_bucket, pick the WINNER of each multi-claim bucket, and record it in
    `rs_claim_resolution` (the losers stay queryable — never retracted). Returns the
    resolutions written. Idempotent: re-running rewrites the same winner.

    Winner order (highest first):
      (a) a claim whose best evidence kind `authority_policy.is_controlling()` → wins; else
      (b) max evidence `authority_tier`; else
      (c) max `confidence`; else
      (d) latest `ingested_at`.

    `authority_policy` is INJECTED (the store stays vocabulary-neutral)."""
    claims = await store.subject_predicate_claims(subject_id, predicate, tenant_id=tenant_id)

    # Attach each claim's evidence signal; a claim with NO active evidence is ungrounded and
    # excluded from resolution (same grounding-exclusion discipline as the grounded reads).
    buckets: dict[str, list[dict]] = {}
    for c in claims:
        tiers = await store.claim_evidence_tiers(c["claim_id"])
        if not tiers:
            continue
        best_tier = max((int(t["authority_tier"]) for t in tiers), default=0)
        controlling = any(authority_policy.is_controlling(t["evidence_kind"] or "") for t in tiers)
        enriched = {**c, "best_tier": best_tier, "controlling": controlling}
        buckets.setdefault(_valid_bucket(c.get("valid_from")), []).append(enriched)

    written: list[dict] = []
    for bucket, group in buckets.items():
        if len(group) < 2:
            continue  # a single grounded claim is no conflict — nothing to resolve.
        # Deterministic ordering: controlling, then tier, then confidence, then recency, and
        # finally claim_id as a total-order tiebreak so idempotent re-runs pick the same winner.
        group.sort(key=_winner_key, reverse=True)
        winner = group[0]
        losers = group[1:]
        rationale = _rationale(winner)
        await store.record_resolution(
            subject_id=subject_id, predicate=predicate, valid_bucket=bucket,
            winning_claim_id=winner["claim_id"],
            conflict_claim_ids=[l["claim_id"] for l in losers],
            winner_authority_tier=winner["best_tier"], rationale=rationale,
            tenant_id=tenant_id)
        written.append({
            "subject_id": subject_id, "predicate": predicate, "valid_bucket": bucket,
            "winning_claim_id": winner["claim_id"],
            "conflict_claim_ids": [l["claim_id"] for l in losers],
            "winner_authority_tier": winner["best_tier"], "rationale": rationale,
        })
    return written


def _winner_key(c: dict):
    """Total-order winner key (higher wins): controlling > tier > confidence > recency >
    claim_id. `ingested_at` may be a datetime (DB) or None; fall back to a stable sentinel."""
    ing = c.get("ingested_at")
    ing_key = ing.timestamp() if hasattr(ing, "timestamp") else 0.0
    return (
        1 if c.get("controlling") else 0,
        int(c.get("best_tier") or 0),
        float(c.get("confidence") or 0.0),
        ing_key,
        c["claim_id"],
    )


def _rationale(winner: dict) -> str:
    if winner.get("controlling"):
        return "controlling evidence (authority policy is_controlling)"
    if winner.get("best_tier"):
        return f"highest evidence authority_tier={winner['best_tier']}"
    if winner.get("confidence"):
        return f"highest confidence={winner['confidence']}"
    return "most recent ingested_at"
