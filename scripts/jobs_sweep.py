#!/usr/bin/env python
"""Committed, resumable JOBS SWEEP — probe the Fortune-2000-scale company universe for public job
boards (6 ATS APIs + Workday tenant discovery) and ingest every hit into rs_job.

Universe: the SEC company_tickers.json list is MARKET-CAP ORDERED, so `--top 2000` is a clean
US-listed Fortune/Global-2000 proxy (free, no vendored list); `--names-file` overrides with one
company name per line (use it for private F2000 members).

Run in prod (the container has ROSTER_CORPUS_DSN + OPENAI_API_KEY):
    railway ssh --service roster-api "python scripts/jobs_sweep.py --dry --top 40"       # free probe
    railway ssh --service roster-api "setsid nohup python scripts/jobs_sweep.py --live --top 2000 \
        > /tmp/sweep.log 2>&1 &"                                                          # the sweep
    railway ssh --service roster-api "python scripts/jobs_sweep.py --live --top 2000 --workday"

RESUMABLE: every (ats, token) probe — hit or miss — is recorded in rs_ats_probed and never
re-probed (a redeploy that kills the process loses nothing; relaunch continues). IDEMPOTENT:
ingestion reuses ingest_jobs.upsert_jobs (ON CONFLICT on rs_job's real unique key). Cost:
ATS probes are free HTTP; the only spend is embeddings (~$0.02/1M tokens — pennies per 10k jobs).
Politeness: bounded concurrency (default 16), one attempt per token, no retry hammering.
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ingest_jobs as ij   # committed fetchers + upsert + embed + checkpoint (single source of truth)

_SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_UA = {"User-Agent": "roster-jobs-sweep/1.0 (contact: ops@roster)"}

# Legal-form suffixes stripped when slugifying a registered company name ("ACME WORKS INC" → acme works).
_SUFFIX = re.compile(
    r"\b(incorporated|inc|corp|corporation|company|co|ltd|limited|plc|llc|lp|sa|nv|se|ag|"
    r"holdings?|group|international|intl|enterprises?|technologies|technology|tech)\b\.?", re.I)


def slug_variants(title: str) -> list[str]:
    """Candidate ATS tokens for a company display name, most-likely first. Bounded to 3."""
    base = _SUFFIX.sub(" ", (title or "").lower())
    base = re.sub(r"[^a-z0-9 ]+", " ", base).strip()
    words = base.split()
    if not words:
        return []
    joined = "".join(words)
    out = [joined]
    if len(words) > 1:
        out.append("-".join(words))
        out.append(words[0])          # "acme works" → sometimes just "acme"
    seen, uniq = set(), []
    for s in out:
        if 2 < len(s) <= 40 and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:3]


# ---- The three ATS fetchers ingest_jobs doesn't have (same normalized-row contract) -------------
def fetch_smartrecruiters(token: str) -> list[dict]:
    out, offset = [], 0
    while True:                        # paginated, bounded by the API's own totalFound
        d = ij._http_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
                          f"?limit=100&offset={offset}")
        for j in d.get("content", []):
            loc = j.get("location") or {}
            out.append({"title": j.get("name") or "",
                        "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x),
                        "department": (j.get("department") or {}).get("label") or "",
                        "url": f"https://jobs.smartrecruiters.com/{token}/{j.get('id')}"})
        offset += 100
        if offset >= int(d.get("totalFound") or 0) or offset >= 1500:
            break
    return out


def fetch_workable(token: str) -> list[dict]:
    d = ij._http_json(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=false")
    return [{"title": j.get("title") or "", "location": j.get("location") or "",
             "department": j.get("department") or "", "url": j.get("url") or ""}
            for j in d.get("jobs", [])]


def fetch_breezy(token: str) -> list[dict]:
    d = ij._http_json(f"https://{token}.breezy.hr/json")
    return [{"title": j.get("name") or "",
             "location": ((j.get("location") or {}).get("name") or ""),
             "department": j.get("department") or "", "url": j.get("url") or ""}
            for j in (d if isinstance(d, list) else [])]


PROBES = {"greenhouse": ij.fetch_greenhouse, "lever": ij.fetch_lever, "ashby": ij.fetch_ashby,
          "smartrecruiters": fetch_smartrecruiters, "workable": fetch_workable,
          "breezy": fetch_breezy}

# ---- Workday tenant discovery (the F2000 heavyweight ATS; custom per-tenant configs) ------------
_WD_HOSTS = ("wd1", "wd5", "wd3", "wd2", "wd12")
_WD_SITES = ("External", "careers", "Careers", "External_Career_Site", "ExternalCareerSite",
             "external_experienced", "External_Careers")


def discover_workday(slug: str) -> tuple | None:
    """Find a live Workday cxs config for `slug`: (company, host, wd, tenant, site) or None.
    A dead tenant host fails DNS instantly, so misses are cheap; sites are probed only on a live
    host. One page probed (limit 1) — the full paginated pull happens in ij.fetch_workday."""
    for wd in _WD_HOSTS:
        api_base = f"https://{slug}.{wd}.myworkdayjobs.com/wday/cxs/{slug}"
        # The tenant landing page redirects to /<lang>/<site> — one request reveals the REAL site
        # name (e.g. nvidia → /en-US/NVIDIAExternalCareerSite, adobe → /en-US/external_experienced).
        redirected: tuple[str, ...] = ()
        try:
            req0 = urllib.request.Request(f"https://{slug}.{wd}.myworkdayjobs.com/", headers=_UA)
            with urllib.request.urlopen(req0, timeout=10) as r0:
                parts = [p for p in r0.geturl().split("?")[0].split("/") if p]
                if parts and parts[-1] not in (f"{slug}.{wd}.myworkdayjobs.com",):
                    redirected = (parts[-1],)
        except urllib.error.HTTPError:
            pass                       # host is ALIVE (HTTP answered) — probe site candidates
        except Exception:              # noqa: BLE001 — DNS/conn failure = no tenant on this host
            continue
        sites = redirected + _WD_SITES + (slug, slug.upper(),
                                          f"{slug.upper()}ExternalCareerSite",
                                          f"{slug.capitalize()}ExternalCareerSite",
                                          f"{slug}ExternalCareerSite")
        for site in sites:
            body = json.dumps({"appliedFacets": {}, "limit": 1, "offset": 0,
                               "searchText": ""}).encode()
            req = urllib.request.Request(f"{api_base}/{site}/jobs", data=body,
                                         headers={**_UA, "Content-Type": "application/json",
                                                  "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    d = json.load(r)
                if int(d.get("total") or 0) > 0 or d.get("jobPostings") is not None:
                    return (slug, slug, wd.removeprefix("wd"), slug, site)
            except urllib.error.HTTPError as e:
                if e.code in (404, 422, 400):
                    continue           # wrong site name — try the next candidate
                break                  # 403/5xx → wrong/hostile HOST — try the next wd host
            except Exception:          # noqa: BLE001 — DNS failure = no tenant on this host
                break                  # next wd host
    return None


async def already_probed(conn) -> set[str]:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS rs_ats_probed (
             ats text NOT NULL, token text NOT NULL, hit int NOT NULL DEFAULT 0,
             probed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (ats, token))""")
    # The table may pre-exist from the 2026-08-31 in-container sweep with a leaner schema —
    # migrate additively so that sweep's ~140k probe records still count as done.
    await conn.execute("ALTER TABLE rs_ats_probed ADD COLUMN IF NOT EXISTS hit int NOT NULL DEFAULT 0")
    await conn.execute("ALTER TABLE rs_ats_probed ADD COLUMN IF NOT EXISTS probed_at timestamptz "
                       "NOT NULL DEFAULT now()")
    rows = await conn.fetch("SELECT ats, token FROM rs_ats_probed")
    return {f"{r['ats']}:{r['token']}" for r in rows}


