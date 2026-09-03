"""OPEN-WEB PERSON DISCOVERY — the Talent Map P0: a NAME we do not hold yet is found, disambiguated
with the user, brought into the index, and enriched with every evidence layer — never handed off.

Sources (each keyed, none name-only at the point of use):
  GitHub   /search/users?q="<name>" in:name → hydrated profiles (company, location, bio, links)
  OpenAlex /authors?search=<name>          → authors with last-known institutions, works, ORCID
  LinkedIn search snippets                 → headline profiles (name-matched, via Brave)
The candidates are shown as cards with a grounded distinguisher (employer · place · source). The
user's context (company, school, place) or a pick chooses ONE; only then is that identity minted
into rs_entity (+facets +vector) through the same ingest paths as the bulk sweeps, and the normal
on-demand enrichment (papers/repos/posts/talks/LinkedIn) runs. Same-named people are never merged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import urllib.parse
import urllib.request

_log = logging.getLogger("roster.discovery")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

_UA = {"User-Agent": "roster-discovery/1.0", "Accept": "application/json"}


def _get_json(url: str, headers: dict | None = None, timeout: int = 15):
    req = urllib.request.Request(url, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --------------------------------------------------------------------------- #
# Source fetchers (sync; run in threads)                                        #
# --------------------------------------------------------------------------- #
def github_candidates(name: str, *, token: str, limit: int = 6) -> list[dict]:
    if not token:
        return []
    q = urllib.parse.quote(f'"{name}" in:name')
    d = _get_json(f"https://api.github.com/search/users?q={q}&per_page={limit}",
                  headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    out = []
    for u in (d.get("items") or [])[:limit]:
        try:
            p = _get_json(f"https://api.github.com/users/{u['login']}",
                          headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
        except Exception:  # noqa: BLE001
            continue
        if p.get("type") != "User":
            continue
        out.append(p)
    return out


def openalex_candidates(name: str, *, limit: int = 8) -> list[dict]:
    q = urllib.parse.quote(name)
    d = _get_json(f"https://api.openalex.org/authors?search={q}&per-page={limit}")
    return list(d.get("results") or [])[:limit]


def openalex_author(author_id: str) -> dict | None:
    try:
        return _get_json(f"https://api.openalex.org/authors/{author_id}")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# Candidate rows (card shape) — grounded, typed, with a distinguisher            #
# --------------------------------------------------------------------------- #
def _norm(v: str) -> str:
    return re.sub(r"\s+", "_", (v or "").strip().lower())


def _row(eid: str, name: str, facets: list[tuple[str, str]], doc: str, links: list[dict], blurb: str,
         source_label: str) -> dict:
    from api.evidence import evidence_packet
    frows = [{"facet_key": k, "value_norm": _norm(v), "display_value": v, "document_id": doc, "block_id": ""}
             for k, v in facets if v]
    attrs = [{"key": f["facet_key"], "display": f["display_value"], "document_id": doc, "block_id": ""} for f in frows]
    return {"entity_id": eid, "name": name, "blurb": blurb, "attributes": attrs, "links": links,
            "citation": {"document_id": doc, "block_id": ""}, "evidence": evidence_packet(frows, eid),
            "web": True, "source_label": source_label, "_facets": frows}


def rows_from_github(profiles: list[dict]) -> list[dict]:
    out = []
    for p in profiles:
        nm = (p.get("name") or "").strip()
        if len(nm.split()) < 2:
            continue
        facets = [("company", (p.get("company") or "").lstrip("@").strip()), ("metro", p.get("location") or ""),
                  ("title", (p.get("bio") or "")[:120])]
        links = [{"kind": "github", "url": p.get("html_url") or ""}]
        if p.get("blog"):
            links.append({"kind": "website", "url": p["blog"]})
        if p.get("twitter_username"):
            links.append({"kind": "x", "url": "https://x.com/" + p["twitter_username"]})
        blurb = " — ".join(x for x in [(p.get("bio") or "").strip(), (p.get("company") or "").strip(),
                                        (p.get("location") or "").strip()] if x)
        out.append(_row("github:" + p["login"], nm, facets, p.get("html_url") or "", links, blurb, "GitHub"))
    return out


def rows_from_openalex(authors: list[dict]) -> list[dict]:
    out = []
    for a in authors:
        aid = (a.get("id") or "").rsplit("/", 1)[-1]
        nm = (a.get("display_name") or "").strip()
        if not aid or len(nm.split()) < 2:
            continue
        insts = [i.get("display_name") or "" for i in (a.get("last_known_institutions") or [])]
        country = next((i.get("country_code") or "" for i in (a.get("last_known_institutions") or []) if i.get("country_code")), "")
        topics = [t.get("display_name") or "" for t in (a.get("topics") or [])[:3]]
        facets = [("company", insts[0] if insts else ""), ("role", "Researcher"), ("country", country.lower()),
                  ("title", ", ".join(t for t in topics if t)[:120]), ("link_orcid", a.get("orcid") or "")]
        links = ([{"kind": "orcid", "url": a["orcid"]}] if a.get("orcid") else [])
        blurb = " — ".join(x for x in [f"Researcher at {insts[0]}" if insts else "Researcher",
                                        f"{a.get('works_count', 0)} works, {a.get('cited_by_count', 0)} citations",
                                        ", ".join(t for t in topics if t)] if x)
        out.append(_row("openalex:" + aid, nm, facets, f"https://openalex.org/{aid}", links, blurb, "OpenAlex"))
    return out


def rows_from_linkedin(name: str, results: list[dict]) -> list[dict]:
    from api.linkedin_resolve import name_matches, parse_title
    out, seen = [], set()
    for r in results or []:
        url = (r.get("url") or "").strip()
        m = re.match(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/([^/?#\s]+)", url, re.I)
        if not m:
            continue
        rname, headline = parse_title(r.get("title") or "")
        if not name_matches(name, rname):
            continue
        slug = m.group(2).lower()
        if slug in seen:
            continue
        seen.add(slug)
        co = ""
        mm = re.search(r"\bat\s+([^|·\-–]+)$", headline or "")
        if mm:
            co = mm.group(1).strip()
        facets = [("title", headline), ("company", co), ("role", re.sub(r"\s+at\s+.*$", "", headline) if co else "")]
        out.append(_row("linkedin:" + slug, rname, facets, url, [{"kind": "linkedin", "url": url}],
                        headline, "LinkedIn"))
    return out


async def discover_candidates(name: str, ctx: str = "", *, gh=None, oa=None, li=None) -> list[dict]:
    """All open-web candidates for a name (GitHub · OpenAlex · LinkedIn), as card rows. Fetchers are
    injectable for tests. Failures per source are silent — a missing source is not a missing person."""
    from api.linkedin_resolve import search_snippets
    token = os.environ.get("ROSTER_GITHUB_TOKEN", "")
    gh = gh or (lambda n: github_candidates(n, token=token))
    oa = oa or openalex_candidates
    li = li or search_snippets
    q = f'"{name}" {ctx} site:linkedin.com/in'.replace("  ", " ").strip()
    gh_t = asyncio.create_task(asyncio.to_thread(gh, name))
    oa_t = asyncio.create_task(asyncio.to_thread(oa, name))
    li_t = asyncio.create_task(li(q))
    rows: list[dict] = []
    for t, build in ((gh_t, rows_from_github), (oa_t, rows_from_openalex), (li_t, lambda r: rows_from_linkedin(name, r))):
        try:
            res = await asyncio.wait_for(t, 20)
            rows += build(res or [])
        except Exception as e:  # noqa: BLE001
            _log.info("discovery source failed: %s", e)
    return rows


# --------------------------------------------------------------------------- #
# Bring the chosen identity into the index (same paths as the bulk ingest)      #
# --------------------------------------------------------------------------- #
async def ingest_identity(store, entity_id: str, *, name_hint: str = "") -> bool:
    """Mint a chosen open-web identity into rs_entity (+ facets + vector). GitHub uses the committed
    people ingester (LLM facets + embedding); OpenAlex and LinkedIn use code-derived facets."""
    src, _, key = (entity_id or "").partition(":")
    pool = await store._get_pool()
    try:
        if src == "github":
            import ingest_people as ip
            token = os.environ.get("ROSTER_GITHUB_TOKEN", "")
            prof = await asyncio.to_thread(ip.hydrate, key, token=token)
            if not prof:
                return False
            fac = (await asyncio.to_thread(ip.extract_facets, [prof]))[0]
            text = " ".join(str(prof.get(k) or "") for k in ("name", "bio", "company", "location"))
            vec = await asyncio.to_thread(ip.embed_one, text)
            async with pool.acquire() as conn:
                await ip.upsert_person(conn, prof, fac, vec)
            return True
        if src == "openalex":
            a = await asyncio.to_thread(openalex_author, key)
            if not a:
                return False
            row = rows_from_openalex([a])
            if not row:
                return False
            return await _mint(pool, entity_id, row[0])
        if src == "linkedin":
            from api.linkedin_resolve import headline_for_url
            url = f"https://www.linkedin.com/in/{key}"
            headline, _hits = await headline_for_url({"name": name_hint}, url)
            rows = rows_from_linkedin(name_hint, [{"url": url, "title": f"{name_hint} - {headline}" if headline else name_hint,
                                                    "snippet": ""}])
            if not rows:
                return False
            return await _mint(pool, entity_id, rows[0])
    except Exception as e:  # noqa: BLE001
        _log.warning("ingest_identity(%s) failed: %s", entity_id, e)
    return False


async def _mint(pool, entity_id: str, row: dict) -> bool:
    from api.people_population import embed_query
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """INSERT INTO rs_entity (entity_id, tenant_id, kind, name, facets, retrieved_at)
                   VALUES ($1,'demo','person',$2,'{}'::jsonb, now())
                   ON CONFLICT (entity_id) DO UPDATE SET name = EXCLUDED.name, retrieved_at = now()""",
                entity_id, row["name"])
            for f in row.get("_facets") or []:
                await conn.execute(
                    """INSERT INTO roster_entity_facet (tenant_id, entity_id, facet_key, facet_value_norm,
                         display_value, source_document_id, confidence)
                       VALUES ('demo',$1,$2,$3,$4,$5,0.6)
                       ON CONFLICT (tenant_id, entity_id, facet_key, facet_value_norm) DO UPDATE SET
                         display_value = EXCLUDED.display_value""",
                    entity_id, f["facet_key"], f["value_norm"][:200], f["display_value"][:300], f["document_id"])
            for l in row.get("links") or []:
                if l.get("url"):
                    await conn.execute(
                        """INSERT INTO roster_entity_facet (tenant_id, entity_id, facet_key, facet_value_norm,
                             display_value, source_document_id, confidence)
                           VALUES ('demo',$1,$2,$3,$4,$5,0.6) ON CONFLICT DO NOTHING""",
                        entity_id, "link_" + l["kind"], l["url"].lower()[:200], l["url"][:300], row["citation"]["document_id"])
    vec = embed_query(f"{row['name']} {row.get('blurb') or ''}")
    if vec:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO rs_person_vec (entity_id, embedding) VALUES ($1, $2::vector)
                   ON CONFLICT (entity_id) DO UPDATE SET embedding = EXCLUDED.embedding""", entity_id, vec)
    return True


