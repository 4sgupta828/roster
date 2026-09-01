#!/usr/bin/env python
"""Committed, resumable PEOPLE ingester — GitHub profiles → rs_entity(kind=person) + facets + vector.

Runs with prod env injected (local IP for the GitHub API, prod DB for writing):
    railway run --service roster-api python scripts/ingest_people.py --dry  --limit 20
    railway run --service roster-api python scripts/ingest_people.py --live --limit 20   # tiny paid validation
    railway run --service roster-api python scripts/ingest_people.py --live               # full window sweep

Needs ROSTER_CORPUS_DSN (write target). For --live: ROSTER_GITHUB_TOKEN (5000/hr search+hydrate) and
an LLM key — OPENAI_API_KEY (gpt-4o-mini facet extraction) or DEEPSEEK_API_KEY — plus OPENAI_API_KEY
for the profile embedding. IDEMPOTENT: entity_id = 'github:<login>' upsert; facets upsert on
(tenant,entity,facet_key,value); vector upsert on entity_id. RESUMABLE: each search WINDOW is
checkpointed in rs_ingest_checkpoint and skipped on re-run unless --refresh.

MEANING IS THE MODEL'S (repo Rule 18): role/seniority/function/skill/company/metro/country are
extracted by the LLM from the profile, never regex-guessed. Structural facets (the person's links)
are code-derived. --dry does GitHub fetch only — no LLM, no embed, no write, ZERO cost — so you can
see who'd be ingested and the per-window yield before spending anything.

Scaling to 2x: WINDOWS below tile GitHub's 1000-result-per-query cap across many
(location × language × follower-range) slices; add slices to cover more of the graph.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request

# Search WINDOWS tile the 1000-results-per-query cap. Each is a GitHub user-search qualifier string,
# paged (100/page × up to 10 pages = 1000) and sorted by followers so prominent people come first.
# location × language across many hubs gives the breadth to reach a 2x (~350k) target; multi-word
# locations MUST be quoted or GitHub splits them into a free-text term (the New-York under-yield bug).
_CITIES = (
    "San Francisco", "New York", "Seattle", "Los Angeles", "Boston", "Austin", "Chicago", "Denver",
    "Portland", "Atlanta", "Toronto", "Vancouver", "London", "Berlin", "Paris", "Amsterdam", "Madrid",
    "Barcelona", "Munich", "Zurich", "Dublin", "Stockholm", "Copenhagen", "Warsaw", "Lisbon",
    "Bangalore", "Bengaluru", "Hyderabad", "Pune", "Delhi", "Mumbai", "Chennai", "Singapore", "Tokyo",
    "Seoul", "Shanghai", "Beijing", "Shenzhen", "Sydney", "Melbourne", "Tel Aviv", "Sao Paulo",
    "Buenos Aires", "Mexico City", "Lagos", "Nairobi", "Cairo", "Jakarta", "Remote",
)
_LANGS = (
    "Python", "TypeScript", "JavaScript", "Go", "Rust", "Java", "C++", "Ruby", "Scala", "Kotlin",
    "Swift", "C#", "PHP", "Elixir", "Julia", "Haskell", "R", "Dart", "Clojure", "Solidity",
)
WINDOWS: list[str] = [f'location:"{c}" language:{lang}' for c in _CITIES for lang in _LANGS]

_GH = "https://api.github.com"


_MAX_SLEEP = 3660   # cap any single rate-limit sleep at ~61 min (a full GitHub window) — never spin


def _sleep_until_reset(headers, *, retry_after: str = "") -> None:
    """Sleep until a GitHub bucket resets. Honors Retry-After (secondary-limit / abuse) first, else
    X-RateLimit-Reset. GitHub explicitly warns NOT to keep calling before the reset — doing so risks a
    ban — so we sleep the FULL window (bounded to _MAX_SLEEP), never a fixed short cap."""
    wait = 0
    try:
        if retry_after:
            wait = int(float(retry_after)) + 2
        else:
            reset = int(headers.get("X-RateLimit-Reset", "0"))
            wait = max(0, reset - int(time.time())) + 2
    except Exception:   # noqa: BLE001
        wait = 60
    wait = max(1, min(wait, _MAX_SLEEP))
    print(f"  … rate-limited; sleeping {wait}s (until bucket reset)", file=sys.stderr)
    time.sleep(wait)


def _gh_get(path: str, *, token: str, params: dict | None = None):
    """GitHub GET honoring per-bucket limits + Retry-After. On 403/429 (primary or secondary limit),
    sleep until the bucket resets and retry once; the search bucket (30/min) and core bucket (5k/hr) are
    handled naturally because each response carries its OWN bucket's headers."""
    url = _GH + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in (1, 2):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "User-Agent": "roster-people-ingest/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r), dict(r.headers)
        except urllib.error.HTTPError as e:
            hdr = dict(e.headers or {})
            # 403 with remaining=0 OR 429 = rate/secondary limit → wait for reset, then retry once.
            rem = hdr.get("X-RateLimit-Remaining")
            if e.code in (403, 429) and (e.code == 429 or rem == "0" or hdr.get("Retry-After")) and attempt == 1:
                _sleep_until_reset(hdr, retry_after=hdr.get("Retry-After", ""))
                continue
            raise


