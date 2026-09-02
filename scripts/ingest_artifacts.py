#!/usr/bin/env python
"""Committed, resumable PUBLIC-ARTIFACT linker — evidence-model-v2 step 1.

Links DATED public artifacts to people already in the index, by STRONG IDENTITY KEY only:
  openalex:A…   → the author's works (OpenAlex /works filtered by that author id)  → 'paper' rows
  github:login  → the login's own non-fork public repos + public org memberships → 'repo'/'org' rows
Name-only matching is never done here (the design invariant: never merge same-named people).

Zero LLM / embedding spend: HTTP metadata only. Writes rs_person_artifact (upsert) and marks the
person in rs_artifact_scan so re-runs skip them (RESUMABLE; --refresh re-scans). Priority order:
people who appear in SAVED MAPS first (they are being reviewed), then the newest-ingested people.

    python scripts/ingest_artifacts.py --source openalex --limit 300
    python scripts/ingest_artifacts.py --source github   --limit 300
    python scripts/ingest_artifacts.py --source all --ids github:torvalds,openalex:A5042962646 --dry

Env: ROSTER_CORPUS_DSN; ROSTER_GITHUB_TOKEN (core bucket, shared with the people sweep — this leg
stops when fewer than ROSTER_GH_RESERVE (default 1200) calls remain so the sweep keeps headroom);
ROSTER_OPENALEX_MAILTO (polite pool — higher, more reliable rate limits) / ROSTER_OPENALEX_KEY.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                                            # sibling: ingest_people helpers
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "apps"))     # api.artifacts parsers

from ingest_people import _gh_get, _throttle  # noqa: E402
from api.artifacts import (_DDL, fetch_openalex_works, github_org_artifact,  # noqa: E402
                           openalex_work_artifact, select_repos, write_person_artifacts)

_PREFIX = {"openalex": "openalex:", "github": "github:"}


# ---- GitHub ----
def gh_person(login: str, *, token: str) -> tuple[list[dict], list[dict], int]:
    """(kept repo artifacts, org artifacts, public repo count) for a login; 2 core calls."""
    repos, hdr = _gh_get(f"/users/{login}/repos", token=token,
                         params={"per_page": 100, "type": "owner", "sort": "pushed"})
    _throttle(hdr)
    kept = select_repos(repos if isinstance(repos, list) else [], login)
    orgs, hdr = _gh_get(f"/users/{login}/orgs", token=token, params={"per_page": 50})
    _throttle(hdr)
    org_rows = [a for a in (github_org_artifact(o) for o in (orgs if isinstance(orgs, list) else [])) if a]
    remaining = int(hdr.get("X-RateLimit-Remaining", "9999") or 9999)
    if remaining < int(os.environ.get("ROSTER_GH_RESERVE", "1200") or 1200):
        raise _Reserve(remaining)
    return kept, org_rows, len(repos) if isinstance(repos, list) else 0


class _Reserve(Exception):
    """Raised when the shared GitHub core bucket is down to the reserve — end this run gracefully."""


# ---- DB ----
async def candidates(conn, source: str, *, limit: int, refresh: bool) -> list[str]:
    """People to scan for `source`: saved-map members first, then newest-ingested; unscanned only
    (unless --refresh)."""
    skip = "" if refresh else "AND s.entity_id IS NULL"
    rows = await conn.fetch(
        f"""WITH pri AS (SELECT DISTINCT (x->>'entity_id') AS entity_id
                         FROM rs_map, jsonb_array_elements(rows) x)
            SELECT e.entity_id
            FROM rs_entity e
            LEFT JOIN rs_artifact_scan s ON s.entity_id = e.entity_id AND s.source = $1
            LEFT JOIN pri ON pri.entity_id = e.entity_id
            WHERE e.kind = 'person' AND e.status = 'active' AND e.entity_id LIKE $2 {skip}
            ORDER BY (pri.entity_id IS NULL), e.retrieved_at DESC
            LIMIT $3""", source, _PREFIX[source] + "%", int(limit))
    return [r["entity_id"] for r in rows]


async def run_source(conn, source: str, ids: list[str], *, dry: bool, token: str) -> tuple[int, int]:
    """Scan `ids` for one source. Returns (people scanned, artifacts written)."""
    n_people = n_art = 0
    t0 = time.time()
    for eid in ids:
        key = eid.split(":", 1)[1]
        try:
            if source == "openalex":
                works, total = fetch_openalex_works(key)
                arts = [a for a in (openalex_work_artifact(w, key) for w in works) if a]
                time.sleep(0.11)                                   # ≤10 req/s (polite-pool ceiling)
            else:
                repos, orgs, total = gh_person(key, token=token)
                arts = repos + orgs
        except _Reserve as r:
            print(f"  … GitHub core bucket at reserve ({r}); ending this run", file=sys.stderr)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404 and not dry:                          # account gone: record, move on
                await write_person_artifacts(conn, eid, source, [], 0, status="error")
            print(f"  ! {eid}: HTTP {e.code}", file=sys.stderr)
            continue
        except Exception as e:  # noqa: BLE001 — one bad person never aborts the run
            print(f"  ! {eid}: {e}", file=sys.stderr)
            continue
        n_people += 1
        n_art += len(arts)
        if dry:
            print(f"{eid}: {len(arts)} artifacts (source total {total})")
            for a in arts[:5]:
                print(f"   - {a['kind']} {a['date'] or '----'} {a['title'][:70]}  {a['url']}")
            continue
        try:
            await write_person_artifacts(conn, eid, source, arts, total)
        except Exception as e:  # noqa: BLE001 — a write error on one person must not abort the run
            print(f"  ! {eid}: write failed: {e}", file=sys.stderr)
            continue
        if n_people % 50 == 0:
            print(f"  {source}: {n_people}/{len(ids)} people, {n_art} artifacts, {time.time()-t0:.0f}s", flush=True)
    return n_people, n_art


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=("openalex", "github", "all"), default="all")
    ap.add_argument("--limit", type=int, default=200, help="people per source this run")
    ap.add_argument("--ids", default="", help="comma-separated entity ids (targeted validation)")
    ap.add_argument("--dry", action="store_true", help="fetch + print only; no writes")
    ap.add_argument("--refresh", action="store_true", help="re-scan people already scanned")
    args = ap.parse_args()
    import asyncpg
    dsn = os.environ.get("ROSTER_CORPUS_DSN", "")
    if not dsn:
        print("ROSTER_CORPUS_DSN not set", file=sys.stderr); sys.exit(2)
    token = os.environ.get("ROSTER_GITHUB_TOKEN", "")
    sources = ["openalex", "github"] if args.source == "all" else [args.source]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(_DDL)
        for source in sources:
            if source == "github" and not token:
                print("ROSTER_GITHUB_TOKEN not set — skipping github", file=sys.stderr); continue
            if args.ids:
                ids = [i.strip() for i in args.ids.split(",") if i.strip().startswith(_PREFIX[source])]
            else:
                ids = await candidates(conn, source, limit=args.limit, refresh=args.refresh)
            if not ids:
                print(f"{source}: nothing to scan"); continue
            n_p, n_a = await run_source(conn, source, ids, dry=args.dry, token=token)
            print(f"{source}: scanned {n_p} people → {n_a} artifacts{' (dry)' if args.dry else ''}", flush=True)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
