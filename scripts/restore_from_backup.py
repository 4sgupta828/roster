#!/usr/bin/env python
"""RECREATE from an R2 backup (the path `apps/api/backup.py` promises). DRY BY DEFAULT — prints what
would be loaded; `--live` writes. Idempotent (ON CONFLICT DO NOTHING) so it can be re-run or applied
over a partially restored database. The target schema must exist (start the app once against the
new database, or run its ensure_schema paths) — a table missing in the target is reported, not
created. Embeddings / tsvectors are not in backups: re-embed afterwards (people: `ingest_people.py
--backfill`, jobs: `ingest_jobs.py --backfill`).

    python scripts/restore_from_backup.py                       # newest complete backup, dry
    python scripts/restore_from_backup.py --date 2026-09-03 --tables rs_map,rs_person_link --live
    python scripts/restore_from_backup.py --list                # backups in R2
    python scripts/restore_from_backup.py --prune-keep 2 [--live]   # retention by hand

Env: ROSTER_CORPUS_DSN (target — point it at the NEW database), R2_ENDPOINT/R2_ACCESS_KEY_ID/
R2_SECRET_ACCESS_KEY/R2_BUCKET. Run inside the container (`railway ssh --service roster-api`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps"))

from api.backup import R2Named, backup_dates, prune_backups, run_restore  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="", help="backup date (default: newest complete)")
    ap.add_argument("--tables", default="", help="comma-separated subset (default: all, in restore order)")
    ap.add_argument("--live", action="store_true", help="WRITE to the target database (default: dry run)")
    ap.add_argument("--list", action="store_true", help="list complete backups and exit")
    ap.add_argument("--prune-keep", type=int, default=0, help="delete backups older than the newest N complete ones")
    args = ap.parse_args()
    store = R2Named()
    if args.list:
        print("complete backups:", backup_dates(store)); return
    if args.prune_keep:
        print(json.dumps(prune_backups(store, keep=args.prune_keep, dry=not args.live), indent=1)); return
    dsn = os.environ.get("ROSTER_CORPUS_DSN", "")
    if not dsn:
        print("ROSTER_CORPUS_DSN not set (point it at the TARGET database)", file=sys.stderr); sys.exit(2)
    if args.live:
        print(f"LIVE restore into {dsn.split('@')[-1]} — idempotent inserts (ON CONFLICT DO NOTHING)", flush=True)
    res = await run_restore(dsn, store, date=args.date,
                            tables=[t.strip() for t in args.tables.split(",") if t.strip()] or None, live=args.live)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
