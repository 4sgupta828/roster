#!/usr/bin/env python
"""Committed, resumable JOBS ingester — public ATS boards → rs_job.

Runs with prod env injected (local IP for fetching, prod DB for writing):
    railway run --service roster-api python scripts/ingest_jobs.py --dry  --limit 20
    railway run --service roster-api python scripts/ingest_jobs.py --live
    railway run --service roster-api python scripts/ingest_jobs.py --live --boards-file boards.tsv

Needs ROSTER_CORPUS_DSN (write target) and, for --live embeddings, OPENAI_API_KEY.
IDEMPOTENT: upserts on rs_job's real unique key (company, title, location, source), so re-running
refreshes rather than duplicates. RESUMABLE: each board is checkpointed in rs_ingest_checkpoint;
a completed board is skipped on re-run unless --refresh. --dry fetches + parses + counts but never
writes or embeds (zero cost) — use it to validate the pipeline and see per-board yields first.

Scaling to 2x: supply a large (ats, token, company) list via --boards-file (TSV: ats<TAB>token<TAB>company).
The committed STARTER_BOARDS below is a small, known-good set for validation.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.request

# (ats, board_token, company_display) — public board identifiers (structural, not secret).
STARTER_BOARDS: list[tuple[str, str, str]] = [
    ("greenhouse", "stripe", "Stripe"), ("greenhouse", "databricks", "Databricks"),
    ("greenhouse", "gitlab", "GitLab"), ("greenhouse", "coinbase", "Coinbase"),
    ("greenhouse", "robinhood", "Robinhood"), ("greenhouse", "instacart", "Instacart"),
    ("greenhouse", "airbnb", "Airbnb"), ("greenhouse", "dropbox", "Dropbox"),
    ("greenhouse", "twitch", "Twitch"), ("greenhouse", "cloudflare", "Cloudflare"),
    ("lever", "plaid", "Plaid"), ("lever", "netflix", "Netflix"), ("lever", "spotify", "Spotify"),
    ("lever", "brex", "Brex"), ("lever", "ramp", "Ramp"),
    ("ashby", "openai", "OpenAI"), ("ashby", "notion", "Notion"), ("ashby", "linear", "Linear"),
    ("ashby", "vercel", "Vercel"), ("ashby", "anthropic", "Anthropic"),
]

_UA = {"User-Agent": "roster-jobs-ingest/1.0 (+https://roster-api-production-3405.up.railway.app)"}


def _http_json(url: str, *, timeout: int = 25):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "").strip().lower())


# ---- ATS fetchers: each returns a list of normalized dicts {title, location, department, url} ----
def fetch_greenhouse(token: str) -> list[dict]:
    d = _http_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=false")
    out = []
    for j in d.get("jobs", []):
        out.append({"title": j.get("title") or "",
                    "location": ((j.get("location") or {}).get("name") or ""),
                    "department": (", ".join(x.get("name", "") for x in (j.get("departments") or [])) or ""),
                    "url": j.get("absolute_url") or ""})
    return out


def fetch_lever(token: str) -> list[dict]:
    d = _http_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in (d if isinstance(d, list) else []):
        cat = j.get("categories") or {}
        out.append({"title": j.get("text") or "",
                    "location": cat.get("location") or "",
                    "department": cat.get("team") or cat.get("department") or "",
                    "url": j.get("hostedUrl") or ""})
    return out


def fetch_ashby(token: str) -> list[dict]:
    d = _http_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false")
    out = []
    for j in d.get("jobs", []):
        out.append({"title": j.get("title") or "",
                    "location": j.get("location") or "",
                    "department": j.get("departmentName") or j.get("teamName") or "",
                    "url": j.get("jobUrl") or ""})
    return out


_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


def embed_batch(texts: list[str]) -> list[str | None]:
    """Embed a batch with text-embedding-3-small → pgvector literals. One HTTP call per batch.
    Returns None entries on failure so upsert can still write the row (embedding NULL, backfilled later)."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not texts:
        return [None] * len(texts)
    try:
        body = json.dumps({"model": "text-embedding-3-small",
                           "input": [t[:2000] for t in texts]}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)["data"]
        return ["[" + ",".join(f"{x:.6f}" for x in item["embedding"]) + "]" for item in data]
    except Exception as e:   # noqa: BLE001
        print(f"  ! embed batch failed ({e}); writing rows without embeddings", file=sys.stderr)
        return [None] * len(texts)


