"""Offline tests (NO DB, NO network) for the canonicalization CORE (Task S2a).

Two primitives, tested with a FAKE store + FAKE LLM so the whole suite stays offline:

  * `resolve_entity` — all six resolution paths (strong-id, exact-norm single, new,
    llm-merge, llm-uncertain→unresolved, no-llm→unresolved fail-safe) PLUS the strong-id
    disambiguation of an otherwise-ambiguous name.
  * `resolve_conflicts` — winner selection (controlling → tier → confidence → recency) and
    that the loser is recorded, never dropped.
  * `resolved_entity_claims` — the winner-preferred read ASSEMBLY (winner returned with
    alternates attached; unresolved groups pass through; a missing winner falls back to the
    whole group). Exercised via a `ClaimGraphStore` subclass that stubs the two DB fetches,
    so the REAL assembly logic runs offline.

WHAT THIS PROVES vs NOT (Rule 3): a fake LLM proves the resolution LOGIC and the fail-safe
GATES (never a false merge; uncertain/no-client → unresolved), and the winner ORDERING and
append-only recording. It proves NOTHING about real same-entity merge QUALITY — that is the
live-validation concern of S2c. The DB round-trips live in `test_canonicalize_integration.py`.
"""
from __future__ import annotations

import asyncio

import pytest

from api.canonicalize import resolve_conflicts, resolve_entity
from api.claimgraph import ClaimGraphStore, normalize_name
from roster_vertical.authority import TechAuthorityPolicy


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
class _FakeLLM:
    """Records every complete_json call; returns a scripted verdict (or raises). A `payload`
    that is a LIST is consumed in call order (one verdict per candidate); a single dict is
    returned for every call; an Exception is raised."""
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str, str]] = []

    async def complete_json(self, *, model, system, user):
        self.calls.append((model, system, user))
        if isinstance(self.payload, Exception):
            raise self.payload
        if isinstance(self.payload, list):
            return self.payload[min(len(self.calls) - 1, len(self.payload) - 1)]
        return self.payload


class _FakeEntityStore:
    """In-memory stand-in for the resolve_entity store surface."""
    def __init__(self, *, entities=None, aliases=None, by_norm=None):
        self.entities: dict = dict(entities or {})       # id -> {name, kind, status}
        self.aliases: dict = dict(aliases or {})         # raw-key -> entity_id
        self._by_norm: dict = dict(by_norm or {})        # norm -> [candidate dicts]
        self.added_aliases: list = []
        self.upserts: list = []

    async def get_entity(self, entity_id, *, tenant_id="demo"):
        e = self.entities.get(entity_id)
        if e and e.get("status", "active") == "active":
            return {"entity_id": entity_id, "name": e["name"], "kind": e["kind"]}
        return None

    async def resolve_alias(self, alias):
        return self.aliases.get(alias)

    async def add_alias(self, alias, entity_id, source=""):
        self.added_aliases.append((alias, entity_id, source))

    async def find_entities_by_norm(self, norm, kind, *, tenant_id="demo"):
        return [c for c in self._by_norm.get(norm, []) if c.get("kind", kind) == kind]

    async def upsert_entity(self, entity_id, kind=None, name=None, *, facets=None,
                            tenant_id="demo", **kw):
        self.entities[entity_id] = {"name": name, "kind": kind, "status": "active",
                                    "facets": facets or {}}
        self.upserts.append({"entity_id": entity_id, "kind": kind, "name": name,
                             "facets": facets or {}})
        return entity_id


