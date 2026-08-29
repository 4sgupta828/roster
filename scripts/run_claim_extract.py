#!/usr/bin/env python3
"""Slice-1 claim-graph extraction trigger — corpus `rs_block` → grounded claim graph.

The ONLY trigger surface for `api.claim_extract_job.run_claim_extraction` (Task 2b). NOT
wired into any worker auto-loop — running this script by hand is the only way the job fires.

CREDIT DISCIPLINE (mirrors `scripts/run_downloads.py --dry`): **dry-run by DEFAULT**. It
projects the LLM cost and enforces the caps but spends NOTHING unless you pass `--live`.
`--live` is the only way to actually call the LLM and write claims.

Usage:
  ROSTER_CORPUS_DSN=postgresql://…/roster  python scripts/run_claim_extract.py            # dry run
  ROSTER_CORPUS_DSN=…  python scripts/run_claim_extract.py --source yc --limit 200       # dry run, scoped
  ROSTER_CORPUS_DSN=…  python scripts/run_claim_extract.py --live --max-usd 2            # SPEND (capped)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", action="append", dest="source_keys", default=None,
                    help="source_key to scan (repeatable); default: yc")
    ap.add_argument("--tenant", default="demo")
    ap.add_argument("--limit", type=int, default=None, help="max rs_block rows to scan")
    ap.add_argument("--live", action="store_true",
                    help="ACTUALLY call the LLM and write claims (default: dry run, no spend)")
    ap.add_argument("--max-blocks", type=int, default=500)
    ap.add_argument("--max-llm-calls", type=int, default=200)
    ap.add_argument("--max-usd", type=float, default=5.0)
    ap.add_argument("--price-per-call-usd", type=float, default=0.01)
    ap.add_argument("--model", default=None)
    ap.add_argument("--diligence", action="store_true",
                    help="extract the slice-2 DILIGENCE predicates too (default: slice-1 only)")
    a = ap.parse_args()

    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        print("ERROR: set ROSTER_CORPUS_DSN", file=sys.stderr)
        return 2

    source_keys = a.source_keys or ["yc"]
    dry_run = not a.live
    print(f"source_keys={source_keys}  tenant={a.tenant}  limit={a.limit}  "
          f"dry_run={dry_run}  caps(blocks={a.max_blocks},calls={a.max_llm_calls},usd={a.max_usd})")
    if not dry_run:
        print("!! --live: this WILL call the LLM and write claims (subject to caps) !!")

    # Imported here (after arg-parse) so `--help` never needs the app import path.
    from api.claim_extract_job import run_claim_extraction
    from roster_vertical.claim_predicates import active_predicates

    predicates = active_predicates(include_diligence=a.diligence)
    print(f"predicates={'slice1+diligence' if a.diligence else 'slice1'} "
          f"({len(predicates)} active)")

    summary = asyncio.run(run_claim_extraction(
        dsn=dsn, source_keys=source_keys, tenant_id=a.tenant, limit=a.limit,
        dry_run=dry_run, max_blocks=a.max_blocks, max_llm_calls=a.max_llm_calls,
        max_usd=a.max_usd, price_per_call_usd=a.price_per_call_usd, model=a.model,
        predicates=predicates))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
