#!/usr/bin/env python3
"""FRESH tech-knowledge-graph — P1: one-company CLEAN accretion (zero-pollution contract).

Seeds ONE company as a canonical entity (from its domain = a STRONG id), then runs the
multi-source enrichment (news + EDGAR Form-D) through the MENTION-FIRST contract
(`object_policy='mention'`): every object (investor/tech/category named in the evidence) is
either promoted to a canonical edge on a real identity anchor, or degraded to a grounded VALUE
claim + parked in the `rs_mention` lane — NEVER soft-minted as a polluted `<kind>:<norm>` node.

CREDIT DISCIPLINE: **dry-run by DEFAULT** (projects cost, spends nothing). `--live` spends (capped
by the pipeline). Use a FRESH `--tenant` so the clean graph never mixes with the discarded data.

Usage:
  ROSTER_CORPUS_DSN=postgresql://…/roster  python scripts/run_fresh_graph.py \
      --domain elevenlabs.io --name "ElevenLabs" --tenant fresh1                 # dry run
  ROSTER_CORPUS_DSN=…  python scripts/run_fresh_graph.py --domain … --name … --tenant fresh1 --live
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


async def _run(a) -> int:
    dsn = os.environ.get("ROSTER_CORPUS_DSN", "")
    if not dsn:
        print("set ROSTER_CORPUS_DSN", file=sys.stderr)
        return 2
    from api.canonicalize import resolve_entity
    from api.claimgraph_tech import make_tech_claim_store
    from api.enrich_sources import enrich_one

    store = make_tech_claim_store(dsn)
    try:
        # 1) SEED the company as a CANONICAL entity from its domain (a strong id → real identity).
        seed = await resolve_entity(store, name=a.name, kind="company",
                                    strong_ids={"domain": a.domain}, tenant_id=a.tenant,
                                    source_key="seed", on_new="mention")
        company_id = seed["entity_id"]
        print(f"seed: {a.name} -> {company_id} ({seed['method']})")
        if company_id is None:
            print("seed did not resolve to a canonical id (needs a strong id)", file=sys.stderr)
            return 1

        # 2) CLEAN accretion: enrich through the mention-first contract.
        summary = await enrich_one(company_id, a.name, dsn, dry_run=not a.live,
                                   tenant_id=a.tenant, object_policy="mention")
        print("enrich summary:", json.dumps(summary, default=str)[:600])

        # 3) Show the accretion — clean by construction.
        pool = await store._get_pool()
        async with pool.acquire() as conn:
            ent = await conn.fetch(
                "SELECT kind, count(*) n FROM rs_entity WHERE tenant_id=$1 GROUP BY kind ORDER BY kind",
                a.tenant)
            n_mention = await conn.fetchval(
                "SELECT count(*) FROM rs_mention WHERE tenant_id=$1", a.tenant)
            n_claim = await conn.fetchval(
                "SELECT count(*) FROM rs_claim WHERE tenant_id=$1", a.tenant)
            polluted = await conn.fetchval(
                "SELECT count(*) FROM rs_entity WHERE tenant_id=$1 AND "
                "(entity_id LIKE '%__unresolved:%' OR entity_id LIKE 'category:%')", a.tenant)
        print("entities:", {r["kind"]: r["n"] for r in ent})
        print(f"mentions: {n_mention}   claims: {n_claim}   POLLUTED object nodes: {polluted}")
        print("CLEAN" if polluted == 0 else "POLLUTION DETECTED")
        return 0 if polluted == 0 else 1
    finally:
        await store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--domain", required=True, help="company's own domain (the strong id) e.g. elevenlabs.io")
    ap.add_argument("--name", required=True, help="company display name")
    ap.add_argument("--tenant", default="fresh", help="FRESH graph namespace (keep separate from legacy)")
    ap.add_argument("--live", action="store_true", help="ACTUALLY spend (default: dry run, no spend)")
    a = ap.parse_args()
    return asyncio.run(_run(a))


if __name__ == "__main__":
    raise SystemExit(main())