class _FakeConflictStore:
    """In-memory stand-in for the resolve_conflicts store surface."""
    def __init__(self, claims, tiers):
        self._claims = claims
        self._tiers = tiers                       # claim_id -> [{authority_tier, evidence_kind}]
        self.resolutions: list = []

    async def subject_predicate_claims(self, subject_id, predicate, *, tenant_id="demo"):
        return list(self._claims)

    async def claim_evidence_tiers(self, claim_id):
        return list(self._tiers.get(claim_id, []))

    async def record_resolution(self, *, subject_id, predicate, valid_bucket, winning_claim_id,
                                conflict_claim_ids, winner_authority_tier, rationale,
                                tenant_id="demo"):
        self.resolutions.append({
            "subject_id": subject_id, "predicate": predicate, "valid_bucket": valid_bucket,
            "winning_claim_id": winning_claim_id,
            "conflict_claim_ids": list(conflict_claim_ids),
            "winner_authority_tier": winner_authority_tier, "rationale": rationale,
        })


# --------------------------------------------------------------------------- #
# resolve_entity — the six paths                                              #
# --------------------------------------------------------------------------- #
def test_strong_id_hit_resolves_to_existing_entity() -> None:
    store = _FakeEntityStore(entities={"cik:12345": {"name": "OpenAI", "kind": "company"}})
    r = asyncio.run(resolve_entity(store, name="OpenAI Global", kind="company",
                                   strong_ids={"cik": "12345"}, source_key="edgar"))
    assert r == {"entity_id": "cik:12345", "is_new": False, "method": "strong_id",
                 "candidates": []}
    # the mention name is registered as an alias of the canonical entity
    assert ("OpenAI Global", "cik:12345", "edgar") in store.added_aliases


def test_strong_id_via_existing_alias() -> None:
    # the strong-id STRING was previously registered as an alias of a canonical entity.
    store = _FakeEntityStore(entities={"yc:openai": {"name": "OpenAI", "kind": "company"}},
                             aliases={"cik:12345": "yc:openai"})
    r = asyncio.run(resolve_entity(store, name="OpenAI", kind="company",
                                   strong_ids={"cik": "12345"}))
    assert r["entity_id"] == "yc:openai" and r["method"] == "strong_id"


def test_exact_norm_single_hit_adds_alias() -> None:
    store = _FakeEntityStore(by_norm={"acme": [{"entity_id": "yc:acme", "name": "Acme, Inc.",
                                                 "kind": "company", "facets": {}}]})
    r = asyncio.run(resolve_entity(store, name="ACME  Inc", kind="company", source_key="news"))
    assert r["method"] == "exact_norm" and r["entity_id"] == "yc:acme" and r["is_new"] is False
    assert ("ACME  Inc", "yc:acme", "news") in store.added_aliases


def test_zero_candidates_creates_new_entity() -> None:
    store = _FakeEntityStore()
    r = asyncio.run(resolve_entity(store, name="Brand New Co", kind="company"))
    assert r["method"] == "new" and r["is_new"] is True
    assert r["entity_id"] == "company:" + normalize_name("Brand New Co")
    assert any(u["entity_id"] == r["entity_id"] for u in store.upserts)


def test_zero_candidates_with_strong_id_uses_strong_id_key() -> None:
    store = _FakeEntityStore()
    r = asyncio.run(resolve_entity(store, name="Acme", kind="company",
                                   strong_ids={"domain": "Acme.com"}))
    assert r["method"] == "new" and r["entity_id"] == "domain:acme.com"


def test_ambiguous_llm_confident_same_merges() -> None:
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    # judge says SAME+confident for the FIRST candidate, NOT-same for the second (exactly one
    # confident match → a clean merge).
    llm = _FakeLLM([{"same": True, "confidence": 0.95, "why": "same domain + founders"},
                    {"same": False, "confidence": 0.1, "why": "different company"}])
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", context="AI infra",
                                   llm=llm))
    assert r["method"] == "llm_merge" and r["entity_id"] == "yc:apex-a" and r["is_new"] is False
    assert llm.calls, "the LLM judge must have been consulted for the ambiguous case"


def test_ambiguous_llm_confident_same_to_TWO_candidates_is_unresolved() -> None:
    # if the judge is confidently SAME to MORE THAN ONE candidate, that is itself ambiguous —
    # fail safe: do NOT pick one arbitrarily; keep separate + flag.
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    llm = _FakeLLM({"same": True, "confidence": 0.95})   # same for BOTH candidates
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=llm))
    assert r["method"] == "unresolved"