async def ensure_checkpoint(conn) -> None:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS rs_ingest_checkpoint (
             source     text NOT NULL,          -- engine: 'jobs' | 'people'
             cursor_key text NOT NULL,          -- e.g. 'greenhouse:stripe' or a people search window
             status     text NOT NULL DEFAULT 'done',
             n_seen     int  NOT NULL DEFAULT 0,
             n_written  int  NOT NULL DEFAULT 0,
             updated_at timestamptz NOT NULL DEFAULT now(),
             PRIMARY KEY (source, cursor_key)
           )""")


async def done_boards(conn) -> set[str]:
    rows = await conn.fetch("SELECT cursor_key FROM rs_ingest_checkpoint "
                            "WHERE source='jobs' AND status='done'")
    return {r["cursor_key"] for r in rows}


async def upsert_jobs(conn, company: str, source: str, rows: list[dict], vecs: list[str | None]) -> int:
    """Idempotent upsert on the real unique key (company, title, location, source). location/department
    coalesced to '' so the unique key actually dedups (NULLs never collide in a unique index)."""
    n = 0
    for r, vec in zip(rows, vecs):
        title = (r.get("title") or "").strip()
        if not title:
            continue
        await conn.execute(
            """INSERT INTO rs_job (company, title, location, department, url, source, title_norm, embedding, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8::vector,now())
               ON CONFLICT (company, title, location, source) DO UPDATE SET
                 department = EXCLUDED.department, url = EXCLUDED.url, title_norm = EXCLUDED.title_norm,
                 embedding  = COALESCE(EXCLUDED.embedding, rs_job.embedding), updated_at = now()""",
            company, title, (r.get("location") or ""), (r.get("department") or ""),
            (r.get("url") or ""), source, _norm_title(title), vec)
        n += 1
    return n


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="write to rs_job (+ embed); default is dry")
    ap.add_argument("--dry", action="store_true", help="fetch+parse+count only; no write, no embed, no cost")
    ap.add_argument("--limit", type=int, default=0, help="max boards to process this run (0 = all)")
    ap.add_argument("--refresh", action="store_true", help="re-process boards already marked done")
    ap.add_argument("--boards-file", default="", help="TSV of ats<TAB>token<TAB>company to use instead of STARTER_BOARDS")
    args = ap.parse_args()
    live = args.live and not args.dry

    boards = list(STARTER_BOARDS)
    if args.boards_file:
        boards = []
        with open(args.boards_file) as fh:
            for ln in fh:
                p = ln.rstrip("\n").split("\t")
                if len(p) >= 3 and p[0] in _FETCHERS:
                    boards.append((p[0], p[1], p[2]))

    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        print("ROSTER_CORPUS_DSN not set — run via `railway run --service roster-api ...`", file=sys.stderr)
        sys.exit(2)
    import asyncpg
    conn = await asyncpg.connect(dsn)
    await ensure_checkpoint(conn)
    already = set() if args.refresh else await done_boards(conn)

    total_seen = total_written = total_embedded = boards_done = 0
    try:
        for ats, token, company in boards:
            key = f"{ats}:{token}"
            if key in already:
                continue
            if args.limit and boards_done >= args.limit:
                break
            boards_done += 1
            try:
                rows = _FETCHERS[ats](token)
            except Exception as e:   # noqa: BLE001 — a dead/renamed board must not abort the sweep
                print(f"[skip] {key} ({company}): fetch failed: {e}", file=sys.stderr)
                continue
            total_seen += len(rows)
            if args.dry:
                print(f"[dry ] {key:28s} {company:20s} {len(rows):4d} jobs")
                continue
            if live:
                vecs = [None] * len(rows)
                texts = [f"{r.get('title','')} at {company}. {r.get('location','')}. {r.get('department','')}"
                         for r in rows]
                for i in range(0, len(texts), 100):     # embed in batches of 100
                    vecs[i:i + 100] = embed_batch(texts[i:i + 100])
                total_embedded += sum(1 for v in vecs if v is not None)
                async with conn.transaction():
                    w = await upsert_jobs(conn, company, ats, rows, vecs)
                    await conn.execute(
                        """INSERT INTO rs_ingest_checkpoint (source, cursor_key, status, n_seen, n_written)
                           VALUES ('jobs',$1,'done',$2,$3)
                           ON CONFLICT (source, cursor_key) DO UPDATE SET
                             status='done', n_seen=EXCLUDED.n_seen, n_written=EXCLUDED.n_written, updated_at=now()""",
                        key, len(rows), w)
                total_written += w
                print(f"[live] {key:28s} {company:20s} {len(rows):4d} jobs → {w} upserted")
    finally:
        await conn.close()

    # cost accounting (embeddings only; ATS fetches are free)
    est_tokens = total_embedded * 20      # ~20 tokens/job blurb (rough)
    est_cost = est_tokens / 1_000_000 * 0.02   # text-embedding-3-small: $0.02 / 1M tokens
    print("\n=== summary ===")
    print(f"boards processed: {boards_done} | jobs seen: {total_seen} | "
          f"{'DRY (no writes)' if not live else f'upserted: {total_written} | embedded: {total_embedded}'}")
    if live:
        print(f"est. embedding cost this run: ~${est_cost:.4f}  (${est_cost / max(total_written,1) * 1000:.4f} / 1k jobs)")


if __name__ == "__main__":
    asyncio.run(main())
