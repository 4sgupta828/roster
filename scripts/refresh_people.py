"""ON-DEMAND people refresh (admin-triggered, never automatic — owner's call 2026-09-04).

Re-checks GitHub profiles CONDITIONALLY: `GET /users/{login}` with the stored ETag. A 304 costs no rate
limit and means "unchanged" (retrieved_at bumped). A 200 means the profile moved: the facets are
re-extracted (the ONE LLM call, only for people who changed), the person is re-embedded, stale semantic
facets (old employer, old role) are replaced, and the identity edges follow. Selection, most useful
first: explicit --logins, then --map (the people on one saved map), then the oldest `retrieved_at`
older than --older-than-days. Progress is a checkpoint row ('people_refresh', <run_id>) for the admin
status endpoint. Dry by default.

    python scripts/refresh_people.py --live --limit 500 --older-than-days 14
    python scripts/refresh_people.py --live --map <map_id>
"""
from __future__ import annotations

import argparse
import asyncio
import re
import importlib.util
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

_here = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("ingest_people", _here / "ingest_people.py")
ip = importlib.util.module_from_spec(_spec); sys.modules["ingest_people"] = ip; _spec.loader.exec_module(ip)   # type: ignore

_GH = "https://api.github.com"
_SEMANTIC = ("role", "seniority", "function", "company", "metro", "country", "skill", "title")


def gh_get_cond(login: str, *, token: str, etag: str | None) -> tuple[int, dict | None, str]:
    """Conditional profile GET → (status, profile|None, etag). 304 does not count against the limit."""
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "roster-people-refresh/1.0"}
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(f"{_GH}/users/{login}", headers=headers)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ip._throttle(dict(r.headers))
                return 200, json.load(r), (r.headers.get("ETag") or "")
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return 304, None, (etag or "")
            hdr = dict(e.headers or {})
            if e.code in (403, 429) and attempt == 1 and (e.code == 429 or hdr.get("X-RateLimit-Remaining") == "0" or hdr.get("Retry-After")):
                ip._sleep_until_reset(hdr, retry_after=hdr.get("Retry-After", ""))
                continue
            raise
    return 0, None, (etag or "")


def _norm_co(v) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v or "").lower().lstrip("@"))


def profile_changed(stored: dict, fresh: dict) -> bool:
    """The fields the facets are extracted from: a change here is what warrants the LLM + re-embed.
    An EMPLOYER move counts on its own (normalized); location / bio / blog count only when the stored
    snapshot actually holds them (363k legacy rows carry no snapshot — the first pass learns it, free)."""
    if _norm_co(stored.get("company")) != _norm_co(fresh.get("company")):
        return True
    return any((stored.get(k) or "") != (fresh.get(k) or "") for k in ("location", "bio", "blog") if stored.get(k))


async def ensure_columns(conn) -> None:
    await conn.execute("ALTER TABLE rs_entity ADD COLUMN IF NOT EXISTS etag text")
    await conn.execute("ALTER TABLE rs_entity ADD COLUMN IF NOT EXISTS refreshed_at timestamptz")
    await conn.execute("ALTER TABLE rs_entity ADD COLUMN IF NOT EXISTS changed_at timestamptz")


async def select_targets(conn, *, logins: list[str], map_id: str, older_than_days: int, limit: int) -> list[dict]:
    # the stored snapshot: rs_entity.facets when the ingest wrote one; else the employer facet on record
    CO = "(SELECT f.display_value FROM roster_entity_facet f WHERE f.entity_id = e.entity_id AND f.facet_key = 'company' LIMIT 1) AS company_facet"
    if logins:
        ids = ["github:" + l.strip() for l in logins if l.strip()]
        rows = await conn.fetch(f"SELECT e.entity_id, e.facets, e.etag, {CO} FROM rs_entity e WHERE e.entity_id = ANY($1::text[]) AND e.kind='person'", ids)
    elif map_id:
        rows = await conn.fetch(
            f"""SELECT e.entity_id, e.facets, e.etag, {CO} FROM rs_map m, jsonb_array_elements(m.rows) r
               JOIN rs_entity e ON e.entity_id = r->>'entity_id'
               WHERE m.id = $1 AND e.entity_id LIKE 'github:%' AND e.kind='person'
               ORDER BY e.refreshed_at NULLS FIRST LIMIT $2""", map_id, int(limit))
    else:
        rows = await conn.fetch(
            f"""SELECT e.entity_id, e.facets, e.etag, {CO} FROM rs_entity e
               WHERE e.kind='person' AND e.entity_id LIKE 'github:%' AND COALESCE(e.status,'') <> 'suppressed'
                 AND COALESCE(e.refreshed_at, e.retrieved_at) < now() - $1 * interval '1 day'
               ORDER BY COALESCE(e.refreshed_at, e.retrieved_at) NULLS FIRST LIMIT $2""", int(older_than_days), int(limit))
    out = []
    for r in rows:
        f = r["facets"]; f = json.loads(f) if isinstance(f, str) else (f or {})
        if not f:
            f = {"company": r["company_facet"] or ""}
        out.append({"entity_id": r["entity_id"], "login": r["entity_id"].split(":", 1)[1], "facets": f, "etag": r["etag"]})
    return out


