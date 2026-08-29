"""Company hyperlinks — link the company's OWN site whenever possible.

The LLM identifies which spans in the answer are COMPANIES (Rule 18 — the model owns the
semantic 'is this a company' decision; a regex/registry-substring matcher would mis-fire on
common words) AND gives each company's OFFICIAL HOMEPAGE where it is confident (LLMs reliably
know openai.com, anthropic.com, …). Each company's NAME then links by this precedence:

  1. the company's official homepage (LLM-supplied, format-validated) — the site itself;
  2. else, if it's a KNOWN entity in our claim graph, its grounded canonical page;
  3. else, a scoped web search (last resort — flagged so the FE can show it as a weaker link).

Known companies additionally carry `roster_url` → the in-product grounded /entity page (the ◉).

Returns [{name, url, grounded: bool, search: bool, roster_url?}] where `name` is the EXACT surface
form as it appears in the answer (so the FE first-occurrence matches it). Best-effort: any
failure → []. The homepage is a NAVIGATION link (best-effort), never presented as grounded fact.
"""
from __future__ import annotations

import urllib.parse

from pydantic import BaseModel

_DETECT_SYSTEM = (
    "From the text below, extract the COMPANIES / startups / labs / funds discussed as SUBJECTS. "
    "For each, return its `name` EXACTLY as written in the text (same surface form + casing), and "
    "its official homepage `website` — the company's OWN domain (e.g. Runway → https://runwayml.com, "
    "OpenAI → https://openai.com, Anthropic → https://anthropic.com). You reliably know the real "
    "domains of well-known companies, so GIVE the homepage whenever you can identify the actual "
    "company — this is a navigation link to their site, not a factual claim. Leave `website` empty "
    "ONLY when you genuinely do not recognize the company or its real domain is truly ambiguous (do "
    "not fabricate a domain for a company you don't know). NEVER return a search / Wikipedia / "
    "Crunchbase / LinkedIn / social / GitHub URL as the website — only the company's own site. "
    "EXCLUDE products/models (GPT-4, Claude), technologies, methods, benchmarks, people, government "
    "bodies, and generic terms. Deduplicate. If none, return an empty list.")

# hosts that are NOT a company's own homepage (a search/reference/social page is not 'the site')
_NON_HOMEPAGE_HOSTS = (
    "google.", "bing.", "duckduckgo.", "search.", "yahoo.", "baidu.",
    "wikipedia.", "wikidata.", "crunchbase.", "pitchbook.", "linkedin.", "twitter.",
    "x.com", "facebook.", "instagram.", "youtube.", "medium.com", "substack.com",
    "github.com", "ycombinator.com", "sec.gov",
)


class _Company(BaseModel):
    name: str = ""
    website: str = ""


class _Companies(BaseModel):
    companies: list[_Company] = []


def _web_search_url(name: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote(name)


def _valid_homepage(u: str) -> str:
    """Return a normalized homepage URL, or '' if it isn't a plausible company site. Format-only
    validation (Rule 18: structural) — a scheme+real host, and not a search/reference/social host."""
    u = (u or "").strip()
    if not u:
        return ""
    if not (u.startswith("http://") or u.startswith("https://")):
        # a bare domain the model returned without a scheme → assume https, if it looks like a host
        if " " not in u and "." in u.split("/", 1)[0]:
            u = "https://" + u
        else:
            return ""
    try:
        host = u.split("//", 1)[1].split("/", 1)[0].split("?", 1)[0].lower()
    except Exception:  # noqa: BLE001
        return ""
    if "." not in host or " " in host:
        return ""
    if any(bad in host for bad in _NON_HOMEPAGE_HOSTS):
        return ""
    return u


async def detect_and_resolve_companies(service, store, *, answer: str, ui=None,
                                       tenant_id: str = "demo", max_companies: int = 20) -> list[dict]:
    """LLM-detect companies (+ their homepages) in `answer`, resolve each to a link. Never raises."""
    text = (answer or "").strip()
    if not text or getattr(service, "llm", None) is None:
        return []
    try:
        comp = await service.llm.complete(
            system=_DETECT_SYSTEM,
            messages=[{"role": "user", "content": text[:8000]}],
            response_format=_Companies, max_tokens=700)
        detected = list(getattr(comp.parsed, "companies", []) or [])
    except Exception:  # noqa: BLE001 — best-effort; detection failure must not break the answer
        return []
    # keep only names present VERBATIM in the answer (the FE matches on surface form), deduped
    seen: set = set()
    picked: list[tuple[str, str]] = []       # (surface name, validated homepage)
    for c in detected:
        nm = (getattr(c, "name", "") or "").strip()
        if not nm or nm.lower() in seen or nm not in text:
            continue
        seen.add(nm.lower())
        picked.append((nm, _valid_homepage(getattr(c, "website", "") or "")))
        if len(picked) >= max_companies:
            break
    if not picked:
        return []

    # resolve names against the graph registry (one query), exact normalized-name match
    registry: dict[str, dict] = {}
    normalize_name = None
    if store is not None:
        try:
            from api.claimgraph import normalize_name as _nn
            normalize_name = _nn
            registry = await store.company_norm_map(tenant_id=tenant_id)
        except Exception:  # noqa: BLE001
            registry = {}
    if normalize_name is None:
        def normalize_name(s):  # type: ignore
            return (s or "").strip().lower()

    out: list[dict] = []
    for nm, homepage in picked:
        known = registry.get(normalize_name(nm)) if registry else None
        eid = known["entity_id"] if known else None
        roster_url = ("/entity/" + urllib.parse.quote(eid, safe="")) if eid else None
        grounded_page = None
        if eid:
            try:
                grounded_page = ui.source_url(eid) if ui else None
            except Exception:  # noqa: BLE001
                grounded_page = None
            grounded_page = grounded_page or roster_url
        # precedence: the company's own homepage → grounded canonical page → web search
        if homepage:
            url, is_search = homepage, False
        elif grounded_page:
            url, is_search = grounded_page, False
        else:
            url, is_search = _web_search_url(nm), True
        item = {"name": nm, "url": url, "grounded": bool(known), "search": is_search}
        if roster_url:
            item["roster_url"] = roster_url
        out.append(item)
    return out