def _throttle(headers: dict) -> None:
    """Proactively pace: when the just-used bucket is nearly exhausted, sleep until it resets so the
    NEXT call doesn't trip a 403/429. Search calls carry the 30/min bucket, hydrate the 5k/hr core."""
    try:
        rem = int(headers.get("X-RateLimit-Remaining", "999"))
        if rem <= 1:
            _sleep_until_reset(headers)
    except Exception:   # noqa: BLE001
        pass


def search_logins(window: str, *, token: str, cap: int) -> list[str]:
    """Enumerate up to `cap` user logins for one search window (paged, rate-limit aware)."""
    logins, page = [], 1
    while len(logins) < cap and page <= 10:
        data, hdr = _gh_get("/search/users", token=token,
                            params={"q": f"type:user {window}", "per_page": 100, "page": page,
                                    "sort": "followers", "order": "desc"})
        items = data.get("items", [])
        if not items:
            break
        logins += [it["login"] for it in items]
        _throttle(hdr)
        page += 1
    return logins[:cap]


def hydrate(login: str, *, token: str) -> dict | None:
    try:
        u, hdr = _gh_get(f"/users/{login}", token=token)
        _throttle(hdr)
        return u
    except Exception as e:   # noqa: BLE001 — a single bad profile must not abort the window
        print(f"  ! hydrate {login} failed: {e}", file=sys.stderr)
        return None


# ---- LLM facet extraction (Rule 18: the model owns meaning) ----
_FACET_SYS = (
    "You extract normalized professional FACETS from a GitHub profile. Return STRICT JSON: "
    '{"role":"","seniority":"","function":"","company":"","metro":"","country":"","skills":[]}. '
    "role: a normalized job role (snake_case, e.g. software_engineer, ml_engineer, data_scientist, "
    "designer, product_manager) or '' if unclear. seniority: one of intern|junior|mid|senior|staff|"
    "principal|lead|manager|director|vp|c_level or ''. function: the discipline (snake_case, e.g. "
    "machine_learning, backend, frontend, devops, security, data) or ''. company: current employer, "
    "lowercased short name, or ''. metro: normalized metro (e.g. bay_area, new_york, london) or ''. "
    "country: ISO-ish lowercase (us, uk, de, in, ca) or ''. skills: up to 6 lowercased skill tokens. "
    "Infer ONLY from the given profile; never guess a value that isn't supported. Unknown → empty."
)


