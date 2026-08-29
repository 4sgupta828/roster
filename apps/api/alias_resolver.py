"""LLM alias-resolver — collapse SURFACE-FORM variants of the same entity into ONE canonical node.

Structural normalization (`normalize_name`) already merges case / punctuation / legal-suffix variants,
but NOT semantic aliases: `a16z` == `Andreessen Horowitz`, `TTS` == `Text-to-Speech`. Deciding those are
the same real-world entity is a SEMANTIC judgment the LLM owns (Rule 18) — never a regex/keyword rule.

This module asks the LLM to CLUSTER the surface forms of one kind, then VALIDATES the clustering in code
(confidence gate + verbatim-member guard + cross-cluster dedup + canonical-is-a-member) so a false merge
can never pollute. FAIL-SAFE: no client, a judge error, low confidence, or any ambiguity → the name stays
its OWN singleton (an unmerged duplicate is safe; a wrong merge collapses two real entities and is not).

OUTPUT = an ALIAS MAP: for each accepted cluster, ONE canonical `rs_entity` (`<kind>:<canonical_norm>`)
plus an `rs_entity_alias` row (`member_norm -> canonical entity`) per member. The graph view remaps a
claim's `object_norm` through this map to the canonical node — behind the `ROSTER_ALIAS_RESOLVER` flag.

Mirrors `canonicalize.py`'s discipline (same `ENTAIL_MODEL` judge tier, same 0.8 confidence floor, same
fail-safe posture); it batches the same-entity question as a CLUSTERING to catch abbreviations a pairwise
token-blocker can't. The LLM is injectable for tests.
"""
from __future__ import annotations

import logging

from api.claimgraph import normalize_name
from roster_kernel.research.claims_first import ENTAIL_MODEL, _client, _json

_log = logging.getLogger(__name__)

# Correctness-critical semantic call → the stronger entail-tier model (same as canonicalize's judge).
CLUSTER_MODEL = ENTAIL_MODEL
# Only a confident cluster merges — anything below stays singletons (fail-safe).
_MIN_CLUSTER_CONFIDENCE = 0.8

_CLUSTER_SYSTEM = (
    "You are a strict entity-resolution judge for {KIND} names that appeared in grounded startup "
    "documents. You are given a LIST of surface-form names. GROUP ONLY the names that refer to the "
    "SAME real-world {KIND} — e.g. an abbreviation and its full name ('a16z' and 'Andreessen "
    "Horowitz'; 'TTS' and 'Text-to-Speech'). DO NOT group different organizations or things that "
    "merely share a word ('Accel' and 'Accel-KKR' are DIFFERENT funds; 'GPT-4' and 'GPT-4o' are "
    "DIFFERENT models). When unsure, leave a name in its OWN group. Output STRICT JSON: "
    '{{"clusters": [{{"canonical": "<the most standard/complete name, copied EXACTLY from the input>", '
    '"members": ["<exact input strings>"], "confidence": 0.0-1.0}}]}}. Include ONLY groups with 2+ '
    "members. Every member AND the canonical MUST be copied VERBATIM from the input list. Be conservative."
)


async def _cluster_call(client, kind: str, forms: list[str]) -> list:
    """One clustering LLM call. Returns the raw `clusters` list, or [] on any error (fail-safe)."""
    system = _CLUSTER_SYSTEM.format(KIND=kind)
    user = (f"{kind.upper()} SURFACE FORMS ({len(forms)}):\n"
            + "\n".join(f"- {f}" for f in forms) + "\n\nReturn ONLY the JSON.")
    try:
        raw = await _json(client, CLUSTER_MODEL, system, user)
    except Exception:  # noqa: BLE001 — any judge error → no merges
        _log.warning("alias_resolver: cluster call errored for kind=%s — no merges (fail-safe)", kind)
        return []
    if not isinstance(raw, dict):
        return []
    clusters = raw.get("clusters")
    return clusters if isinstance(clusters, list) else []


