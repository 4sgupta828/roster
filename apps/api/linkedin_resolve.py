"""LinkedIn RESOLUTION via search-engine snippets — never by reading LinkedIn.

LinkedIn's terms forbid scraping and its profile pages are authwalled to bots, so Roster never
fetches linkedin.com. What IS public and quotable is the search engine's own snippet of a profile:
the result title ("Tom Brown - Co-Founder at Anthropic | LinkedIn") carries the person's
self-written HEADLINE, and the description carries a few more self-written words. This module:

  1. searches `"<name>" <hints> site:linkedin.com/in` (keyless DuckDuckGo HTML leg; Brave when keyed);
  2. keeps only linkedin.com/in/ results whose result-name MATCHES the person's name (code-owned);
  3. scores each by the person's GROUNDED hints (company / past employers / role / metro) found in the
     headline + snippet; resolves only when ONE name-matching profile carries a hint and — when we
     hold a company for the person — that hint includes the company. Anything less is 'ambiguous'
     (candidates shown, unverified) or 'none'. A guess is never written.

A resolved profile is stored as two SELF-STATED facets (family 'linkedin'): link_linkedin (the URL)
and linkedin_headline (the quoted headline). Because the profile was CHOSEN on name + company, its
agreement on company is CONSISTENCY between two self-authored sources — labeled that way, never
promoted to corroboration (see evidence.py).
"""
from __future__ import annotations

import logging
import re

_log = logging.getLogger("roster.linkedin")