def clarify_web(name: str, rows: list[dict]) -> str:
    srcs = sorted({r.get("source_label") or "" for r in rows if r.get("source_label")})
    return (f"Which {name}? Not in Roster's index yet — {len(rows)} possible match{'es' if len(rows) != 1 else ''} "
            f"found on {', '.join(srcs)}. Pick one to bring them in, or add a company, school or location.")


# --------------------------------------------------------------------------- #
# OPEN-WEB FOOTPRINT with ALL the hints — when no keyed source has the person   #
# --------------------------------------------------------------------------- #
_HINT_STOP = {"the", "one", "who", "works", "worked", "work", "from", "with", "and", "for", "that", "this",
              "guy", "person", "at", "in", "of", "is", "was", "a", "an", "his", "her", "their", "based",
              "located", "company", "school", "studied", "went", "he", "she", "they", "also", "now",
              "currently", "previously", "formerly", "engineer", "researcher", "manager"}
_URL_RE = re.compile(r"https?://[^\s]+|(?:www\.)?[a-z0-9-]+\.(?:com|io|dev|net|org|ai|edu)\b[^\s]*", re.I)


def hint_tokens(ctx: str) -> list[str]:
    toks = re.findall(r"[a-z0-9][a-z0-9.+\-]{1,}", (ctx or "").lower())
    out = []
    for t in toks:
        if t in _HINT_STOP or len(t) < 3 or t in out:
            continue
        out.append(t)
    return out[:8]


