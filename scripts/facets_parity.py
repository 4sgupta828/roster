"""PARITY: the SQL facet store must agree with the kernel's reference semantics on the same rows
(docs/specs/facet-contract-evaluator.md §5 — the leaky-pool invariant and the count basis).

Runs in the API container:  python scripts/facets_parity.py --kind job --sample 300
For a random slice of entities it (1) loads their facet rows, (2) evaluates a handful of contracts through
the SQL adapter restricted to that slice, (3) re-evaluates them through InMemoryFacetStore over the same
rows, and reports any id that one side returns and the other does not, plus count differences."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, "/app/apps" if os.path.isdir("/app/apps") else os.path.join(os.path.dirname(__file__), "..", "apps"))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="job")
    ap.add_argument("--sample", type=int, default=300)
    args = ap.parse_args()
    import asyncpg
    from roster_kernel.facets import Contract, InMemoryFacetStore, count_rows, matches_must
    from roster_vertical.facet_schema import FACET_SCHEMA
    from api.facet_store import FacetSQLStore
    dsn = os.environ["ROSTER_CORPUS_DSN"]
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)

    async def getter():
        return pool
    store = FacetSQLStore(getter, FACET_SCHEMA)
    await store.ensure_schema()
    # a slice: the newest N entities WITH facet rows
    async with pool.acquire() as conn:
        if args.kind == "job":
            ids = [r["entity_id"] for r in await conn.fetch("SELECT DISTINCT entity_id FROM roster_entity_facet WHERE entity_kind='job' ORDER BY entity_id DESC LIMIT $1", args.sample)]
        else:
            ids = [r["entity_id"] for r in await conn.fetch("SELECT DISTINCT entity_id FROM roster_entity_facet WHERE entity_kind='person' LIMIT $1", args.sample)]
    rows = await store.enumerate(args.kind, {}, cap=10**6)
    rows = [r for r in rows if r["_eid"] in set(ids)]
    mem = InMemoryFacetStore(rows, FACET_SCHEMA)
    contracts = [{"level": ["leadership"]}, {"field": ["software"], "level": ["senior", "staff_plus"]}, {"work_mode": ["remote"]},
                 {"comp": {"min": 150000}}, {"company_type": ["startup"]}, {"skill": ["python"]}]
    bad = 0
    for must in contracts:
        sql_ids = {r["_eid"] for r in await store.enumerate(args.kind, must, cap=10**6) if r["_eid"] in set(ids)}
        mem_ids = {r["_eid"] for r in rows if matches_must(r, must, FACET_SCHEMA)}
        if sql_ids != mem_ids:
            bad += 1
            print(f"MISMATCH must={must}: sql-only={sorted(sql_ids - mem_ids)[:5]} mem-only={sorted(mem_ids - sql_ids)[:5]}")
        else:
            print(f"ok   must={must}: {len(sql_ids)} rows")
    # counts over the whole slice vs the reference (only keys both sides know)
    mem_counts = count_rows(rows, FACET_SCHEMA, args.kind)
    print("reference counts (slice):", {k: dict(list(v.items())[:4]) for k, v in mem_counts.items() if v})
    await pool.close()
    print("PARITY", "FAILED" if bad else "OK", f"({len(rows)} rows)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