_LI_URL = re.compile(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#\s]+", re.I)
_STOP = {"the", "and", "at", "of", "in", "for", "a", "an", "inc", "llc", "ltd", "corp", "co",
         "company", "university", "group", "labs", "lab", "ai", "technologies", "technology"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _tokens(s: str) -> list[str]:
    return [t for t in _norm(s).split() if t and t not in _STOP and len(t) >= 2]


def parse_title(title: str) -> tuple[str, str]:
    """'Tom Brown - Co-Founder at Anthropic | LinkedIn' → ('Tom Brown', 'Co-Founder at Anthropic').
    Also handles ' - LinkedIn' suffixes and the location-only variant."""
    t = re.sub(r"\s*[|\-–]\s*LinkedIn\s*$", "", (title or "").strip(), flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[-–|]\s+", t) if p.strip()]
    if not parts:
        return "", ""
    name = re.sub(r"^[^\w]+", "", parts[0]).strip()      # Brave prefixes some titles with '.'
    return name, " - ".join(parts[1:])


def name_matches(person: str, result_name: str) -> bool:
    """The result's name is the person's name: same first and last tokens (middle names/initials
    allowed in either), or identical normalized strings. 'Tom Brown' ≠ 'Tomer Brown'."""
    p, r = _norm(person).split(), _norm(result_name).split()
    if not p or not r:
        return False
    if p == r:
        return True
    if len(p) < 2 or len(r) < 2:
        return False
    return p[0] == r[0] and p[-1] == r[-1]


def hint_hits(hints: dict[str, list[str]], text: str) -> dict[str, list[str]]:
    """Which grounded hints appear in the headline/snippet text — per hint kind (company, worked_at,
    role, metro). A hint matches when ALL its meaningful tokens are present."""
    hay = " " + _norm(text) + " "
    out: dict[str, list[str]] = {}
    for kind, vals in (hints or {}).items():
        for v in vals or []:
            toks = _tokens(v)
            if toks and all((" " + t + " ") in hay or (t in hay) for t in toks):
                out.setdefault(kind, []).append(v)
    return out


def choose(person_name: str, hints: dict[str, list[str]], results: list[dict]) -> dict:
    """The resolution decision over search results ({url,title,snippet} each). Returns
    {status: resolved|ambiguous|none, match: {...}|None, candidates: [...]}. Code-owned; the
    company gate makes a same-named person at another company unresolvable rather than guessed."""
    cands = []
    for r in results or []:
        url = (r.get("url") or "").strip()
        m = _LI_URL.match(url)
        if not m:
            continue
        rname, headline = parse_title(r.get("title") or "")
        if not name_matches(person_name, rname):
            continue
        hits = hint_hits(hints, headline + " " + (r.get("snippet") or ""))
        score = 2 * len(hits.get("company", [])) + len(hits.get("worked_at", [])) \
            + len(hits.get("role", [])) + len(hits.get("metro", []))
        cands.append({"url": m.group(0), "name": rname, "headline": headline,
                      "snippet": (r.get("snippet") or "")[:300], "hits": hits, "score": score})
    if not cands:
        return {"status": "none", "match": None, "candidates": []}
    cands.sort(key=lambda c: -c["score"])
    top = cands[0]
    has_company_hint = bool((hints or {}).get("company"))
    company_ok = bool(top["hits"].get("company")) if has_company_hint else bool(top["score"])
    tied = [c for c in cands if c["score"] == top["score"]]
    if top["score"] > 0 and company_ok and len(tied) == 1:
        return {"status": "resolved", "match": top, "candidates": cands[:5]}
    return {"status": "ambiguous", "match": None, "candidates": cands[:5]}


def hints_from_row(row: dict) -> dict[str, list[str]]:
    """Grounded hints from a people row's attributes: company, past employers, role, metro."""
    out: dict[str, list[str]] = {}
    for a in row.get("attributes") or []:
        k, d = a.get("key") or "", str(a.get("display") or "").strip()
        if not d or len(d) < 2:
            continue
        if k in ("company", "worked_at", "role", "metro"):
            out.setdefault(k, []).append(d.replace("_", " "))
    return out


async def search_snippets(query: str, *, max_results: int = 10) -> list[dict]:
    """Search-engine results (url/title/snippet) — NO page fetches. Keyless DuckDuckGo HTML leg
    (works from the prod datacenter, verified); Brave when BRAVE_API_KEY is set."""
    import asyncio
    import os
    try:
        if os.environ.get("BRAVE_API_KEY"):
            from roster_kernel.providers.brave_web import BraveWebSearch
            res = await BraveWebSearch().search(query, max_results=max_results, open_web=True)
            return [{"url": r.url, "title": r.title, "snippet": r.snippet or ""} for r in res]
        return await asyncio.to_thread(_ddg_get, query, max_results)
    except Exception as e:  # noqa: BLE001 — the leg is best-effort
        _log.info("linkedin snippet search failed: %s", e)
        return []


_DDG_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124 Safari/537.36")


def _ddg_get(query: str, max_results: int) -> list[dict]:
    """DuckDuckGo HTML results via a plain GET (verified to return results from the prod datacenter
    where the kernel provider's POST shape draws the anti-bot page). Parsing reuses the kernel's
    result regexes; no result page is fetched."""
    import urllib.parse
    import urllib.request
    from roster_kernel.providers.ddg_web import _LINK, _SNIP, _clean, _real_url
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={"User-Agent": _DDG_UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        body = r.read().decode("utf-8", "ignore")
    links = _LINK.findall(body)[:max_results]
    snips = [_clean(s) for s in _SNIP.findall(body)][:max_results]
    out = []
    for i, (href, title) in enumerate(links):
        u = _real_url(href)
        if u.startswith("http"):
            out.append({"url": u, "title": _clean(title), "snippet": snips[i] if i < len(snips) else ""})
    return out


def build_query(name: str, hints: dict[str, list[str]]) -> str:
    """'"First Last" <company> site:linkedin.com/in' — middle initials are dropped from the quoted
    phrase ("Tom B Brown" would miss the 'Tom Brown - …' title) while name_matches still accepts them."""
    toks = name.split()
    if len(toks) >= 3:
        toks = [toks[0], toks[-1]]        # first + last: recall first; name_matches accepts middles
    co = (hints.get("company") or [""])[0]
    return f'"{" ".join(toks)}" {co} site:linkedin.com/in'.replace("  ", " ").strip()


async def resolve_linkedin(row: dict, *, search=search_snippets) -> dict:
    """Resolve a people row's LinkedIn profile from snippets. Returns the `choose` decision plus
    the query used and the evidence framing. Never fetches linkedin.com."""
    import os
    name = (row.get("name") or "").strip()
    if not name or len(name.split()) < 2:
        return {"status": "none", "match": None, "candidates": [], "query": ""}
    hints = hints_from_row(row)
    q = build_query(name, hints)
    results = await search(q)
    if not results and not os.environ.get("BRAVE_API_KEY"):
        # On the KEYLESS leg an empty result is far more often a BLOCKED search (DuckDuckGo serves
        # an anti-bot page from datacenter IPs after a few calls) than a true zero — say so; never
        # let 'search unavailable' read as 'no such profile'. With Brave keyed, empty means none.
        return {"status": "unavailable", "match": None, "candidates": [], "query": q,
                "note": "the search leg returned nothing (keyless DuckDuckGo is rate-limited from "
                        "the server; set BRAVE_API_KEY for a reliable leg)"}
    dec = choose(name, hints, results)
    dec["query"] = q
    dec["evidence"] = {"type": "self_stated", "family": "linkedin",
                       "note": "headline quoted from the search engine's snippet of the profile; "
                               "matched on name + grounded hints — a self-authored source, not "
                               "independent corroboration"}
    return dec


async def headline_for_url(row: dict, url: str, *, search=search_snippets) -> tuple[str, dict]:
    """The snippet headline for a profile URL we ALREADY hold (no identity decision to make):
    search `site:<that exact path>`, accept only a result whose URL is that profile and whose
    result-name matches the person. Returns (headline, hint hits) or ('', {})."""
    path = re.sub(r"^https?://", "", (url or "").strip()).rstrip("/")
    if "/in/" not in path:
        return "", {}
    results = await search(f"site:{path}")
    want = path.lower().split("/in/", 1)[1].split("/")[0]
    for r in results or []:
        u = (r.get("url") or "").lower()
        if "/in/" not in u or u.split("/in/", 1)[1].split("/")[0].rstrip("/") != want:
            continue
        rname, headline = parse_title(r.get("title") or "")
        if headline and name_matches(row.get("name") or "", rname):
            return headline, hint_hits(hints_from_row(row), headline + " " + (r.get("snippet") or ""))
    return "", {}


async def persist_resolution(store, entity_id: str, match: dict, *, tenant_id: str = "demo") -> None:
    """Write the resolved profile as SELF-STATED facets (family 'linkedin' via the URL)."""
    url = match["url"]
    await store.add_person_facet(entity_id=entity_id, facet_key="link_linkedin",
                                 facet_value_norm=url.lower()[:200], display_value=url[:300],
                                 source_document_id=url, confidence=0.7, tenant_id=tenant_id)
    if match.get("headline"):
        await store.add_person_facet(entity_id=entity_id, facet_key="linkedin_headline",
                                     facet_value_norm=_norm(match["headline"])[:200],
                                     display_value=match["headline"][:300],
                                     source_document_id=url, confidence=0.7, tenant_id=tenant_id)
    # The headline STATES the company we matched on → a second self-authored statement of the
    # company, under its OWN facet key (the facet table's key is (entity, key, value) without the
    # source, so a second `company` row would overwrite the GitHub row's provenance). evidence.py
    # maps linkedin_company onto the `company` claim axis → CONSISTENCY (both self-stated), never
    # corroboration.
    for co in (match.get("hits") or {}).get("company") or []:
        norm = str(co).strip().lower().replace(" ", "_")
        if norm:
            await store.add_person_facet(entity_id=entity_id, facet_key="linkedin_company",
                                         facet_value_norm=norm[:200], display_value=str(co)[:300],
                                         source_document_id=url, confidence=0.7, tenant_id=tenant_id)


# --------------------------------------------------------------------------- #
# COHORT ENRICHMENT: read the snippets for the people SHOWN (top-20 / next-10)  #
# --------------------------------------------------------------------------- #
_SCAN_DDL = """
CREATE TABLE IF NOT EXISTS rs_linkedin_scan (
    entity_id  text PRIMARY KEY,
    status     text NOT NULL,                 -- resolved | ambiguous | none | already
    url        text NOT NULL DEFAULT '',
    headline   text NOT NULL DEFAULT '',
    scanned_at timestamptz NOT NULL DEFAULT now()
);
"""


def _embed_many(texts: list[str]) -> list[list[float] | None]:
    """One embeddings call for a small batch (headline ↔ brief fit). None per item on failure."""
    import json
    import os
    import urllib.request
    key = os.environ.get("OPENAI_API_KEY")
    clean = [(t or "").strip()[:1000] for t in texts]
    if not key or not any(clean):
        return [None] * len(texts)
    try:
        body = json.dumps({"model": "text-embedding-3-small",
                           "input": [c or "." for c in clean]}).encode()
        req = urllib.request.Request("https://api.openai.com/v1/embeddings", data=body,
                                     headers={"Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)["data"]
        out = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        return [v if c else None for v, c in zip(out, clean)]
    except Exception as e:  # noqa: BLE001
        _log.info("embed_many failed: %s", e)
        return [None] * len(texts)


def _cos(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b:
        return None
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na and nb else None


async def reembed_person(pool, entity_id: str) -> bool:
    """Rebuild the person's search vector from ALL current facets (now including the LinkedIn
    headline) so future semantic searches see what the person says about themselves."""
    from api.people_population import embed_query
    try:
        async with pool.acquire() as conn:
            name = await conn.fetchval("SELECT name FROM rs_entity WHERE entity_id = $1", entity_id)
            frows = await conn.fetch(
                "SELECT facet_key, display_value FROM roster_entity_facet WHERE entity_id = $1", entity_id)
        parts = [name or ""] + [f["display_value"] for f in frows
                                if not str(f["facet_key"]).startswith("link_")]
        vec = embed_query(" ".join(p for p in parts if p))
        if not vec:
            return False
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_person_vec (entity_id, embedding) VALUES ($1, $2::vector)
                   ON CONFLICT (entity_id) DO UPDATE SET embedding = EXCLUDED.embedding""", entity_id, vec)
        return True
    except Exception as e:  # noqa: BLE001
        _log.info("reembed_person(%s) skipped: %s", entity_id, e)
        return False


async def enrich_cohort(store, entity_ids: list[str], brief: str, *, tenant_id: str = "demo",
                        max_people: int = 20, qps: float = 1.0, daily_cap: int | None = None,
                        search=search_snippets, facets: dict | None = None) -> dict:
    """Read LinkedIn snippets for the people SHOWN (never the whole 200): resolve each unscanned
    person (Brave: ~1 query/s), store confident matches as self-stated facets + re-embed them,
    remember every outcome so a person is queried once, then score HEADLINE ↔ BRIEF fit and a
    code-owned rank read per row. Returns {rows:[{entity_id, linkedin:{...}, rank_read, row}],
    quota:{used_today, cap}, skipped}."""
    import asyncio
    import os
    import time
    from api.artifacts import attach_artifacts
    from api.evidence import rank_read
    from api.people_population import _person_row_from_facets
    ids = [e for e in dict.fromkeys(entity_ids or []) if e][:max_people]
    if not ids:
        return {"rows": [], "quota": {}, "skipped": []}
    cap = int(daily_cap if daily_cap is not None else os.environ.get("ROSTER_LINKEDIN_DAILY_CAP", "600") or 600)
    scans = await store.linkedin_scans(ids)
    used_today = await store.linkedin_scans_today()
    raw = {r["entity_id"]: r for r in await store.people_by_ids(ids, tenant_id=tenant_id)}
    out_rows, skipped = [], []
    last_call = 0.0
    for eid in ids:
        r = raw.get(eid)
        if not r:
            skipped.append(eid); continue
        row = _person_row_from_facets(r)
        li = {"status": "", "url": "", "headline": "", "hits": {}}
        has_link = next((l["url"] for l in row.get("links") or [] if (l.get("kind") or "") == "linkedin"), "")
        headline_facet = next((a["display"] for a in row.get("attributes") or []
                               if a.get("key") == "linkedin_headline"), "")
        prior = scans.get(eid)
        if prior:
            li.update({"status": prior["status"], "url": prior["url"] or has_link,
                       "headline": prior["headline"] or headline_facet})
        elif has_link:
            # a LinkedIn link already on file (from the person's own GitHub/OpenAlex profile) but no
            # headline yet → one search pinned to that exact profile URL to read its snippet headline
            headline = headline_facet
            if not headline and used_today < cap:
                wait = (1.0 / qps) - (time.time() - last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                last_call = time.time()
                used_today += 1
                headline, hits = await headline_for_url(row, has_link, search=search)
                if headline:
                    await persist_resolution(store, eid, {"url": has_link, "headline": headline, "hits": hits},
                                             tenant_id=tenant_id)
                    fresh = await store.people_by_ids([eid], tenant_id=tenant_id)
                    if fresh:
                        row = _person_row_from_facets(fresh[0])
                    li["hits"] = hits
            li.update({"status": "already", "url": has_link, "headline": headline})
            await store.record_linkedin_scan(eid, "already", url=has_link, headline=headline)
        elif used_today >= cap:
            li["status"] = "quota"
        else:
            wait = (1.0 / qps) - (time.time() - last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            last_call = time.time()
            dec = await resolve_linkedin(row, search=search)
            used_today += 1
            st = dec.get("status") or "none"
            if st == "resolved" and dec.get("match"):
                m = dec["match"]
                await persist_resolution(store, eid, m, tenant_id=tenant_id)
                await store.record_linkedin_scan(eid, "resolved", url=m["url"], headline=m.get("headline") or "")
                li.update({"status": "resolved", "url": m["url"], "headline": m.get("headline") or "",
                           "hits": m.get("hits") or {}})
                fresh = await store.people_by_ids([eid], tenant_id=tenant_id)
                if fresh:
                    row = _person_row_from_facets(fresh[0])
                get_pool = getattr(store, "_get_pool", None)
                if get_pool is not None:
                    try:
                        await reembed_person(await get_pool(), eid)
                    except Exception:  # noqa: BLE001
                        pass
            elif st in ("ambiguous", "none"):
                await store.record_linkedin_scan(eid, st)
                li["status"] = st
                li["candidates"] = [{"name": c["name"], "headline": c["headline"], "url": c["url"]}
                                    for c in (dec.get("candidates") or [])[:3]]
            else:
                li["status"] = st          # unavailable: not recorded → retried next time
        row["linkedin"] = li
        row["_facets"] = r.get("facets") or []
        out_rows.append(row)
    # PUBLIC WORK for the people shown: scan anyone not yet scanned (OpenAlex works / GitHub repos +
    # orgs by identity key — 1–2 metadata calls each, bounded) so 'What they've done' is filled for
    # the visible cohort rather than 'not checked yet'. Then the EXTRA layers: posts from the
    # person's declared site/newsletter feeds (strong key) and gated talks (one search each, counted
    # against the daily cap; ROSTER_TALKS_ENRICH=0 turns talks off).
    get_pool = getattr(store, "_get_pool", None)
    if get_pool is not None:
        try:
            from api.artifacts import scan_person_extras, scan_person_now
            pool = await get_pool()
            talks_on = os.environ.get("ROSTER_TALKS_ENRICH", "1") == "1"
            from api.artifacts import scan_linked_identities
            for r in out_rows:
                if not (r.get("artifacts") or {}).get("scanned"):
                    await scan_person_now(pool, r["entity_id"], timeout=8.0)
                await scan_linked_identities(pool, r["entity_id"], timeout=8.0)
                want_talks = talks_on and used_today < cap
                got = await scan_person_extras(pool, r["entity_id"], r.get("_facets") or [], r,
                                               talks=want_talks, search=search, timeout=15.0)
                if want_talks and got.get("talks") is not None:
                    used_today += 1
        except Exception as e:  # noqa: BLE001 — best-effort
            _log.info("cohort artifact scan skipped: %s", e)
    for r in out_rows:
        r.pop("_facets", None)
    await attach_artifacts(store, out_rows)
    # HEADLINE ↔ BRIEF fit: what the person says they do vs what the brief asks for (one batch call)
    heads = [(r["linkedin"].get("headline") or "") for r in out_rows]
    if (brief or "").strip() and any(heads):
        vecs = _embed_many([brief] + heads)
        bvec = vecs[0]
        for r, hv in zip(out_rows, vecs[1:]):
            fit = _cos(bvec, hv)
            if fit is not None:
                r["linkedin"]["headline_fit"] = round(max(0.0, min(1.0, fit)), 3)
    for r in out_rows:
        r["rank_read"] = rank_read(r, facets)
    return {"rows": out_rows, "quota": {"used_today": used_today, "cap": cap}, "skipped": skipped}