def _name_in(text: str, name: str) -> bool:
    toks = name.split()
    if len(toks) < 2:
        return False
    return re.search(r"\b" + re.escape(toks[0]) + r"\b[\w.\-' ]{0,30}?\b" + re.escape(toks[-1]) + r"\b", text, re.I) is not None


def footprint_from_results(name: str, ctx: str, results: list[dict]) -> dict | None:
    """Pages that mention the FULL NAME together with the hints → one web-footprint candidate:
    the pages are its evidence (linked), the hints the pages confirm become its facets. None when
    no page carries the name with at least one hint."""
    toks = hint_tokens(ctx)
    if not toks:
        return None
    pages = []
    for r in results or []:
        text = (r.get("title") or "") + " " + (r.get("snippet") or "")
        if not _name_in(text, name):
            continue
        low = text.lower()
        hits = [t for t in toks if t in low]
        if not hits:
            continue
        pages.append({"url": r.get("url") or "", "title": (r.get("title") or "")[:160],
                      "snippet": (r.get("snippet") or "")[:240], "hits": hits})
    if not pages:
        return None
    pages.sort(key=lambda p: -len(p["hits"]))
    confirmed = []
    for p in pages:
        for h in p["hits"]:
            if h not in confirmed:
                confirmed.append(h)
    from urllib.parse import urlparse
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") + "-" + re.sub(r"[^a-z0-9]+", "", "".join(confirmed[:2]))[:24]
    eid = "web:" + slug
    doc = pages[0]["url"]
    # facets: only what a page CONFIRMS (a hint present next to the name), never the bare hint
    facets = [("title", pages[0]["snippet"] or pages[0]["title"]), ("web_confirmed", ", ".join(confirmed))]
    links = []
    for p in pages[:5]:
        host = urlparse(p["url"]).netloc.lower().removeprefix("www.")
        if p["url"] and not any(l["url"] == p["url"] for l in links):
            links.append({"kind": host[:30], "url": p["url"]})
    row = _row(eid, name, facets, doc, links, pages[0]["snippet"] or pages[0]["title"], "the open web")
    row["web_pages"] = pages[:5]
    row["web_hits"] = confirmed
    row["blurb"] = f"Public pages mention {name} with {', '.join(confirmed)}: “{pages[0]['title']}”"
    return row


async def web_footprint(name: str, ctx: str, *, search=None) -> dict | None:
    """One general web search with ALL the hints — '"<name>" cisco bangalore' — no site filter."""
    from api.linkedin_resolve import search_snippets
    search = search or search_snippets
    toks = hint_tokens(ctx)
    if not toks:
        return None
    q = f'"{name}" ' + " ".join(toks[:6])
    try:
        res = await asyncio.wait_for(search(q), 20)
    except Exception as e:  # noqa: BLE001
        _log.info("web footprint search failed: %s", e)
        return None
    return footprint_from_results(name, ctx, res or [])


def url_hint(ctx: str) -> str:
    m = _URL_RE.search(ctx or "")
    return m.group(0) if m else ""


def identity_from_url(url: str) -> str:
    """A pasted profile URL is the strongest hint of all: github → github:login, linkedin →
    linkedin:slug; anything else → '' (handled as a web page)."""
    u = (url or "").strip()
    m = re.search(r"github\.com/([A-Za-z0-9-]+)/?$", u)
    if m:
        return "github:" + m.group(1)
    m = re.search(r"linkedin\.com/in/([^/?#\s]+)", u, re.I)
    if m:
        return "linkedin:" + m.group(1).lower()
    return ""