async def record_probe(conn, ats: str, token: str, hit: int) -> None:
    await conn.execute(
        """INSERT INTO rs_ats_probed (ats, token, hit) VALUES ($1,$2,$3)
           ON CONFLICT (ats, token) DO UPDATE SET hit=EXCLUDED.hit, probed_at=now()""",
        ats, token, hit)


def load_universe(top: int, names_file: str) -> list[str]:
    if names_file:
        with open(names_file) as fh:
            return [ln.strip() for ln in fh if ln.strip()][:top or None]
    req = urllib.request.Request(_SEC_TICKERS, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return [v["title"] for v in list(d.values())[: top or 2000]]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry", action="store_true", help="probe + count only; no writes/embeds")
    ap.add_argument("--top", type=int, default=2000, help="top-N companies (SEC market-cap order)")
    ap.add_argument("--names-file", default="", help="one company name per line (overrides SEC list)")
    ap.add_argument("--workday", action="store_true", help="also run Workday tenant discovery")
    ap.add_argument("--concurrency", type=int, default=16)
    args = ap.parse_args()
    live = args.live and not args.dry

    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        print("ROSTER_CORPUS_DSN not set", file=sys.stderr)
        sys.exit(2)
    import asyncpg
    conn = await asyncpg.connect(dsn)
    await ij.ensure_checkpoint(conn)
    done = await already_probed(conn)

    companies = load_universe(args.top, args.names_file)
    print(f"universe: {len(companies)} companies | already probed pairs: {len(done)}", flush=True)

    sem = asyncio.Semaphore(max(2, args.concurrency))
    lock = asyncio.Lock()              # serialize DB writes on the single connection
    stats = {"probed": 0, "hits": 0, "jobs": 0, "written": 0}

    async def probe_one(ats: str, token: str, company: str) -> None:
        key = f"{ats}:{token}"
        if key in done:
            return
        done.add(key)
        async with sem:
            try:
                rows = await asyncio.to_thread(PROBES[ats], token)
            except Exception:          # noqa: BLE001 — miss (404/timeout/parse) = probed, no hit
                rows = []
        stats["probed"] += 1
        async with lock:
            await record_probe(conn, ats, token, len(rows))
        if not rows:
            return
        stats["hits"] += 1
        stats["jobs"] += len(rows)
        print(f"[hit ] {ats:16s} {token:24s} {company[:28]:28s} {len(rows):5d} jobs", flush=True)
        if not live:
            return
        texts = [f"{r.get('title','')} at {company}. {r.get('location','')}. {r.get('department','')}"
                 for r in rows]
        vecs: list = [None] * len(rows)
        for i in range(0, len(texts), 100):
            vecs[i:i + 100] = await asyncio.to_thread(ij.embed_batch, texts[i:i + 100])
        async with lock:
            async with conn.transaction():
                w = await ij.upsert_jobs(conn, ats, rows, vecs, company_default=company)
                await conn.execute(
                    """INSERT INTO rs_ingest_checkpoint (source, cursor_key, status, n_seen, n_written)
                       VALUES ('jobs',$1,'done',$2,$3)
                       ON CONFLICT (source, cursor_key) DO UPDATE SET
                         status='done', n_seen=EXCLUDED.n_seen, n_written=EXCLUDED.n_written,
                         updated_at=now()""", key, len(rows), w)
        stats["written"] += w

    async def workday_one(company: str) -> None:
        slugs = slug_variants(company)
        for slug in slugs[:2]:
            key = f"workday:{slug}"
            if key in done:
                continue
            done.add(key)
            async with sem:
                cfg = await asyncio.to_thread(discover_workday, slug)
            async with lock:
                await record_probe(conn, "workday", slug, 1 if cfg else 0)
            if not cfg:
                continue
            _, host, wd, tenant, site = cfg
            print(f"[wday] discovered {host}.wd{wd} tenant={tenant} site={site} ({company[:30]})",
                  flush=True)
            if not live:
                return
            try:
                rows = await asyncio.to_thread(ij.fetch_workday, (company, host, wd, tenant, site))
            except Exception:          # noqa: BLE001
                return
            if not rows:
                return
            stats["hits"] += 1
            stats["jobs"] += len(rows)
            texts = [f"{r.get('title','')} at {company}. {r.get('location','')}." for r in rows]
            vecs: list = [None] * len(rows)
            for i in range(0, len(texts), 100):
                vecs[i:i + 100] = await asyncio.to_thread(ij.embed_batch, texts[i:i + 100])
            async with lock:
                async with conn.transaction():
                    w = await ij.upsert_jobs(conn, "workday", rows, vecs, company_default=company)
                    await conn.execute(
                        """INSERT INTO rs_ingest_checkpoint (source, cursor_key, status, n_seen, n_written)
                           VALUES ('jobs',$1,'done',$2,$3)
                           ON CONFLICT (source, cursor_key) DO UPDATE SET
                             status='done', n_seen=EXCLUDED.n_seen, n_written=EXCLUDED.n_written,
                             updated_at=now()""", key, len(rows), w)
            stats["written"] += w
            return                     # one tenant per company is enough

    try:
        tasks = []
        for company in companies:
            for token in slug_variants(company):
                for ats in PROBES:
                    tasks.append(probe_one(ats, token, company))
        # bounded batches so the task list (≤ 2000×3×6 = 36k coroutines) never floods memory
        for i in range(0, len(tasks), 600):
            await asyncio.gather(*tasks[i:i + 600])
            if i % 6000 == 0:
                print(f"  … progress: {stats}", flush=True)
        if args.workday:
            wtasks = [workday_one(c) for c in companies]
            for i in range(0, len(wtasks), 200):
                await asyncio.gather(*wtasks[i:i + 200])
    finally:
        await conn.close()
    est_cost = stats["jobs"] * 20 / 1_000_000 * 0.02
    print(f"\n=== sweep summary ===\nprobed: {stats['probed']} | boards hit: {stats['hits']} | "
          f"jobs seen: {stats['jobs']} | upserted: {stats['written'] if live else 0} "
          f"({'DRY' if not live else 'live'}) | est. embed cost: ~${est_cost:.2f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
