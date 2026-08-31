"""People-population ANSWER engine — the fix for people-DISCOVERY/enumeration questions.

"Find all people where role∈{Director,EM} ∧ function=ML ∧ location=Bay Area" cannot be answered by a
web-RAG retrieval sample (there is no page listing them). It is answered by FILTERING a grounded
people index:

    parse facets (LLM = query COMPILER, not data processor)  [LLM owns the semantic normalization]
      → enumerate_by_facets (SQL AND across keys / OR within a key)  [code owns the filter]
      → grounded people_rows (each attribute cites the claim/evidence it came from)  [grounding intrinsic]
      → HONEST coverage_basis (index scope, matches, sources ingested vs missing — never "all people")
      → an empty match is a SUCCESSFUL honest result (a data gap), NEVER a silent web-RAG fallback.

Flag-gated at the app boundary (ROSTER_PEOPLE_POPULATION). Rule 18: the LLM owns intent/normalization;
code owns the filter, the citation, and the coverage facts. Public-data only.
"""
from __future__ import annotations

import logging
import urllib.parse

from pydantic import BaseModel

_log = logging.getLogger(__name__)

# Sources the index does NOT yet cover — stated in every answer so "find all" never implies the whole
# world. (A curation frontier, not a bug: coverage grows per ingested source.)
NOT_INGESTED = ["LinkedIn", "X/Twitter", "most company org charts"]


class _FacetParse(BaseModel):
    """Fixed schema for the LLM facet compiler (dynamic dicts don't schema cleanly). Empty lists =
    the question did not constrain that facet; ALL empty (and no `person`) = not a people query.
    `person` is set ONLY when the question is about ONE specific named individual (identity/profile),
    with `person_context` carrying any employer/role hints that disambiguate them."""
    role: list[str] = []
    seniority: list[str] = []
    function: list[str] = []
    industry: list[str] = []
    metro: list[str] = []
    company: list[str] = []
    worked_at: list[str] = []
    country: list[str] = []
    state: list[str] = []
    stage: list[str] = []
    accelerator: list[str] = []
    skill: list[str] = []
    person: str = ""
    person_context: str = ""


async def parse_people_facets(question: str, llm) -> tuple[dict[str, list[str]], str, str]:
    """LLM query-compiler: free-text people question → (facet filter, person, person_context).
    `facets` is a normalized enumeration filter (empty when the question is not enumeration); `person`
    is a single named individual (identity/profile question) with `person_context` disambiguating
    hints. All empty = not a people query. Fail safe on any LLM/parse failure (never guess)."""
    from roster_vertical.people_facets import PEOPLE_FACET_KEYS, facet_parse_prompt
    try:
        comp = await llm.complete(
            system="You compile a people-search question into normalized facets, OR identify a single "
                   "named person. Return only the structured object; empty if it is not about people.",
            messages=[{"role": "user", "content": facet_parse_prompt(question)}],
            response_format=_FacetParse, max_tokens=400)
        p = comp.parsed
    except Exception as e:  # noqa: BLE001 — a parse/provider failure must not crash the route
        _log.warning("parse_people_facets failed: %s", e)
        return {}, "", ""
    out: dict[str, list[str]] = {}
    for k in PEOPLE_FACET_KEYS:
        vals = [str(v).strip().lower().replace(" ", "_")
                for v in (getattr(p, k, None) or []) if str(v).strip()]
        if vals:
            out[k] = vals
    return out, (getattr(p, "person", "") or "").strip(), (getattr(p, "person_context", "") or "").strip()


def build_person_profile_card(name: str, context: str = "") -> dict:
    """A single-person profile card built from EXPLICIT profile searches — GitHub (direct user search),
    X (direct search), and LinkedIn (Google search over name + hints, since LinkedIn has no open
    search). These are navigation aids (searches, clearly labeled), so a person question always
    surfaces their GitHub / X / LinkedIn even when we hold no stored profile for them yet."""
    terms = " ".join(t for t in [name, context] if t)
    g = urllib.parse.quote
    links = [
        {"kind": "github_search", "url": "https://github.com/search?q=" + g(terms) + "&type=users"},
        {"kind": "x_search", "url": "https://x.com/search?q=" + g(terms) + "&f=user"},
        {"kind": "linkedin_search",
         "url": "https://www.google.com/search?q=" + g(terms + " site:linkedin.com/in")},
    ]
    return {"entity_id": "search:" + name, "name": name,
            "attributes": ([{"key": "context", "display": context}] if context else []),
            "links": links, "citation": None}


