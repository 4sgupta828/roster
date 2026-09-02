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


# ---- Eightfold (Netflix, Micron, PayPal, … — the enterprise "talent intelligence" ATS). Boards live
# on a per-company host (a custom careers domain or <slug>.eightfold.ai) and answer a public JSON
# search API; `domain=<slug>.com` selects the tenant. A hit's host names the company itself, which is
# the identity guard's strongest signal. Paginated (start/num), bounded to 2,000 postings. ----
_EF_HOSTS = ("{t}.eightfold.ai", "careers.{t}.com", "jobs.{t}.com", "explore.jobs.{t}.net",
             "careers.{t}.net", "jobs.{t}.net")
_EF_FOUND: dict[str, str] = {}      # token → host that answered (for board identity)


def _eightfold_page(host: str, domain: str, start: int, num: int) -> dict:
    return ij._http_json(f"https://{host}/api/apply/v2/jobs?domain={domain}&start={start}&num={num}")


def fetch_eightfold(token: str) -> list[dict]:
    domain = f"{token}.com"
    host_hit, first = "", None
    for pat in _EF_HOSTS:
        host = pat.format(t=token)
        try:
            d = _eightfold_page(host, domain, 0, 100)
        except Exception:  # noqa: BLE001 — DNS/404/HTML → not this host
            continue
        if isinstance(d, dict) and isinstance(d.get("positions"), list) and "count" in d:
            host_hit, first = host, d
            break
    if not host_hit:
        return []
    _EF_FOUND[token] = host_hit
    out, start, total = [], 0, int(first.get("count") or 0)
    d = first
    while True:
        for j in d.get("positions") or []:
            out.append({"title": j.get("name") or j.get("posting_name") or "",
                        "location": j.get("location") or ", ".join(j.get("locations") or []),
                        "department": j.get("department") or "",
                        "url": j.get("canonicalPositionUrl") or f"https://{host_hit}/careers/job/{j.get('id')}"})
        got = len(d.get("positions") or [])
        if not got:
            break
        start += got                       # the API may page fewer than requested (50 for Netflix)
        if start >= min(total, 2000):
            break
        try:
            d = _eightfold_page(host_hit, domain, start, 100)
        except Exception:  # noqa: BLE001
            break
    return out


PROBES = {"greenhouse": ij.fetch_greenhouse, "lever": ij.fetch_lever, "ashby": ij.fetch_ashby,
          "smartrecruiters": fetch_smartrecruiters, "workable": fetch_workable,
          "breezy": fetch_breezy, "eightfold": fetch_eightfold}


# ---- WRONG-COMPANY GUARD (evidence-identity directive) ------------------------------------------
# A slug variant ("eli", "alphabet") can be owned by an UNRELATED small company on that ATS; blindly
# upserting its jobs under the F2000 display name is wrong-company attribution — the exact failure
# class the repo's evidence discipline forbids. Every hit is verified against the board OWNER's own
# name (fetched cheaply, hits only) before ingestion.
def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _SUFFIX.sub(" ", (s or "").lower()))


def identity_ok(board_name: str, company: str, token: str) -> bool:
    """True iff the board plausibly belongs to `company`. Unverifiable boards (no name surfaced)
    are accepted only on the full-name slug (≥6 chars) — never on a short first-word variant."""
    base = _norm_name(company)
    if not board_name:
        full = slug_variants(company)
        return bool(full) and token == full[0] and len(token) >= 6
    b = _norm_name(re.sub(r"\b(jobs?|careers?|career page|job board|at)\b", " ", board_name, flags=re.I))
    if not b or not base:
        return False
    if b == base or _norm_name(board_name) == base:
        return True
    # "openai" ⊂ "openaijobs" fine; "alphabet" ⊂ "alphabettranslationservices" is NOT — bound the
    # length ratio so a generic-word company name can't swallow an unrelated longer board name.
    if base in b and len(b) <= 1.5 * len(base):
        return True
    # Board name ⊂ company base ("Intuitive" ⊂ intuitivesurgical ✓) — but a SHORT brand fragment
    # ("Novo" ⊂ novonordisk, an unrelated fintech's board) must not pass: require the board name to
    # cover at least half the company base and 5+ chars.
    return b in base and len(b) >= max(5, 0.5 * len(base))


