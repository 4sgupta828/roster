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

# (ats, board_token, company_display) — public board identifiers (structural, not secret). A curated,
# high-confidence set of large tech employers (high job counts). Wrong/renamed tokens just 404 and are
# skipped. For a bigger 2x sweep, supply thousands via --boards-file (ats<TAB>token<TAB>company).
STARTER_BOARDS: list[tuple[str, str, str]] = [
    # --- Greenhouse (boards-api.greenhouse.io) ---
    ("greenhouse", "stripe", "Stripe"), ("greenhouse", "databricks", "Databricks"),
    ("greenhouse", "gitlab", "GitLab"), ("greenhouse", "coinbase", "Coinbase"),
    ("greenhouse", "robinhood", "Robinhood"), ("greenhouse", "instacart", "Instacart"),
    ("greenhouse", "airbnb", "Airbnb"), ("greenhouse", "dropbox", "Dropbox"),
    ("greenhouse", "twitch", "Twitch"), ("greenhouse", "cloudflare", "Cloudflare"),
    ("greenhouse", "pinterest", "Pinterest"), ("greenhouse", "reddit", "Reddit"),
    ("greenhouse", "figma", "Figma"), ("greenhouse", "gusto", "Gusto"),
    ("greenhouse", "affirm", "Affirm"), ("greenhouse", "doordash", "DoorDash"),
    ("greenhouse", "snowflake", "Snowflake"), ("greenhouse", "samsara", "Samsara"),
    ("greenhouse", "asana", "Asana"), ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "hashicorp", "HashiCorp"), ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "elastic", "Elastic"), ("greenhouse", "confluent", "Confluent"),
    ("greenhouse", "gemini", "Gemini"), ("greenhouse", "discord", "Discord"),
    ("greenhouse", "lyft", "Lyft"), ("greenhouse", "sofi", "SoFi"),
    ("greenhouse", "chime", "Chime"), ("greenhouse", "flexport", "Flexport"),
    ("greenhouse", "benchling", "Benchling"), ("greenhouse", "airtable", "Airtable"),
    ("greenhouse", "faire", "Faire"), ("greenhouse", "checkr", "Checkr"),
    ("greenhouse", "webflow", "Webflow"), ("greenhouse", "grammarly", "Grammarly"),
    ("greenhouse", "niantic", "Niantic"), ("greenhouse", "unity", "Unity"),
    ("greenhouse", "cockroachlabs", "Cockroach Labs"), ("greenhouse", "mongodb", "MongoDB"),
    ("greenhouse", "cruise", "Cruise"), ("greenhouse", "wealthsimple", "Wealthsimple"),
    ("greenhouse", "toast", "Toast"), ("greenhouse", "opensea", "OpenSea"),
    ("greenhouse", "circle", "Circle"), ("greenhouse", "kraken", "Kraken"),
    ("greenhouse", "vimeo", "Vimeo"), ("greenhouse", "squarespace", "Squarespace"),
    # --- Lever (api.lever.co) ---
    ("lever", "spotify", "Spotify"), ("lever", "palantir", "Palantir"),
    ("lever", "kickstarter", "Kickstarter"), ("lever", "mercury", "Mercury"),
    ("lever", "attentive", "Attentive"), ("lever", "hometap", "Hometap"),
    ("lever", "voleon", "Voleon"), ("lever", "included", "Included Health"),
    # --- Ashby (api.ashbyhq.com) ---
    ("ashby", "openai", "OpenAI"), ("ashby", "notion", "Notion"), ("ashby", "linear", "Linear"),
    ("ashby", "ramp", "Ramp"), ("ashby", "runway", "Runway"), ("ashby", "clipboard", "Clipboard Health"),
    ("ashby", "posthog", "PostHog"), ("ashby", "replit", "Replit"), ("ashby", "cursor", "Cursor"),
    ("ashby", "perplexity", "Perplexity"), ("ashby", "mistral", "Mistral AI"),
    ("ashby", "scale", "Scale AI"), ("ashby", "sardine", "Sardine"), ("ashby", "tome", "Tome"),
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


# WORKDAY tenants — the single biggest source in the existing corpus (~17k). Each is
# (company, host, wd_num, tenant, site); the public jobs API is a POST to the cxs endpoint. Tenant
# configs vary per company and must be sourced/verified (dry-run weeds out wrong ones); extend via
# --workday-file (company<TAB>host<TAB>wd<TAB>tenant<TAB>site).
WORKDAY_TENANTS: list[tuple[str, str, str, str, str]] = [
    ("NVIDIA", "nvidia", "5", "nvidia", "NVIDIAExternalCareerSite"),
    ("Salesforce", "salesforce", "1", "salesforce", "External_Career_Site"),
    ("Workday", "workday", "5", "workday", "Workday"),
    ("Adobe", "adobe", "5", "adobe", "external_experienced"),
    ("Dell", "dell", "1", "dell", "External"),
    ("Nike", "nike", "1", "nike", "nike"),
]


