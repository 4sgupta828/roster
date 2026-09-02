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
from api.artifacts import _DDL, github_org_artifact, openalex_work_artifact, select_repos  # noqa: E402

_OA_WORKS = "https://api.openalex.org/works"
_OA_SELECT = ("id,doi,title,display_name,publication_year,publication_date,type,cited_by_count,"
              "primary_location,authorships")
_PREFIX = {"openalex": "openalex:", "github": "github:"}


# ---- OpenAlex ----
def oa_works(author_id: str) -> tuple[list[dict], int]:
    """Top-25 works by citations for one author id → (works, total works count)."""
    params = {"filter": f"authorships.author.id:{author_id}", "per-page": 25,
              "sort": "cited_by_count:desc", "select": _OA_SELECT}
    if os.environ.get("ROSTER_OPENALEX_MAILTO"):
        params["mailto"] = os.environ["ROSTER_OPENALEX_MAILTO"]
    if os.environ.get("ROSTER_OPENALEX_KEY"):
        params["api_key"] = os.environ["ROSTER_OPENALEX_KEY"]
    url = _OA_WORKS + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "roster-artifacts/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
            return data.get("results") or [], int((data.get("meta") or {}).get("count") or 0)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            if e.code == 404:
                return [], 0
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2 * (attempt + 1)); continue
            raise
    return [], 0


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


async def write_person(conn, eid: str, source: str, arts: list[dict], n_total: int, status: str = "done") -> None:
    async with conn.transaction():
        for a in arts:
            await conn.execute(
                """INSERT INTO rs_person_artifact (entity_id, kind, artifact_key, title, url, date, venue,
                                                   role, detail, link_method, confidence, source_family, fetched_at)
                   VALUES ($1,$2,$3,$4,$5,$6::date,$7,$8,$9::jsonb,$10,$11,$12, now())
                   ON CONFLICT (entity_id, kind, artifact_key) DO UPDATE SET
                     title=EXCLUDED.title, url=EXCLUDED.url, date=EXCLUDED.date, venue=EXCLUDED.venue,
                     role=EXCLUDED.role, detail=EXCLUDED.detail, fetched_at=now()""",
                eid, a["kind"], a["artifact_key"], a["title"], a["url"], a.get("date"), a["venue"],
                a["role"], json.dumps(a["detail"]), a["link_method"], float(a["confidence"]), a["source_family"])
        await conn.execute(
            """INSERT INTO rs_artifact_scan (entity_id, source, status, n_found, n_total, scanned_at)
               VALUES ($1,$2,$3,$4,$5, now())
               ON CONFLICT (entity_id, source) DO UPDATE SET status=EXCLUDED.status, n_found=EXCLUDED.n_found,
                 n_total=EXCLUDED.n_total, scanned_at=now()""", eid, source, status, len(arts), int(n_total))


async def run_source(conn, source: str, ids: list[str], *, dry: bool, token: str) -> tuple[int, int]:
    """Scan `ids` for one source. Returns (people scanned, artifacts written)."""
    n_people = n_art = 0
    t0 = time.time()
    for eid in ids:
        key = eid.split(":", 1)[1]
        try:
            if source == "openalex":
                works, total = oa_works(key)
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
                await write_person(conn, eid, source, [], 0, status="error")
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
        await write_person(conn, eid, source, arts, total)
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