def board_identity(ats: str, token: str) -> str:
    """The board OWNER's display name — API metadata where available, else the hosted page title."""
    try:
        if ats == "eightfold":
            host = _EF_FOUND.get(token, "")
            # a custom careers domain (careers.netflix.com / explore.jobs.netflix.net) IS the company;
            # <slug>.eightfold.ai only proves the slug — report the slug and let identity_ok decide
            return token if host else ""
        if ats == "greenhouse":
            return ij._http_json(f"https://boards-api.greenhouse.io/v1/boards/{token}").get("name") or ""
        if ats == "workable":
            return ij._http_json("https://apply.workable.com/api/v1/widget/accounts/"
                                 f"{token}?details=false").get("name") or ""
        if ats == "smartrecruiters":
            d = ij._http_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1")
            return (((d.get("content") or [{}])[0]).get("company") or {}).get("name") or ""
        page = {"lever": f"https://jobs.lever.co/{token}",
                "ashby": f"https://jobs.ashbyhq.com/{token}",
                "breezy": f"https://{token}.breezy.hr"}.get(ats)
        if page:
            req = urllib.request.Request(page, headers={**_UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=12) as r:
                m = re.search(rb"<title>([^<]{1,160})</title>", r.read(40000))
            return m.group(1).decode("utf-8", "ignore").strip() if m else ""
    except Exception:   # noqa: BLE001 — unverifiable, not fatal; identity_ok falls back strict
        pass
    return ""

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


_TICKERS: dict[str, str] = {}   # company title → ticker (SEC list) — an extra Workday tenant guess


def load_universe(top: int, names_file: str) -> list[str]:
    if names_file:
        # one company per line; an optional second TAB column is the ticker (SEC list staged from a
        # laptop — sec.gov serves 403 to datacenter IPs)
        out = []
        with open(names_file) as fh:
            for ln in fh:
                parts = [x.strip() for x in ln.rstrip("\n").split("\t")]
                if not parts or not parts[0]:
                    continue
                out.append(parts[0])
                if len(parts) > 1 and 2 <= len(parts[1]) <= 12:
                    _TICKERS[parts[0]] = parts[1].lower().replace("-", "").replace(".", "")
        return out[:top or None]
    req = urllib.request.Request(_SEC_TICKERS, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    rows = list(d.values())[: top or 2000]
    for v in rows:
        t = str(v.get("ticker") or "").lower().replace("-", "").replace(".", "")
        if 2 <= len(t) <= 12:
            _TICKERS[v["title"]] = t
    return [v["title"] for v in rows]


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
        if live:                       # dry runs record NOTHING — they must not poison live resume
            async with lock:
                await record_probe(conn, ats, token, len(rows))
        if not rows:
            return
        ident = await asyncio.to_thread(board_identity, ats, token)
        if not identity_ok(ident, company, token):
            stats["rejected"] = stats.get("rejected", 0) + 1
            print(f"[rej ] {ats:16s} {token:24s} board='{(ident or '?')[:32]}' ≠ {company[:28]} "
                  f"— wrong-company guard", flush=True)
            if live:                              # remember the rejection (never re-probed)
                async with lock:
                    await record_probe(conn, ats, token, -len(rows))
            return
        stats["hits"] += 1
        stats["jobs"] += len(rows)
        print(f"[hit ] {ats:16s} {token:24s} {company[:28]:28s} {len(rows):5d} jobs "
              f"(board='{(ident or token)[:28]}')", flush=True)
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
        # tenant guesses: full slug, first word, and the stock ticker (Workday tenants are very often
        # the ticker or the short brand — 'nvidia', 'wm', 'pfizer'); every guess is verified by the
        # tenant's own site redirect before any job is read
        slugs = slug_variants(company)[:2]
        tk = _TICKERS.get(company)
        if tk and tk not in slugs:
            slugs.append(tk)
        for slug in slugs:
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
          f"wrong-company rejected: {stats.get('rejected', 0)} | jobs seen: {stats['jobs']} | "
          f"upserted: {stats['written'] if live else 0} ({'DRY' if not live else 'live'}) | "
          f"est. embed cost: ~${est_cost:.2f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
