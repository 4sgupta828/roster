"""Integration tests for the people-population store core (facet write + enumerate_by_facets).

DB round-trips: skipped unless ROSTER_CORPUS_DSN is set (mirrors the other integration suites).

    ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5433/roster_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest apps/api/test_people_population_integration.py -q

Proves the enumeration contract that the failing prod query needs: multi-facet AND across keys, OR
within a key (seniority ∈ {director, engineering_manager}), grounding carried per facet, and honest
empty on an unmatched facet — using unique per-run ids so it is safe against a populated DB.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from api.claimgraph_tech import make_tech_claim_store

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN")]


def _uid(p: str) -> str:
    return f"{p}:{uuid.uuid4().hex[:12]}"


async def _seed_person(store, *, eid, name, seniority, function, metro, doc):
    await store.upsert_entity(eid, "person", name)
    for key, val, disp in [("seniority", seniority, seniority.title()),
                           ("function", function, function.replace("_", " ").title()),
                           ("metro", metro, metro.replace("_", " ").title())]:
        await store.add_person_facet(entity_id=eid, facet_key=key, facet_value_norm=val,
                                     display_value=disp, source_document_id=doc, source_block_id="b1",
                                     confidence=0.9)


def test_enumerate_by_facets_and_or_and_grounding():
    async def body():
        store = make_tech_claim_store(DSN)
        try:
            await store.ensure_schema()
            tag = uuid.uuid4().hex[:8]
            director = _uid("person:dir")   # Director, ML, Bay Area  -> MATCH
            em = _uid("person:em")          # Engineering Manager, ML, Bay Area -> MATCH (OR seniority)
            ic = _uid("person:ic")          # Staff (IC), ML, Bay Area -> NO (seniority not in set)
            nyc = _uid("person:nyc")        # Director, ML, NYC -> NO (metro)
            doc = f"https://acme.example/leadership#{tag}"
            await _seed_person(store, eid=director, name=f"Dana Director {tag}", seniority="director",
                               function="machine_learning", metro="bay_area", doc=doc)
            await _seed_person(store, eid=em, name=f"Evan EM {tag}", seniority="engineering_manager",
                               function="machine_learning", metro="bay_area", doc=doc)
            await _seed_person(store, eid=ic, name=f"Ivy IC {tag}", seniority="staff",
                               function="machine_learning", metro="bay_area", doc=doc)
            await _seed_person(store, eid=nyc, name=f"Nate NYC {tag}", seniority="director",
                               function="machine_learning", metro="nyc", doc=doc)

            facets = {"seniority": ["director", "engineering_manager"],
                      "function": ["machine_learning"], "metro": ["bay_area"]}
            rows = await store.enumerate_by_facets(facets, cap=100)
            got = {r["entity_id"] for r in rows}
            assert director in got and em in got, "Director and EM in ML in Bay Area must match"
            assert ic not in got, "a Staff IC must NOT match seniority∈{director,EM}"
            assert nyc not in got, "an NYC director must NOT match metro=bay_area"
            # grounding: every returned facet carries a citation (document + block)
            for r in rows:
                for f in r["facets"]:
                    assert f["document_id"] and f["block_id"], "facet must carry a grounding citation"

            # an unmatched facet → honest empty (no blanket dump)
            assert await store.enumerate_by_facets(
                {"seniority": ["director"], "function": ["quantum_computing"],
                 "metro": ["bay_area"]}, cap=100) == []
            assert await store.enumerate_by_facets({}, cap=100) == []

            stats = await store.people_index_stats()
            assert stats["persons_indexed"] >= 4 and "seniority" in stats["facet_coverage"]
        finally:
            await store.close()
    asyncio.new_event_loop().run_until_complete(body())