def _person_blurb(attrs: list[dict]) -> str:
    """A mini-resume for the person, synthesized from their GROUNDED facets: the stored bio (title) when
    it is substantial, otherwise a one-line summary composed from seniority/role/company/function/skills/
    accelerator. No new facts are invented — every part comes from a stored facet."""
    def g(key):
        return next((a["display"] for a in attrs if a["key"] == key and a.get("display")), "")
    def gall(key):
        seen, out = set(), []
        for a in attrs:
            d = a.get("display")
            if a["key"] == key and d and d.lower() not in seen:
                seen.add(d.lower()); out.append(d)
        return out
    bio = g("title")
    if bio and len(bio) >= 45:
        return bio                              # a real source bio — the best blurb
    sen, role, comp = g("seniority"), g("role"), g("company")
    funcs, skills, accel = gall("function"), gall("skill"), g("accelerator")
    parts = []
    lead = " ".join(dict.fromkeys(x for x in [sen, role] if x))   # dedupe "Founder Founder"
    if lead:
        parts.append(lead + (f" at {comp}" if comp else ""))
    elif comp:
        parts.append(f"at {comp}")
    if funcs:
        parts.append("focus: " + ", ".join(funcs[:3]))
    if skills:
        parts.append("skills: " + ", ".join(skills[:4]))
    if accel:
        parts.append("backed by " + accel)
    blurb = " · ".join(parts)
    return (bio + " · " + blurb).strip(" ·") if bio else blurb   # short bio + structured tail


def _facet_summary(facets: dict[str, list[str]]) -> str:
    parts = [f"{k}∈{{{', '.join(v)}}}" if len(v) > 1 else f"{k}={v[0]}"
             for k, v in facets.items()]
    return "; ".join(parts)


def _coverage_basis(facets, stats, matches: int) -> dict:
    return {
        "query_facets": facets,
        "country_scope": (facets.get("country") or [None])[0],   # resolved geo scope (FE echoes it)
        "matches_returned": matches,
        "persons_indexed": stats.get("persons_indexed", 0),
        "source_documents": stats.get("source_documents", 0),
        "facet_coverage": stats.get("facet_coverage", {}),
        "not_ingested": NOT_INGESTED,
        "population_statement": (
            f"These are the grounded matches CURRENTLY in Roster's people index "
            f"({stats.get('persons_indexed', 0)} people from {stats.get('source_documents', 0)} "
            f"public sources) — NOT an exhaustive list of everyone matching. "
            f"Not yet ingested: {', '.join(NOT_INGESTED)}."),
    }