def validate_clusters(clusters: list, forms: list[str]) -> list[dict]:
    """CODE validation of the LLM clustering (Rule 8 — the provenance/anti-pollution guard):
      * confidence >= 0.8 (fail-safe floor);
      * every member + the canonical must be a VERBATIM input form (hallucination guard);
      * a member appearing in >1 cluster is AMBIGUOUS → dropped from every cluster (never guess);
      * canonical must be one of the surviving members (else pick the longest);
      * a cluster needs >= 2 distinct surviving members to merge anything.
    Returns accepted `[{canonical, members}]`. Pure/deterministic → unit-testable with a stub LLM."""
    fset = set(forms)
    prelim: list[dict] = []
    for c in clusters:
        if not isinstance(c, dict):
            continue
        try:
            conf = float(c.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < _MIN_CLUSTER_CONFIDENCE:
            continue
        members = [m for m in (c.get("members") or []) if isinstance(m, str) and m in fset]
        members = list(dict.fromkeys(members))                       # de-dup, keep order
        canonical = c.get("canonical")
        if canonical not in fset:
            canonical = max(members, key=len) if members else None    # structural fallback
        if canonical and len(members) >= 2:
            prelim.append({"canonical": canonical, "members": members})
    # cross-cluster dedup: a surface form assigned to two clusters is ambiguous → drop it everywhere
    from collections import Counter
    counts = Counter(m for c in prelim for m in c["members"])
    out: list[dict] = []
    for c in prelim:
        members = [m for m in c["members"] if counts[m] == 1]
        if c["canonical"] not in members and counts[c["canonical"]] == 1:
            members.append(c["canonical"])
        members = list(dict.fromkeys(members))
        if c["canonical"] in members and len(members) >= 2:
            out.append({"canonical": c["canonical"], "members": members})
    return out


async def cluster_kind_aliases(store, *, kind: str, predicates, tenant_id: str = "demo",
                               llm=None) -> list[dict]:
    """PROPOSE & VERIFY (judge-panel design). Cluster the surface forms of ONE kind (objects of
    `predicates`) as a cheap-RECALL candidate generator, then CONFIRM every proposed member against
    its cluster's canonical with the pairwise `_judge_same_entity` judge — the same battle-tested
    fail-safe primitive (`ENTAIL_MODEL`, >=0.8, None→no-merge) canonicalize.py uses — fed the member's
    real evidence quote as context (panel: never merge on bare strings). Only pairwise-CONFIRMED
    members are written as aliases (`member_norm -> canonical <kind>:<norm> entity`). Returns the
    applied clusters `[{kind, canonical, canonical_id, members}]`. Idempotent (upsert_entity + add_alias
    ON CONFLICT). FAIL-SAFE throughout: <2 forms, no client, a judge error, or an unconfirmed pair → the
    surface form stays its OWN node (an unmerged duplicate is safe; a wrong merge is not)."""
    from api.canonicalize import _judge_same_entity   # reuse the fail-safe pairwise judge
    forms = await store.distinct_object_forms(predicates, tenant_id=tenant_id)
    if len(forms) < 2:
        return []
    surfaces = [s for s, _, _ in forms]
    norm_of = {s: n for s, n, _ in forms}
    quote_of = {s: q for s, _, q in forms}
    client = _client(llm)
    if client is None:
        _log.warning("alias_resolver: no LLM client for kind=%s — no merges (fail-safe)", kind)
        return []
    candidates = validate_clusters(await _cluster_call(client, kind, surfaces), surfaces)
    applied: list[dict] = []
    for c in candidates:
        canonical = c["canonical"]
        confirmed = [canonical]
        for m in c["members"]:
            if m == canonical:
                continue
            verdict = await _judge_same_entity(
                client, name=m, kind=kind, context=(quote_of.get(m) or "")[:400],
                source_key="alias_resolver",
                candidate={"name": canonical,
                           "facets": {"evidence": (quote_of.get(canonical) or "")[:240]}})
            if verdict and verdict["same"] and verdict["confidence"] >= _MIN_CLUSTER_CONFIDENCE:
                confirmed.append(m)                       # pairwise-VERIFIED merge
        confirmed = list(dict.fromkeys(confirmed))
        if len(confirmed) < 2:
            continue                                       # nothing survived verification → no merge
        canonical_norm = norm_of.get(canonical) or normalize_name(canonical)
        canonical_id = f"{kind}:{canonical_norm}"
        await store.upsert_entity(canonical_id, kind=kind, name=canonical, tenant_id=tenant_id)
        for m in confirmed:
            await store.add_alias(m, canonical_id, source="alias_resolver")   # norm(m) -> canonical
        applied.append({"kind": kind, "canonical": canonical,
                        "canonical_id": canonical_id, "members": confirmed})
    return applied