# --------------------------------------------------------------------------- #
# MENTION-FIRST ER (on_new='mention') — the fresh-graph zero-pollution contract #
# --------------------------------------------------------------------------- #
def test_mention_bare_name_no_strong_id_returns_mention_and_mints_nothing() -> None:
    # A bare name with no strong id and no existing match is NOT an identity → it must become a
    # MENTION, never a canonical entity (no duplicate-spawn). This is the core pollution fix.
    store = _FakeEntityStore()
    r = asyncio.run(resolve_entity(store, name="Sequoia Capital", kind="investor",
                                   on_new="mention"))
    assert r["method"] == "mention" and r["entity_id"] is None
    assert r["mention"]["name"] == "Sequoia Capital" and r["mention"]["kind"] == "investor"
    assert store.upserts == [], "a mention must mint NO canonical rs_entity row"


def test_mention_strong_id_still_promotes_to_canonical() -> None:
    # a STRONG id is a real identity anchor → promote to canonical even under mention-first.
    store = _FakeEntityStore()
    r = asyncio.run(resolve_entity(store, name="Acme", kind="company",
                                   strong_ids={"domain": "Acme.com"}, on_new="mention"))
    assert r["method"] == "new" and r["entity_id"] == "domain:acme.com" and r["entity_id"]


def test_mention_exact_norm_single_still_promotes() -> None:
    # an exact match to ONE existing canonical entity is a real identity → promote (not a mention).
    store = _FakeEntityStore(by_norm={"acme": [{"entity_id": "domain:acme.com", "name": "Acme",
                                                "kind": "company", "facets": {}}]})
    r = asyncio.run(resolve_entity(store, name="ACME", kind="company", on_new="mention"))
    assert r["method"] == "exact_norm" and r["entity_id"] == "domain:acme.com"


def test_mention_ambiguous_no_confident_merge_returns_mention_not_canonical() -> None:
    # namesakes with no strong id and no confident LLM merge → MENTION (never a false merge,
    # never a flagged canonical row). The name waits for stronger evidence.
    cands = [{"entity_id": "domain:apex-a.com", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "domain:apex-b.io", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=None,
                                   on_new="mention"))
    assert r["method"] == "mention" and r["entity_id"] is None
    assert store.upserts == []


def test_mention_ambiguous_confident_merge_still_promotes() -> None:
    cands = [{"entity_id": "domain:apex-a.com", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "domain:apex-b.io", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    llm = _FakeLLM([{"same": True, "confidence": 0.95}, {"same": False, "confidence": 0.1}])
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=llm, on_new="mention"))
    assert r["method"] == "llm_merge" and r["entity_id"] == "domain:apex-a.com"


def test_create_default_is_byte_identical_legacy() -> None:
    # on_new default ('create') is unchanged: a bare name still mints a canonical entity.
    store = _FakeEntityStore()
    r = asyncio.run(resolve_entity(store, name="Brand New Co", kind="company"))
    assert r["method"] == "new" and r["entity_id"] == "company:" + normalize_name("Brand New Co")
    assert any(u["entity_id"] == r["entity_id"] for u in store.upserts)


def test_leak_fabricated_name_never_becomes_canonical() -> None:
    # THE LEAK TEST (in miniature): a fabricated/model-seeded name run through the mention-first
    # gate mints NO canonical entity — it can only ever be a mention. Views (which read only
    # canonical grounded entities) can therefore never surface it.
    store = _FakeEntityStore()
    r = asyncio.run(resolve_entity(store, name="Totally Fake Ghostcorp XYZ", kind="company",
                                   on_new="mention"))
    assert r["entity_id"] is None and r["method"] == "mention"
    assert store.upserts == [] and store.added_aliases == []


