"""Integration tests for the Slice-1 edge model store primitives + end-to-end pathfinding.

DB round-trips: skipped unless ROSTER_CORPUS_DSN points at a Postgres (mirrors
test_claimgraph_integration.py), so the offline suite stays green without a DB.

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest apps/api/test_graph_path_integration.py -q

Verifies the parts the pure-logic unit tests (test_graph_path.py) cannot: that `neighbors`
returns BIDIRECTIONAL grounded edges with active-evidence citations, that `find_entity` resolves
by id and name, and that `find_paths` over the REAL store yields grounded connection paths. Uses
unique per-run ids/names and never truncates, so it is safe against a populated claim graph.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.claimgraph_tech import make_tech_claim_store
from api.graph_path import Edge, find_paths

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN for edge-model integration"),
]


def _uid(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


async def _seed(store):
    """Acme --has_founder--> Alice ; Beta --has_founder--> Alice ; Acme --compared_to--> Beta.
    Every edge carries one ACTIVE evidence row. Returns the ids + the unique Acme display name."""
    await store.ensure_schema()
    tag = uuid.uuid4().hex[:8]
    acme, beta = _uid("domain:acme"), _uid("domain:beta")
    alice = _uid("person:alice")
    acme_name = f"Acme {tag} Inc"
    await store.upsert_entity(acme, "company", acme_name, primary_domain=f"acme-{tag}.com")
    await store.upsert_entity(beta, "company", f"Beta {tag} Inc", primary_domain=f"beta-{tag}.com")
    await store.upsert_entity(alice, "person", f"Alice {tag}")
    for subj, pred, obj, norm in [
        (acme, "has_founder", alice, f"alice-{tag}"),
        (beta, "has_founder", alice, f"alice-{tag}"),
        (acme, "compared_to", beta, f"beta-{tag}"),
    ]:
        cl = await store.upsert_claim(subject_id=subj, predicate=pred, object_kind="entity",
                                      object_entity_id=obj, object_norm=norm, confidence=0.9)
        await store.add_evidence(cl, _uid("doc"), "b1", f"{subj} {pred} {obj}", authority_tier=3)
    return {"acme": acme, "beta": beta, "alice": alice, "acme_name": acme_name}


def test_neighbors_returns_bidirectional_grounded_edges():
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            ids = await _seed(store)
            # Acme (subject side): edges to Alice (has_founder) and Beta (compared_to).
            nb = await store.neighbors(ids["acme"])
            preds = {(e["predicate"], e["object_id"]) for e in nb}
            assert ("has_founder", ids["alice"]) in preds
            assert ("compared_to", ids["beta"]) in preds
            assert all(e["citation"]["quote"] for e in nb)  # grounded
            # Alice (OBJECT side): the reverse direction must surface too (bidirectional).
            nb_alice = await store.neighbors(ids["alice"])
            subjects = {e["subject_id"] for e in nb_alice}
            assert {ids["acme"], ids["beta"]} <= subjects
        finally:
            await store.close()
    asyncio.new_event_loop().run_until_complete(body())


def test_find_entity_resolves_by_id_and_name():
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            ids = await _seed(store)
            by_id = await store.find_entity(ids["acme"])
            assert by_id and by_id["entity_id"] == ids["acme"]
            by_name = await store.find_entity(ids["acme_name"])
            assert by_name and by_name["entity_id"] == ids["acme"]
            assert await store.find_entity(_uid("nope")) is None
        finally:
            await store.close()
    asyncio.new_event_loop().run_until_complete(body())


def test_find_paths_over_real_store_is_grounded():
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            ids = await _seed(store)

            async def neighbors(eid: str):
                rows = await store.neighbors(eid)
                return [Edge(subject_id=r["subject_id"], predicate=r["predicate"],
                             object_id=r["object_id"], claim_id=r["claim_id"],
                             citation=r["citation"]) for r in rows]

            paths = await find_paths(neighbors, ids["acme"], ids["beta"], max_depth=4)
            assert paths, "Acme and Beta are connected (compared_to, and via Alice)"
            shortest = min(paths, key=lambda p: len(p.hops))
            assert len(shortest.hops) == 1 and shortest.hops[0].predicate == "compared_to"
            # every hop of every returned path is grounded
            for p in paths:
                assert all(h.citation["quote"] for h in p.hops)
        finally:
            await store.close()
    asyncio.new_event_loop().run_until_complete(body())
