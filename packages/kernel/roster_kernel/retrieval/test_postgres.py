"""Integration test for PostgresRetrievalSource (pgvector + tsvector).

Skipped unless ROSTER_TEST_PG_DSN points at a pgvector-enabled Postgres, so the
offline suite stays green without a DB. Mirrors the in-memory guarantees against
the real backend + checks ranking parity.

    ROSTER_TEST_PG_DSN=postgresql://strata:strata@localhost:5433/roster_test \
      .venv/bin/python -m pytest .../test_postgres.py -q

Each test runs its whole async body in ONE event loop (an asyncpg pool is bound
to the loop that created it).
"""
from __future__ import annotations

import asyncio
import os

import pytest

from roster_kernel.contract.dto import Locator, RetrievalRequest
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.research.provenance import BlockSpanVerifier
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.retrieval.postgres import PostgresRetrievalSource

DSN = os.environ.get("ROSTER_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ROSTER_TEST_PG_DSN for pg integration")

DIM = 16
_EMB = FakeEmbedder(dim=DIM)
TABLE = "rs_block_test"

_ANSWER = "The committee approved a target value of nine point six percent for the group in this matter."
_DECOY = "Target Value"
BLOCKS = [
    ("ans", "A", "d1", _ANSWER, {"region": "north", "year": "2024"}),
    ("dec", "A", "d1", _DECOY, {"region": "north", "year": "2024"}),
    ("in", "A", "d2", "A southern matter about something else entirely.", {"region": "south", "year": "2023"}),
    ("secret", "B", "dB", "Tenant B private: the code is 4242.", {"region": "north", "year": "2024"}),
]


async def _fresh_source() -> PostgresRetrievalSource:
    src = PostgresRetrievalSource(DSN, dim=DIM, table=TABLE, covers={"region": ("north", "south")})
    await src.ensure_schema()
    pool = await src._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {TABLE}")
    for bid, tenant, doc, text, facets in BLOCKS:
        await src.upsert_block(tenant_id=tenant, document_id=doc, block_id=bid, text=text,
                               embedding=_EMB.embed([text])[0], facets=facets,
                               document_title=f"Doc {doc}", content_type="text/markdown",
                               source_key="reg")
    return src


async def _search(src, *, query, **kw):
    qv = list(_EMB.embed([query])[0])
    return await src.search(RetrievalRequest(query=query, query_embedding=qv, **kw))


def test_pg_tenant_isolation() -> None:
    async def body():
        src = await _fresh_source()
        try:
            hits = await _search(src, query="target value", tenant_id="A", k=10)
            ids = {h.block_id for h in hits}
            assert "ans" in ids
            assert "secret" not in ids          # tenant B never leaks to A
        finally:
            await src.close()
    asyncio.run(body())


def test_pg_facet_filter_and_ranking() -> None:
    async def body():
        src = await _fresh_source()
        try:
            # hybrid search: facet narrows to north/2024 (excludes the south/2023 doc)
            hits = await _search(src, query="approved target value", tenant_id="A",
                                 facets={"region": "north", "year": "2024"}, k=10)
            assert "in" not in {h.block_id for h in hits}
            # ranking quality is a LEXICAL property — assert it on the deterministic
            # BM25+signal path (FakeEmbedder's dense leg is meaningless noise; real
            # embeddings would help, not hurt). No query_embedding → lexical only.
            lex = await src.search(RetrievalRequest(
                query="approved target value", tenant_id="A",
                facets={"region": "north", "year": "2024"}, k=10))
            ids = [h.block_id for h in lex]
            assert ids[0] == "ans"                         # BM25 len-norm + signal beat the stub
            assert ids.index("ans") < ids.index("dec")
            hits2 = await _search(src, query="matter", tenant_id="A",
                                  facets={"year": ("2023", "2024")}, k=10)
            assert {h.block_id for h in hits2} & {"in"}    # IN-set facet membership
        finally:
            await src.close()
    asyncio.run(body())


def test_pg_block_loader_and_provenance() -> None:
    async def body():
        src = await _fresh_source()
        try:
            await _search(src, query="target value", tenant_id="A", k=10)  # fills loader cache
            v_a = BlockSpanVerifier(src.make_block_loader("A"))
            loc = Locator("block_span", "d1", {"block_id": "ans"})
            assert v_a.verify("approved a target value of nine point six percent", loc)
            assert not v_a.verify("a target value of twelve percent", loc)  # fabricated
            assert not v_a.verify("the code is 4242",
                                  Locator("block_span", "dB", {"block_id": "secret"}))  # cross-tenant
        finally:
            await src.close()
    asyncio.run(body())


def test_pg_parity_with_in_memory() -> None:
    async def body():
        src = await _fresh_source()
        try:
            pg = [h.block_id for h in await _search(
                src, query="approved target value", tenant_id="A", k=5)]
        finally:
            await src.close()
        mem = InMemoryRetrievalSource()
        for bid, tenant, doc, text, facets in BLOCKS:
            mem.add(IndexedBlock(block_id=bid, document_id=doc, text=text, tenant_id=tenant,
                                 embedding=tuple(_EMB.embed([text])[0]), facets=facets))
        qv = list(_EMB.embed(["approved target value"])[0])
        mm = [h.block_id for h in await mem.search(RetrievalRequest(
            query="approved target value", tenant_id="A", query_embedding=qv, k=5))]
        assert pg[0] == mm[0]                              # same top result → ranking parity
    asyncio.run(body())