def test_ambiguous_llm_uncertain_creates_flagged_unresolved() -> None:
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    llm = _FakeLLM({"same": False, "confidence": 0.2, "why": "different sectors"})
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", source_key="news",
                                   llm=llm))
    assert r["method"] == "unresolved" and r["is_new"] is True
    up = next(u for u in store.upserts if u["entity_id"] == r["entity_id"])
    assert up["facets"]["resolution"] == "unresolved_candidate"
    assert up["facets"]["candidate_ids"] == ["yc:apex-a", "yc:apex-b"]


def test_ambiguous_llm_same_but_below_threshold_is_unresolved() -> None:
    # SAME but confidence < 0.8 must NOT merge (fail-safe threshold).
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    llm = _FakeLLM({"same": True, "confidence": 0.6, "why": "maybe"})
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=llm))
    assert r["method"] == "unresolved"


def test_ambiguous_no_llm_is_unresolved_failsafe(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)   # ensure _client(None) → None
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=None))
    assert r["method"] == "unresolved" and r["is_new"] is True   # NEVER a false merge


def test_ambiguous_judge_error_is_unresolved_failsafe() -> None:
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    llm = _FakeLLM(RuntimeError("judge boom"))
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=llm))
    assert r["method"] == "unresolved"   # judge error → keep separate + flag


def test_ambiguous_with_strong_id_disambiguates_without_llm() -> None:
    # a strong id is a definitive discriminator — ambiguous name resolves to a NEW node at
    # the strong id, never a name-based LLM merge.
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    llm = _FakeLLM({"same": True, "confidence": 0.99})
    r = asyncio.run(resolve_entity(store, name="Apex", kind="company",
                                   strong_ids={"cik": "999"}, llm=llm))
    assert r["method"] == "new" and r["entity_id"] == "cik:999"
    assert llm.calls == []   # the LLM was NOT consulted — the strong id decided


def test_judge_is_cached_by_norm_and_candidate() -> None:
    cands = [{"entity_id": "yc:apex-a", "name": "Apex", "kind": "company", "facets": {}},
             {"entity_id": "yc:apex-b", "name": "Apex", "kind": "company", "facets": {}}]
    store = _FakeEntityStore(by_norm={"apex": cands})
    llm = _FakeLLM({"same": False, "confidence": 0.1})
    cache: dict = {}
    asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=llm, judge_cache=cache))
    n_first = len(llm.calls)
    asyncio.run(resolve_entity(store, name="Apex", kind="company", llm=llm, judge_cache=cache))
    assert len(llm.calls) == n_first, "cached (norm, candidate) verdicts must not re-call the LLM"


# --------------------------------------------------------------------------- #
# resolve_conflicts — winner selection                                        #
# --------------------------------------------------------------------------- #
_POLICY = TechAuthorityPolicy()


def _claim(cid, *, conf=0.9, valid_from=None):
    return {"claim_id": cid, "object_kind": "value", "object_value": cid, "object_norm": cid,
            "object_entity_id": "", "confidence": conf, "valid_from": valid_from,
            "ingested_at": None}


def test_controlling_evidence_wins_even_over_a_peer() -> None:
    # Two claims, same static bucket: one cited by primary_filing (controlling), one by
    # analysis. The controlling one wins; the loser is recorded, never dropped.
    store = _FakeConflictStore(
        claims=[_claim("c_analysis", conf=0.99), _claim("c_filing", conf=0.5)],
        tiers={"c_analysis": [{"authority_tier": 4, "evidence_kind": "analysis"}],
               "c_filing": [{"authority_tier": 6, "evidence_kind": "primary_filing"}]})
    out = asyncio.run(resolve_conflicts(store, subject_id="yc:acme",
                                        predicate="raised_funding", tenant_id="demo",
                                        authority_policy=_POLICY))
    assert len(out) == 1
    res = out[0]
    assert res["winning_claim_id"] == "c_filing"
    assert res["conflict_claim_ids"] == ["c_analysis"]
    assert "controlling" in res["rationale"]
    assert store.resolutions[0]["winning_claim_id"] == "c_filing"   # actually recorded


