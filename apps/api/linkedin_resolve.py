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
    return parts[0], " - ".join(parts[1:])


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
    import os
    try:
        if os.environ.get("BRAVE_API_KEY"):
            from roster_kernel.providers.brave_web import BraveWebSearch
            client = BraveWebSearch()
        else:
            from roster_kernel.providers.ddg_web import DuckDuckGoWebSearch
            client = DuckDuckGoWebSearch(fetch_bodies=False, timeout=10.0)
        res = await client.search(query, max_results=max_results, open_web=True)
        return [{"url": r.url, "title": r.title, "snippet": r.snippet or ""} for r in res]
    except Exception as e:  # noqa: BLE001 — the leg is best-effort
        _log.info("linkedin snippet search failed: %s", e)
        return []


def build_query(name: str, hints: dict[str, list[str]]) -> str:
    co = (hints.get("company") or [""])[0]
    return f'"{name}" {co} site:linkedin.com/in'.replace("  ", " ").strip()


async def resolve_linkedin(row: dict, *, search=search_snippets) -> dict:
    """Resolve a people row's LinkedIn profile from snippets. Returns the `choose` decision plus
    the query used and the evidence framing. Never fetches linkedin.com."""
    name = (row.get("name") or "").strip()
    if not name or len(name.split()) < 2:
        return {"status": "none", "match": None, "candidates": [], "query": ""}
    hints = hints_from_row(row)
    q = build_query(name, hints)
    results = await search(q)
    dec = choose(name, hints, results)
    dec["query"] = q
    dec["evidence"] = {"type": "self_stated", "family": "linkedin",
                       "note": "headline quoted from the search engine's snippet of the profile; "
                               "matched on name + grounded hints — a self-authored source, not "
                               "independent corroboration"}
    return dec


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