async def refresh(conn, targets: list[dict], *, token: str, live: bool, run_id: str) -> dict:
    st = {"checked": 0, "unchanged": 0, "changed": 0, "gone": 0, "failed": 0, "llm_calls": 0}
    for i, t in enumerate(targets):
        try:
            status, prof, etag = gh_get_cond(t["login"], token=token, etag=t["etag"])
        except urllib.error.HTTPError as e:
            if e.code == 404:   # account deleted / renamed: mark, never search-visible as current
                st["gone"] += 1
                if live:
                    await conn.execute("UPDATE rs_entity SET status = COALESCE(NULLIF(status,''), 'gone'), refreshed_at = now() WHERE entity_id=$1 AND COALESCE(status,'') <> 'suppressed'", t["entity_id"])
                continue
            st["failed"] += 1; print(f"  ! {t['login']}: {e}", file=sys.stderr); continue
        except Exception as e:   # noqa: BLE001
            st["failed"] += 1; print(f"  ! {t['login']}: {e}", file=sys.stderr); continue
        st["checked"] += 1
        if status == 304 or (prof and not profile_changed(t["facets"], prof) and t["etag"]):
            st["unchanged"] += 1
            if live:
                await conn.execute("UPDATE rs_entity SET refreshed_at = now(), etag = COALESCE($2, etag) WHERE entity_id=$1", t["entity_id"], etag or None)
            continue
        if not prof:
            st["failed"] += 1; continue
        changed = profile_changed(t["facets"], prof)
        if not changed:   # first conditional pass: only the etag (and, for legacy rows, the snapshot) was missing
            st["unchanged"] += 1
            if live:
                snap = json.dumps({k: prof.get(k) for k in ("company", "location", "bio", "html_url", "blog")})
                await conn.execute("UPDATE rs_entity SET refreshed_at = now(), etag = $2, facets = CASE WHEN facets IS NULL OR facets = '{}'::jsonb THEN $3::jsonb ELSE facets END WHERE entity_id=$1",
                                   t["entity_id"], etag or None, snap)
            continue
        st["changed"] += 1
        if not live:
            print(f"[dry ] {t['login']:20s} changed: " + ", ".join(k for k in ("company", "location", "bio", "name", "blog") if (t['facets'].get(k) or '') != (prof.get(k) or '')))
            continue
        fac = ip.extract_facets([prof])[0]; st["llm_calls"] += 1
        blurb = " ".join(str(x) for x in [prof.get("name"), prof.get("bio"), fac.get("role"), fac.get("function"),
                                          " ".join(fac.get("skills") or []), prof.get("company"), prof.get("location")] if x)
        vec = ip.embed_one(blurb)
        async with conn.transaction():
            # stale SEMANTIC facets go (old employer / role / skills); link facets are re-derived below
            await conn.execute("DELETE FROM roster_entity_facet WHERE tenant_id='demo' AND entity_id=$1 AND facet_key = ANY($2::text[])",
                               t["entity_id"], list(_SEMANTIC))
            await ip.upsert_person(conn, prof, fac, vec)
            await conn.execute("UPDATE rs_entity SET refreshed_at = now(), changed_at = now(), etag = $2 WHERE entity_id=$1", t["entity_id"], etag or None)
        print(f"[live] {t['login']:20s} changed → facets re-extracted{' + re-embedded' if vec else ''}")
        if live and (i + 1) % 25 == 0:
            await conn.execute("UPDATE rs_ingest_checkpoint SET n_seen=$2, n_written=$3, updated_at=now(), error=$4 WHERE source='people_refresh' AND cursor_key=$1",
                               run_id, st["checked"], st["changed"], json.dumps(st))
    return st


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true"); ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--older-than-days", type=int, default=14)
    ap.add_argument("--logins", default="", help="comma-separated GitHub logins")
    ap.add_argument("--map", default="", help="refresh the people on one saved map")
    ap.add_argument("--run-id", default="", help="checkpoint key for progress (admin endpoint sets it)")
    args = ap.parse_args()
    live = args.live and not args.dry
    dsn, token = os.environ.get("ROSTER_CORPUS_DSN"), os.environ.get("ROSTER_GITHUB_TOKEN", "")
    if not dsn or not token:
        print("ROSTER_CORPUS_DSN and ROSTER_GITHUB_TOKEN required", file=sys.stderr); sys.exit(2)
    import asyncpg
    conn = await asyncpg.connect(dsn)
    run_id = args.run_id or f"run-{int(time.time())}"
    try:
        await ensure_columns(conn)
        await ip.ensure_checkpoint(conn)
        targets = await select_targets(conn, logins=[x for x in args.logins.split(",") if x.strip()], map_id=args.map,
                                       older_than_days=args.older_than_days, limit=args.limit)
        if live:
            await conn.execute("""INSERT INTO rs_ingest_checkpoint (source, cursor_key, status, n_seen, n_written)
                                  VALUES ('people_refresh', $1, 'running', 0, 0)
                                  ON CONFLICT (source, cursor_key) DO UPDATE SET status='running', updated_at=now()""", run_id)
        print(f"people refresh: {len(targets)} targets ({'live' if live else 'dry'})")
        st = await refresh(conn, targets, token=token, live=live, run_id=run_id)
        if live:
            await conn.execute("UPDATE rs_ingest_checkpoint SET status='done', n_seen=$2, n_written=$3, updated_at=now(), error=$4 WHERE source='people_refresh' AND cursor_key=$1",
                               run_id, st["checked"], st["changed"], json.dumps(st))
    finally:
        await conn.close()
    print(f"people refresh {run_id}: {st}")


if __name__ == "__main__":
    asyncio.run(main())