def extract_facets(profiles: list[dict]) -> list[dict]:
    """One LLM call per profile-batch → per-profile facet dicts. Prefers DeepSeek (cheap) then OpenAI
    gpt-4o-mini. Fail-safe: on any error return empty facet dicts (person still ingested w/ links only)."""
    ds, oa = os.environ.get("DEEPSEEK_API_KEY"), os.environ.get("OPENAI_API_KEY")
    if ds:
        endpoint, key, model = "https://api.deepseek.com/chat/completions", ds, "deepseek-chat"
    elif oa:
        endpoint, key, model = "https://api.openai.com/v1/chat/completions", oa, "gpt-4o-mini"
    else:
        return [{} for _ in profiles]
    out = []
    for p in profiles:
        card = {k: p.get(k) for k in ("login", "name", "bio", "company", "location", "blog",
                                       "twitter_username", "hireable", "public_repos")}
        try:
            body = json.dumps({"model": model, "temperature": 0,
                               "response_format": {"type": "json_object"},
                               "messages": [{"role": "system", "content": _FACET_SYS},
                                            {"role": "user", "content": json.dumps(card)}]}).encode()
            req = urllib.request.Request(endpoint, data=body,
                                         headers={"Authorization": "Bearer " + key,
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=45) as r:
                txt = json.load(r)["choices"][0]["message"]["content"]
            out.append(json.loads(txt))
        except Exception as e:   # noqa: BLE001 — fail safe, never crash the sweep
            print(f"  ! facet extract failed for {p.get('login')}: {e}", file=sys.stderr)
            out.append({})
    return out


def embed_one(text: str) -> str | None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key or not (text or "").strip():
        return None
    try:
        body = json.dumps({"model": "text-embedding-3-small", "input": [text[:2000]]}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body,
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            v = json.load(r)["data"][0]["embedding"]
        return "[" + ",".join(f"{x:.6f}" for x in v) + "]"
    except Exception:   # noqa: BLE001
        return None


def _facet_rows(fac: dict, profile: dict) -> list[tuple[str, str, str]]:
    """(facet_key, value_norm, display) rows — LLM semantic facets + code-derived link_* facets."""
    rows: list[tuple[str, str, str]] = []
    for k in ("role", "seniority", "function", "company", "metro", "country"):
        v = str(fac.get(k) or "").strip().lower().replace(" ", "_")
        if v:
            rows.append((k, v, str(fac.get(k))))
    for s in (fac.get("skills") or [])[:6]:
        sv = str(s).strip().lower().replace(" ", "_")
        if sv:
            rows.append(("skill", sv, str(s)))
    if profile.get("name") or profile.get("bio"):
        rows.append(("title", (profile.get("bio") or profile.get("name") or "")[:120].strip().lower(),
                     (profile.get("bio") or profile.get("name") or "")[:120]))
    # structural link facets (code-derived, not semantic)
    if profile.get("html_url"):
        rows.append(("link_github", profile["html_url"].lower(), profile["html_url"]))
    if profile.get("twitter_username"):
        rows.append(("link_x", profile["twitter_username"].lower(),
                     "https://x.com/" + profile["twitter_username"]))
    if profile.get("email"):
        rows.append(("link_email", profile["email"].lower(), "mailto:" + profile["email"]))
    if profile.get("blog"):
        rows.append(("link_website", profile["blog"].lower(), profile["blog"]))
    return rows


async def ensure_checkpoint(conn) -> None:
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS rs_ingest_checkpoint (
             source text NOT NULL, cursor_key text NOT NULL, status text NOT NULL DEFAULT 'done',
             n_seen int NOT NULL DEFAULT 0, n_written int NOT NULL DEFAULT 0,
             updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (source, cursor_key))""")


async def done_windows(conn) -> set[str]:
    rows = await conn.fetch("SELECT cursor_key FROM rs_ingest_checkpoint WHERE source='people' AND status='done'")
    return {r["cursor_key"] for r in rows}


async def upsert_person(conn, profile: dict, fac: dict, vec: str | None) -> None:
    login = profile["login"]
    eid = "github:" + login
    name = profile.get("name") or login
    # TAKEDOWN honored across re-ingest: a suppressed person is never re-added (opt-out is durable).
    if (await conn.fetchval("SELECT status FROM rs_entity WHERE entity_id=$1", eid)) == "suppressed":
        return
    await conn.execute(
        """INSERT INTO rs_entity (entity_id, tenant_id, kind, name, facets, retrieved_at)
           VALUES ($1,'demo','person',$2,$3::jsonb, now())
           ON CONFLICT (entity_id) DO UPDATE SET name=EXCLUDED.name, facets=EXCLUDED.facets, retrieved_at=now()""",
        eid, name, json.dumps({k: profile.get(k) for k in ("company", "location", "bio", "html_url")}))
    for fk, vnorm, disp in _facet_rows(fac, profile):
        if not vnorm:
            continue
        await conn.execute(
            """INSERT INTO roster_entity_facet (tenant_id, entity_id, facet_key, facet_value_norm,
                 display_value, source_document_id, confidence)
               VALUES ('demo',$1,$2,$3,$4,$5,$6)
               ON CONFLICT (tenant_id, entity_id, facet_key, facet_value_norm) DO UPDATE SET
                 display_value=EXCLUDED.display_value, confidence=EXCLUDED.confidence""",
            eid, fk, vnorm[:200], disp[:300], profile.get("html_url") or "", 0.6)
    if vec is not None:
        await conn.execute(
            """INSERT INTO rs_person_vec (entity_id, embedding) VALUES ($1,$2::vector)
               ON CONFLICT (entity_id) DO UPDATE SET embedding=EXCLUDED.embedding""", eid, vec)


async def backfill_embeddings(conn, limit: int) -> tuple[int, int]:
    """Embed people who have NO rs_person_vec row (embed failed at ingest, or never ran) so they become
    visible to semantic search. Blurb is rebuilt from stored facets — no GitHub/LLM calls, embeddings
    only. Returns (embedded, seen)."""
    rows = await conn.fetch(
        """SELECT e.entity_id, e.name, e.facets FROM rs_entity e
           WHERE e.kind='person'
             AND NOT EXISTS (SELECT 1 FROM rs_person_vec v WHERE v.entity_id = e.entity_id)
           LIMIT $1""", int(limit))
    n = 0
    for r in rows:
        fj = r["facets"]
        if isinstance(fj, str):
            try:
                fj = json.loads(fj)
            except Exception:   # noqa: BLE001
                fj = {}
        fj = fj if isinstance(fj, dict) else {}
        frows = await conn.fetch(
            "SELECT facet_key, display_value FROM roster_entity_facet WHERE entity_id=$1", r["entity_id"])
        parts = [r["name"]] + [str(fj.get(k, "")) for k in ("bio", "company", "location")]
        parts += [f["display_value"] for f in frows if not f["facet_key"].startswith("link_")]
        blurb = " ".join(p for p in parts if p).strip()
        vec = embed_one(blurb)
        if vec:
            await conn.execute(
                """INSERT INTO rs_person_vec (entity_id, embedding) VALUES ($1,$2::vector)
                   ON CONFLICT (entity_id) DO UPDATE SET embedding=EXCLUDED.embedding""", r["entity_id"], vec)
            n += 1
    return n, len(rows)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max PEOPLE to process this run (0 = all in the windows)")
    ap.add_argument("--per-window", type=int, default=1000, help="max logins pulled per search window (cap 1000)")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--backfill", type=int, default=0,
                    help="embed N people who have no vector yet (no GitHub/LLM; embeddings only) and exit")
    args = ap.parse_args()
    live = args.live and not args.dry

    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        print("ROSTER_CORPUS_DSN not set — run via `railway run --service roster-api ...`", file=sys.stderr)
        sys.exit(2)

    import asyncpg
    # BACKFILL mode: embeddings only, no GitHub — safe to run without a token.
    if args.backfill:
        conn = await asyncpg.connect(dsn)
        try:
            n, seen = await backfill_embeddings(conn, args.backfill)
        finally:
            await conn.close()
        print(f"backfill: embedded {n} of {seen} vectorless people (~${n*60/1_000_000*0.02:.4f})")
        return

    token = os.environ.get("ROSTER_GITHUB_TOKEN", "")
    if not token:
        print("ROSTER_GITHUB_TOKEN not set — GitHub search needs it (5000/hr).", file=sys.stderr)
        sys.exit(2)

    conn = await asyncpg.connect(dsn)
    await ensure_checkpoint(conn)
    already = set() if args.refresh else await done_windows(conn)

    seen = written = llm_calls = embeds = 0
    try:
        for window in WINDOWS:
            if window in already:
                continue
            if args.limit and seen >= args.limit:
                break
            try:
                logins = search_logins(window, token=token, cap=args.per_window)
            except Exception as e:   # noqa: BLE001
                print(f"[skip] window '{window}': search failed: {e}", file=sys.stderr)
                continue
            if args.limit:
                logins = logins[: max(0, args.limit - seen)]
            print(f"[{'live' if live else 'dry '}] window '{window}': {len(logins)} logins")
            profiles = [p for p in (hydrate(l, token=token) for l in logins) if p]
            seen += len(profiles)
            if args.dry:
                for p in profiles[:10]:
                    print(f"    {p.get('login'):20s} {(p.get('name') or ''):24s} "
                          f"{(p.get('company') or ''):16s} {(p.get('location') or '')}")
                continue
            facs = extract_facets(profiles); llm_calls += len(profiles)
            w_this = 0
            for p, fac in zip(profiles, facs):
                blurb = " ".join(str(x) for x in [p.get("name"), p.get("bio"), fac.get("role"),
                                                  fac.get("function"), " ".join(fac.get("skills") or []),
                                                  p.get("company"), p.get("location")] if x)
                vec = embed_one(blurb)
                if vec is not None:
                    embeds += 1
                async with conn.transaction():
                    await upsert_person(conn, p, fac, vec)
                w_this += 1
            written += w_this
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO rs_ingest_checkpoint (source, cursor_key, status, n_seen, n_written)
                       VALUES ('people',$1,'done',$2,$3)
                       ON CONFLICT (source, cursor_key) DO UPDATE SET
                         status='done', n_seen=EXCLUDED.n_seen, n_written=EXCLUDED.n_written, updated_at=now()""",
                    window, len(profiles), w_this)
            print(f"       → {w_this} people upserted")
    finally:
        await conn.close()

    # cost accounting: LLM facet calls + embeddings
    llm_cost = llm_calls / 1_000_000 * 400 * 0.15      # ~400 in-tokens/profile, ~$0.15/1M (gpt-4o-mini in)
    emb_cost = embeds * 60 / 1_000_000 * 0.02          # ~60 tokens/blurb, $0.02/1M
    print("\n=== summary ===")
    print(f"people seen: {seen} | {'DRY (no writes)' if not live else f'upserted: {written} | llm: {llm_calls} | embedded: {embeds}'}")
    if live:
        tot = llm_cost + emb_cost
        print(f"est. cost this run: ~${tot:.4f}  (~${tot / max(written,1) * 1000:.3f} / 1k people)")


if __name__ == "__main__":
    asyncio.run(main())