def test_higher_authority_tier_wins_when_neither_controlling() -> None:
    store = _FakeConflictStore(
        claims=[_claim("c_low", conf=0.99), _claim("c_high", conf=0.5)],
        tiers={"c_low": [{"authority_tier": 4, "evidence_kind": "analysis"}],
               "c_high": [{"authority_tier": 5, "evidence_kind": "verified_structured"}]})
    out = asyncio.run(resolve_conflicts(store, subject_id="yc:acme", predicate="headcount",
                                        tenant_id="demo", authority_policy=_POLICY))
    assert out[0]["winning_claim_id"] == "c_high"
    assert out[0]["winner_authority_tier"] == 5
    assert "authority_tier" in out[0]["rationale"]


def test_confidence_breaks_tie_at_equal_tier() -> None:
    store = _FakeConflictStore(
        claims=[_claim("c_lo", conf=0.4), _claim("c_hi", conf=0.8)],
        tiers={"c_lo": [{"authority_tier": 4, "evidence_kind": "analysis"}],
               "c_hi": [{"authority_tier": 4, "evidence_kind": "analysis"}]})
    out = asyncio.run(resolve_conflicts(store, subject_id="s", predicate="p", tenant_id="demo",
                                        authority_policy=_POLICY))
    assert out[0]["winning_claim_id"] == "c_hi"


def test_claims_split_into_separate_valid_buckets() -> None:
    # different valid_from YEARS are different buckets — NOT a conflict, so no resolution.
    store = _FakeConflictStore(
        claims=[_claim("c2024", valid_from="2024-01-01"),
                _claim("c2025", valid_from="2025-06-01")],
        tiers={"c2024": [{"authority_tier": 5, "evidence_kind": "verified_structured"}],
               "c2025": [{"authority_tier": 5, "evidence_kind": "verified_structured"}]})
    out = asyncio.run(resolve_conflicts(store, subject_id="s", predicate="headcount",
                                        tenant_id="demo", authority_policy=_POLICY))
    assert out == []   # one claim per bucket → nothing to resolve


def test_ungrounded_claim_excluded_from_resolution() -> None:
    # a claim with no active evidence is ungrounded → not counted; leaves a single grounded
    # claim in the bucket → no conflict.
    store = _FakeConflictStore(
        claims=[_claim("c_grounded"), _claim("c_ungrounded")],
        tiers={"c_grounded": [{"authority_tier": 4, "evidence_kind": "analysis"}]})
    out = asyncio.run(resolve_conflicts(store, subject_id="s", predicate="p", tenant_id="demo",
                                        authority_policy=_POLICY))
    assert out == []


def test_resolution_is_idempotent() -> None:
    store = _FakeConflictStore(
        claims=[_claim("c_low", conf=0.99), _claim("c_high", conf=0.5)],
        tiers={"c_low": [{"authority_tier": 4, "evidence_kind": "analysis"}],
               "c_high": [{"authority_tier": 6, "evidence_kind": "primary_filing"}]})
    a = asyncio.run(resolve_conflicts(store, subject_id="s", predicate="p", tenant_id="demo",
                                      authority_policy=_POLICY))
    b = asyncio.run(resolve_conflicts(store, subject_id="s", predicate="p", tenant_id="demo",
                                      authority_policy=_POLICY))
    assert a[0]["winning_claim_id"] == b[0]["winning_claim_id"] == "c_high"


# --------------------------------------------------------------------------- #
# resolved_entity_claims — winner-preferred read ASSEMBLY (real logic, stubbed DB) #
# --------------------------------------------------------------------------- #
class _FakeReadStore(ClaimGraphStore):
    """Runs the REAL `resolved_entity_claims` assembly with the two DB fetches stubbed."""
    def __init__(self, claims, resolutions):
        self._claims = claims
        self._res = resolutions

    async def ensure_schema(self):
        return None

    async def entity_claims(self, subject_id, *, predicates=None, tenant_id="demo"):
        return list(self._claims)

    async def _fetch_resolutions(self, subject_id, predicates, tenant_id):
        return dict(self._res)


