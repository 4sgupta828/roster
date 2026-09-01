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

# Search WINDOWS tile the 1000/query cap. Each is a GitHub user-search qualifier string; we page it
# (100/page × up to 10 pages = 1000). Keep them disjoint-ish so we cover breadth, not the same 1000.
WINDOWS: list[str] = [
    "location:%s followers:%s language:%s" % (loc, fol, lang)
    for loc in ("San Francisco", "New York", "Seattle", "London", "Berlin", "Bangalore", "Toronto", "Remote")
    for fol in (">500", "200..500", "100..199")
    for lang in ("Python", "TypeScript", "Go", "Rust")
]

_GH = "https://api.github.com"


def _gh_get(path: str, *, token: str, params: dict | None = None):
    url = _GH + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "User-Agent": "roster-people-ingest/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r), dict(r.headers)


def _throttle(headers: dict) -> None:
    """Respect GitHub's rate limit: if the remaining budget is low, sleep until reset."""
    try:
        rem = int(headers.get("X-RateLimit-Remaining", "1"))
        if rem <= 1:
            reset = int(headers.get("X-RateLimit-Reset", "0"))
            wait = max(1, reset - int(time.time()) + 2)
            print(f"  … rate-limited; sleeping {wait}s", file=sys.stderr)
            time.sleep(min(wait, 90))
    except Exception:   # noqa: BLE001
        pass


def search_logins(window: str, *, token: str, cap: int) -> list[str]:
    """Enumerate up to `cap` user logins for one search window (paged, rate-limit aware)."""
    logins, page = [], 1
    while len(logins) < cap and page <= 10:
        data, hdr = _gh_get("/search/users", token=token,
                            params={"q": f"type:user {window}", "per_page": 100, "page": page})
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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max PEOPLE to process this run (0 = all in the windows)")
    ap.add_argument("--per-window", type=int, default=200, help="max logins pulled per search window")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    live = args.live and not args.dry

    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        print("ROSTER_CORPUS_DSN not set — run via `railway run --service roster-api ...`", file=sys.stderr)
        sys.exit(2)
    token = os.environ.get("ROSTER_GITHUB_TOKEN", "")
    if not token:
        print("ROSTER_GITHUB_TOKEN not set — GitHub search needs it (5000/hr).", file=sys.stderr)
        sys.exit(2)

    import asyncpg
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
