"""Slice-2 edge model: person-identity unification (held-out, adversarial, offline).

The bug these tests pin (found by the design panel): a PERSON could be merged/collapsed by BARE
NAME — two different "John Smith" becoming one node — via the exact-norm single-hit merge in
`resolve_entity` and the legacy global `person:<norm>` mint. A name is NOT an identity for a
person; only a STRONG ID (github/orcid/linkedin/wikidata) or an evidence-based LLM same-entity
judgment may merge. This is the `name_is_identity=False` contract.

Companies keep the old behavior (`name_is_identity=True`, the default): an exact-norm single hit
still merges (legal-suffix-stripped company names are a reasonable identity signal). These tests
guard BOTH: persons never merge on bare name; companies still do.

Offline: a fake store + fake LLM, so the resolution LOGIC and fail-safe GATES are proven without a
DB (Rule 3 — proves the gates, not real merge quality).
"""
from __future__ import annotations

import asyncio

from api.canonicalize import resolve_entity


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def complete_json(self, *, model, system, user):
        self.calls.append((model, system, user))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Store:
    """Minimal in-memory stand-in for the resolve_entity store surface."""
    def __init__(self, entities=None, by_norm=None, aliases=None):
        self.entities = dict(entities or {})       # id -> {name, kind, status}
        self._by_norm = dict(by_norm or {})        # norm -> [candidate dicts]
        self.aliases = dict(aliases or {})
        self.upserts = []

    async def get_entity(self, entity_id, *, tenant_id="demo"):
        e = self.entities.get(entity_id)
        if e and e.get("status", "active") == "active":
            return {"entity_id": entity_id, "name": e["name"], "kind": e["kind"]}
        return None

    async def resolve_alias(self, alias):
        return self.aliases.get(alias)

    async def add_alias(self, alias, entity_id, source=""):
        self.aliases[alias] = entity_id

    async def find_entities_by_norm(self, norm, kind, *, tenant_id="demo"):
        return [c for c in self._by_norm.get(norm, []) if c.get("kind", kind) == kind]

    async def upsert_entity(self, entity_id, kind=None, name=None, *, facets=None,
                            tenant_id="demo", **kw):
        self.entities[entity_id] = {"name": name, "kind": kind, "status": "active"}
        self.upserts.append({"entity_id": entity_id, "kind": kind, "name": name})


def _person_candidate(eid="person:johnsmith"):
    """A store that already holds ONE canonical 'John Smith' person (exact-norm hit)."""
    return _Store(
        entities={eid: {"name": "John Smith", "kind": "person"}},
        by_norm={"john smith": [{"entity_id": eid, "name": "John Smith", "kind": "person"}]},
    )


def test_person_namesake_not_merged_by_bare_name():
    # A second bare "John Smith" (no strong id, no LLM) must NOT merge into the existing person.
    store = _person_candidate()
    r = _run(resolve_entity(store, name="John Smith", kind="person",
                            name_is_identity=False, on_new="mention"))
    assert r["method"] == "mention", f"bare-name person should park as a mention, got {r['method']}"
    assert r["entity_id"] is None, "a namesake must not resolve to the existing person's id"


def test_person_merges_across_sources_via_strong_id():
    # Same person seen twice with the same github strong id, different name surface forms → ONE node.
    store = _Store()
    r1 = _run(resolve_entity(store, name="John Smith", kind="person",
                             strong_ids={"github": "jsmith"}, name_is_identity=False,
                             on_new="mention"))
    assert r1["entity_id"] == "github:jsmith" and r1["is_new"] is True
    r2 = _run(resolve_entity(store, name="J. Smith", kind="person",
                             strong_ids={"github": "jsmith"}, name_is_identity=False,
                             on_new="mention"))
    assert r2["entity_id"] == "github:jsmith", "same strong id must resolve to the same node"
    assert r2["method"] == "strong_id" and r2["is_new"] is False


def test_person_with_strong_id_does_not_merge_into_namesake():
    # An existing bare 'John Smith' node must NOT swallow a DIFFERENT John Smith who carries a
    # distinguishing strong id — the strong id mints a fresh, correct node.
    store = _person_candidate()
    r = _run(resolve_entity(store, name="John Smith", kind="person",
                            strong_ids={"github": "jsmith2"}, name_is_identity=False,
                            on_new="mention"))
    assert r["entity_id"] == "github:jsmith2"
    assert r["entity_id"] != "person:johnsmith"


def test_company_exact_norm_still_merges():
    # REGRESSION GUARD: companies keep name_is_identity=True (default) → exact-norm single hit merges.
    store = _Store(
        entities={"domain:acme.com": {"name": "Acme Inc", "kind": "company"}},
        by_norm={"acme": [{"entity_id": "domain:acme.com", "name": "Acme Inc", "kind": "company"}]},
    )
    r = _run(resolve_entity(store, name="Acme, Inc.", kind="company", on_new="mention"))
    assert r["method"] == "exact_norm" and r["entity_id"] == "domain:acme.com"


def test_person_exact_norm_merges_only_with_confident_llm_and_context():
    # A person CAN still merge onto an exact-norm candidate — but only on EVIDENCE (a confident LLM
    # same-entity judgment given context), never on the bare name alone.
    store = _person_candidate()
    yes = _FakeLLM({"same": True, "confidence": 0.95, "reason": "same GitHub + employer"})
    r = _run(resolve_entity(store, name="John Smith", kind="person", name_is_identity=False,
                            on_new="mention", llm=yes,
                            context="John Smith, staff engineer at Acme, github.com/jsmith"))
    assert r["method"] == "llm_merge" and r["entity_id"] == "person:johnsmith"
    assert yes.calls, "the same-entity judge must have been consulted"

    # …and with NO llm the same bare name still parks (no silent name-merge).
    store2 = _person_candidate()
    r2 = _run(resolve_entity(store2, name="John Smith", kind="person", name_is_identity=False,
                             on_new="mention"))
    assert r2["method"] == "mention"