def _rc(cid, pred, *, val="v", valid_from=None):
    return {"entity_id": "s", "name": "", "kind": "", "primary_domain": "",
            "claim_id": cid, "predicate": pred, "object_kind": "value", "object_value": val,
            "object_norm": val, "object_entity_id": "", "confidence": 0.9, "unit": "",
            "valid_from": valid_from, "valid_to": None,
            "evidence": {"document_id": "d", "block_id": "b", "quote": "q", "authority_tier": 5}}


def test_winner_preferred_read_returns_winner_with_alternates() -> None:
    claims = [_rc("c_win", "raised_funding", val="$12M"),
              _rc("c_lose", "raised_funding", val="$10M")]
    res = {("raised_funding", ""): {"predicate": "raised_funding", "valid_bucket": "",
                                    "winning_claim_id": "c_win", "winner_authority_tier": 6,
                                    "rationale": "controlling evidence"}}
    out = asyncio.run(_FakeReadStore(claims, res).resolved_entity_claims("s"))
    assert len(out) == 1
    w = out[0]
    assert w["claim_id"] == "c_win" and w["is_resolved"] is True
    assert w["winner_authority_tier"] == 6 and w["rationale"] == "controlling evidence"
    assert [a["claim_id"] for a in w["alternates"]] == ["c_lose"]   # loser kept VISIBLE
    assert w["alternates"][0]["object_value"] == "$10M"


def test_winner_preferred_read_passes_unresolved_group_through() -> None:
    claims = [_rc("c_a", "offers_product", val="A"), _rc("c_b", "offers_product", val="B")]
    out = asyncio.run(_FakeReadStore(claims, {}).resolved_entity_claims("s"))
    assert {c["claim_id"] for c in out} == {"c_a", "c_b"}   # no resolution → all claims
    assert all("is_resolved" not in c for c in out)


def test_winner_preferred_read_falls_back_when_winner_missing() -> None:
    # a resolution names a winner whose claim is no longer grounded/queryable → surface the
    # whole group rather than hide it (fail-safe visibility).
    claims = [_rc("c_a", "headcount", val="50"), _rc("c_b", "headcount", val="100")]
    res = {("headcount", ""): {"predicate": "headcount", "valid_bucket": "",
                               "winning_claim_id": "c_gone", "winner_authority_tier": 5,
                               "rationale": "x"}}
    out = asyncio.run(_FakeReadStore(claims, res).resolved_entity_claims("s"))
    assert {c["claim_id"] for c in out} == {"c_a", "c_b"}
    assert all("is_resolved" not in c for c in out)


def test_winner_preferred_read_buckets_by_year() -> None:
    # two claims, same predicate, DIFFERENT years → two groups; a resolution for the 2025
    # bucket resolves only that one.
    claims = [_rc("c24", "headcount", val="50", valid_from="2024-01-01"),
              _rc("c25a", "headcount", val="90", valid_from="2025-01-01"),
              _rc("c25b", "headcount", val="100", valid_from="2025-07-01")]
    res = {("headcount", "2025"): {"predicate": "headcount", "valid_bucket": "2025",
                                   "winning_claim_id": "c25b", "winner_authority_tier": 6,
                                   "rationale": "controlling"}}
    out = asyncio.run(_FakeReadStore(claims, res).resolved_entity_claims("s"))
    by = {c["claim_id"]: c for c in out}
    assert "c24" in by and "c24" not in [a for c in out for a in c.get("alternates", [])]
    assert by["c25b"]["is_resolved"] is True
    assert [a["claim_id"] for a in by["c25b"]["alternates"]] == ["c25a"]
    assert "c25a" not in by   # the loser is an alternate, not a top-level row


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