def fetch_workday(cfg: tuple[str, str, str, str, str]) -> list[dict]:
    """POST-paginate a Workday cxs job board → normalized rows. company is the tenant's display name."""
    company, host, wd, tenant, site = cfg
    base = f"https://{host}.wd{wd}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    for _ in range(60):   # bounded: up to 60*20 = 1200 postings/tenant
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}).encode()
        req = urllib.request.Request(api, data=body, headers={**_UA, "Content-Type": "application/json",
                                                              "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        posts = d.get("jobPostings") or []
        if not posts:
            break
        for j in posts:
            path = j.get("externalPath") or ""
            out.append({"company": company, "title": j.get("title") or "",
                        "location": j.get("locationsText") or "", "department": "",
                        "url": (base + "/" + site + path) if path else base})
        offset += 20
        if offset >= int(d.get("total") or 0):
            break
    return out


# ---- AGGREGATOR sources: single public JSON feeds (no per-company token), each job carries its own
# company. These are the volume drivers for a 2x (the existing corpus came largely from these). Each
# returns normalized {company, title, location, url}; pagination is bounded to keep runs finite. ----
def agg_remoteok() -> list[dict]:
    d = _http_json("https://remoteok.com/api")
    out = []
    for j in (d if isinstance(d, list) else []):
        if not isinstance(j, dict) or not j.get("position"):
            continue   # first element is a legal-notice object
        out.append({"company": j.get("company") or "", "title": j.get("position") or "",
                    "location": j.get("location") or "Remote", "department": "", "url": j.get("url") or ""})
    return out


def agg_remotive() -> list[dict]:
    d = _http_json("https://remotive.com/api/remote-jobs")
    return [{"company": j.get("company_name") or "", "title": j.get("title") or "",
             "location": j.get("candidate_required_location") or "Remote", "department": j.get("category") or "",
             "url": j.get("url") or ""} for j in d.get("jobs", [])]


def agg_arbeitnow() -> list[dict]:
    out, url = [], "https://www.arbeitnow.com/api/job-board-api"
    for _ in range(20):   # bounded pagination
        d = _http_json(url)
        for j in d.get("data", []):
            out.append({"company": j.get("company_name") or "", "title": j.get("title") or "",
                        "location": j.get("location") or "", "department": "", "url": j.get("url") or ""})
        url = (d.get("links") or {}).get("next")
        if not url:
            break
    return out


def agg_jobicy() -> list[dict]:
    d = _http_json("https://jobicy.com/api/v2/remote-jobs?count=100")
    return [{"company": j.get("companyName") or "", "title": j.get("jobTitle") or "",
             "location": j.get("jobGeo") or "Remote", "department": (j.get("jobIndustry") or [""])[0]
             if isinstance(j.get("jobIndustry"), list) else (j.get("jobIndustry") or ""),
             "url": j.get("url") or ""} for j in d.get("jobs", [])]


def agg_themuse() -> list[dict]:
    out = []
    for page in range(1, 40):   # bounded pagination
        try:
            d = _http_json(f"https://www.themuse.com/api/public/jobs?page={page}")
        except Exception:   # noqa: BLE001 — themuse 404s past the last page
            break
        results = d.get("results", [])
        if not results:
            break
        for j in results:
            out.append({"company": (j.get("company") or {}).get("name") or "", "title": j.get("name") or "",
                        "location": ", ".join(l.get("name", "") for l in (j.get("locations") or [])),
                        "department": j.get("category") or "", "url": (j.get("refs") or {}).get("landing_page") or ""})
    return out


_AGGREGATORS = {"remoteok": agg_remoteok, "remotive": agg_remotive, "arbeitnow": agg_arbeitnow,
                "jobicy": agg_jobicy, "themuse": agg_themuse}


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


async def upsert_jobs(conn, source: str, rows: list[dict], vecs: list[str | None],
                      company_default: str = "") -> int:
    """Idempotent upsert on the real unique key (company, title, location, source). Company is per-row
    (aggregators) falling back to company_default (single-company boards). location/department coalesced
    to '' so the unique key actually dedups (NULLs never collide in a unique index)."""
    n = 0
    for r, vec in zip(rows, vecs):
        title = (r.get("title") or "").strip()
        company = (r.get("company") or company_default or "").strip()
        if not title or not company:
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
    ap.add_argument("--no-aggregators", action="store_true", help="skip the aggregator feeds (RemoteOK/Remotive/…)")
    ap.add_argument("--no-workday", action="store_true", help="skip the Workday tenant sweep")
    ap.add_argument("--workday-file", default="", help="TSV company<TAB>host<TAB>wd<TAB>tenant<TAB>site to use instead of WORKDAY_TENANTS")
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

    workday_tenants = list(WORKDAY_TENANTS)
    if args.workday_file:
        workday_tenants = []
        with open(args.workday_file) as fh:
            for ln in fh:
                p = ln.rstrip("\n").split("\t")
                if len(p) >= 5:
                    workday_tenants.append((p[0], p[1], p[2], p[3], p[4]))

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
                    w = await upsert_jobs(conn, ats, rows, vecs, company_default=company)
                    await conn.execute(
                        """INSERT INTO rs_ingest_checkpoint (source, cursor_key, status, n_seen, n_written)
                           VALUES ('jobs',$1,'done',$2,$3)
                           ON CONFLICT (source, cursor_key) DO UPDATE SET
                             status='done', n_seen=EXCLUDED.n_seen, n_written=EXCLUDED.n_written, updated_at=now()""",
                        key, len(rows), w)
                total_written += w
                print(f"[live] {key:28s} {company:20s} {len(rows):4d} jobs → {w} upserted")

        # AGGREGATOR pass (skip with --no-aggregators): single public feeds, each job carries its own
        # company — the volume drivers for a 2x. Checkpointed per aggregator, resumable.
        if not args.no_aggregators:
            for name, fetch in _AGGREGATORS.items():
                key = f"agg:{name}"
                if key in already:
                    continue
                if args.limit and boards_done >= args.limit:
                    break
                boards_done += 1
                try:
                    rows = fetch()
                except Exception as e:   # noqa: BLE001
                    print(f"[skip] {key}: fetch failed: {e}", file=sys.stderr)
                    continue
                total_seen += len(rows)
                if args.dry:
                    print(f"[dry ] {key:28s} {'(per-job company)':20s} {len(rows):4d} jobs")
                    continue
                vecs = [None] * len(rows)
                texts = [f"{r.get('title','')} at {r.get('company','')}. {r.get('location','')}. {r.get('department','')}"
                         for r in rows]
                for i in range(0, len(texts), 100):
                    vecs[i:i + 100] = embed_batch(texts[i:i + 100])
                total_embedded += sum(1 for v in vecs if v is not None)
                async with conn.transaction():
                    w = await upsert_jobs(conn, name, rows, vecs)
                    await conn.execute(
                        """INSERT INTO rs_ingest_checkpoint (source, cursor_key, status, n_seen, n_written)
                           VALUES ('jobs',$1,'done',$2,$3)
                           ON CONFLICT (source, cursor_key) DO UPDATE SET
                             status='done', n_seen=EXCLUDED.n_seen, n_written=EXCLUDED.n_written, updated_at=now()""",
                        key, len(rows), w)
                total_written += w
                print(f"[live] {key:28s} {'(aggregator)':20s} {len(rows):4d} jobs → {w} upserted")

        # WORKDAY pass (skip with --no-workday): the biggest single source. Per-tenant cxs API,
        # checkpointed per tenant, resumable. Tenant configs from WORKDAY_TENANTS or --workday-file.
        if not args.no_workday:
            for cfg in workday_tenants:
                company = cfg[0]; key = f"workday:{cfg[1]}"
                if key in already:
                    continue
                if args.limit and boards_done >= args.limit:
                    break
                boards_done += 1
                try:
                    rows = fetch_workday(cfg)
                except Exception as e:   # noqa: BLE001 — a wrong/renamed tenant must not abort the sweep
                    print(f"[skip] {key} ({company}): fetch failed: {e}", file=sys.stderr)
                    continue
                total_seen += len(rows)
                if args.dry:
                    print(f"[dry ] {key:28s} {company:20s} {len(rows):4d} jobs")
                    continue
                vecs = [None] * len(rows)
                texts = [f"{r.get('title','')} at {company}. {r.get('location','')}." for r in rows]
                for i in range(0, len(texts), 100):
                    vecs[i:i + 100] = embed_batch(texts[i:i + 100])
                total_embedded += sum(1 for v in vecs if v is not None)
                async with conn.transaction():
                    w = await upsert_jobs(conn, "workday", rows, vecs, company_default=company)
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