async def answer_people_population(*, question: str, tenant_id: str, store, llm,
                                   scope_country: str = "") -> dict:
    """Answer a people-enumeration question from the grounded people index. Always returns a structured
    result (never raises to the route): a compiled facet filter, grounded rows, and honest coverage.

    `scope_country` (from the top-right selector, flag-gated) HARD-filters results to that country — a
    `country=<scope>` facet is ANDed in, so people we cannot place there are excluded. A country the
    query itself names (compiler-parsed) OVERRIDES the selector default."""
    facets, person, ctx = await parse_people_facets(question, llm)
    if not facets and person:
        # SINGLE-PERSON identity/profile question — the router runs the web bio and attaches this
        # profile card (explicit GitHub/X/LinkedIn search links). kind='person'.
        return {"kind": "person", "not_people_query": False,
                "person_card": build_person_profile_card(person, ctx)}
    stats = await store.people_index_stats(tenant_id=tenant_id)
    if not facets:
        # Not a people query at all — signal the router to fall through to normal research.
        return {"kind": "none", "grounded": False, "not_people_query": True, "people_rows": [],
                "coverage_basis": None, "answer": ""}

    # GEO SCOPE (flag-gated): inject the selector country UNLESS the query already named one (query wins).
    if scope_country and not facets.get("country"):
        facets["country"] = [scope_country]

    rows = await store.enumerate_by_facets(facets, tenant_id=tenant_id, cap=200)

    # GRACEFUL PROGRESSIVE RELAXATION: an over-specific query ANDs to zero (e.g. "sales GTM leaders in
    # California" → state=ca matches no business person; "engineers content platform at netflix" →
    # function=content matches nobody). When the full filter is empty, relax in TIERS — drop the sparse
    # GEO narrowing FIRST (least semantic), then skill, then function/industry — keeping the meaning
    # (a sales/engineer intent) as long as possible, and never relaxing down to a geo/country-only
    # filter (which would return "everyone"). Returns the closest honest match + a note of what relaxed.
    _TIERS = [("metro", "state"), ("skill",), ("function", "industry")]
    _GEO_ONLY = {"country", "state", "metro"}
    relaxed_from: list[str] = []
    if not rows:
        kept, dropped = dict(facets), []
        for tier in _TIERS:
            drop_now = [k for k in tier if k in kept]
            if not drop_now:
                continue
            for k in drop_now:
                kept.pop(k, None); dropped.append(k)
            if not any(k not in _GEO_ONLY for k in kept):     # nothing meaningful left → stop
                break
            r2 = await store.enumerate_by_facets(kept, tenant_id=tenant_id, cap=200)
            if r2:
                rows, relaxed_from, facets = r2, list(dropped), kept
                break

    coverage = _coverage_basis(facets, stats, len(rows))
    if relaxed_from:
        coverage["relaxed_from"] = relaxed_from

    people_rows = []
    for r in rows:
        # one representative grounded citation per person (first facet carrying a source)
        cite = next(({"document_id": f["document_id"], "block_id": f["block_id"]}
                     for f in r["facets"] if f.get("document_id")), None)
        # `link_*` facets are the person's OTHER profiles (linkedin/x/website/medium/email), enriched
        # from the source profile — surfaced as clickable links on the card, not filter attributes.
        links, attrs = [], []
        for f in r["facets"]:
            if f["facet_key"].startswith("link_"):
                links.append({"kind": f["facet_key"][5:], "url": f["display_value"]})
            else:
                attrs.append({"key": f["facet_key"], "display": f["display_value"],
                              "document_id": f["document_id"], "block_id": f["block_id"]})
        # LinkedIn PROXY: when we have no direct LinkedIn link, synthesize a Google search over the
        # person's name + role + company that reliably lands on their LinkedIn — a navigation aid
        # (clearly a SEARCH, not grounded evidence). Skipped when a real LinkedIn link exists.
        if not any(l["kind"] == "linkedin" for l in links):
            title_disp = (next((a["display"] for a in attrs if a["key"] == "title"), "")
                          or next((a["display"] for a in attrs if a["key"] == "seniority"), ""))
            company_disp = next((a["display"] for a in attrs if a["key"] == "company"), "")
            terms = " ".join(t for t in [r["name"], title_disp, company_disp, "LinkedIn"] if t)
            links.append({"kind": "linkedin_search",
                          "url": "https://www.google.com/search?q=" + urllib.parse.quote(terms)})
        people_rows.append({
            "entity_id": r["entity_id"], "name": r["name"], "blurb": _person_blurb(attrs),
            "attributes": attrs, "links": links, "citation": cite})

    # RANK toward the query (user: ranked, not neutral): most prominent first by seniority/tier, then a
    # completeness boost (a contactable, linked profile ranks above a bare one), then name for stability.
    _RANK = {"c_level": 10, "cto": 10, "distinguished_scientist": 9, "vp": 9, "head": 8, "director": 8,
             "senior_manager": 7, "engineering_manager": 7, "lead": 6, "principal": 6, "staff": 5,
             "senior": 4, "researcher": 3, "physician": 3, "mid": 2, "junior": 1, "student": 0}

    def _score(p):
        sen = next((a["display"] for a in p["attributes"] if a["key"] == "seniority"), "")
        base = _RANK.get(sen.lower().replace(" ", "_"), 3)
        return base + min(len(p["links"]), 3) * 0.1   # small boost for a richer/contactable profile

    people_rows.sort(key=lambda p: (-_score(p), p["name"]))

    summary = _facet_summary(facets)
    if people_rows:
        relax_note = (f" (no exact match, so the {', '.join(relaxed_from)} filter"
                      f"{'s were' if len(relaxed_from) > 1 else ' was'} relaxed)" if relaxed_from else "")
        lines = [f"Found {len(people_rows)} people matching [{summary}]{relax_note} in Roster's "
                 f"grounded people index.", "", coverage["population_statement"], ""]
        for i, p in enumerate(people_rows, 1):
            attrs = ", ".join(a["display"] for a in p["attributes"] if a["display"])
            lines.append(f"{i}. {p['name']} — {attrs}")
        answer = "\n".join(lines)
        grounded = True
    else:
        answer = (f"No people in Roster's index yet match [{summary}]. "
                  + coverage["population_statement"]
                  + " This is a coverage gap, not a dead end — ingest a source that lists these people "
                    "(e.g. company leadership/team pages, conference speakers) to populate it.")
        grounded = False

    return {"grounded": grounded, "not_people_query": False, "answer": answer,
            "people_rows": people_rows, "coverage_basis": coverage}
