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

from pydantic import BaseModel

_log = logging.getLogger(__name__)

# Sources the index does NOT yet cover — stated in every answer so "find all" never implies the whole
# world. (A curation frontier, not a bug: coverage grows per ingested source.)
NOT_INGESTED = ["LinkedIn", "X/Twitter", "most company org charts"]


class _FacetParse(BaseModel):
    """Fixed schema for the LLM facet compiler (dynamic dicts don't schema cleanly). Empty lists =
    the question did not constrain that facet; ALL empty = not a people-enumeration query."""
    title: list[str] = []
    seniority: list[str] = []
    function: list[str] = []
    metro: list[str] = []
    company: list[str] = []


async def parse_people_facets(question: str, llm) -> dict[str, list[str]]:
    """LLM query-compiler: free-text people question → normalized facet filter. Returns {} when the
    question is not a people-enumeration query (or on any LLM/parse failure — fail safe, never guess)."""
    from roster_vertical.people_facets import PEOPLE_FACET_KEYS, facet_parse_prompt
    try:
        comp = await llm.complete(
            system="You compile a people-search question into a normalized facet filter. "
                   "Return only the structured facets; empty if it is not a people-discovery query.",
            messages=[{"role": "user", "content": facet_parse_prompt(question)}],
            response_format=_FacetParse, max_tokens=400)
        p = comp.parsed
    except Exception as e:  # noqa: BLE001 — a parse/provider failure must not crash the route
        _log.warning("parse_people_facets failed: %s", e)
        return {}
    out: dict[str, list[str]] = {}
    for k in PEOPLE_FACET_KEYS:
        vals = [str(v).strip().lower().replace(" ", "_")
                for v in (getattr(p, k, None) or []) if str(v).strip()]
        if vals:
            out[k] = vals
    return out


def _facet_summary(facets: dict[str, list[str]]) -> str:
    parts = [f"{k}∈{{{', '.join(v)}}}" if len(v) > 1 else f"{k}={v[0]}"
             for k, v in facets.items()]
    return "; ".join(parts)


def _coverage_basis(facets, stats, matches: int) -> dict:
    return {
        "query_facets": facets,
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


async def answer_people_population(*, question: str, tenant_id: str, store, llm) -> dict:
    """Answer a people-enumeration question from the grounded people index. Always returns a structured
    result (never raises to the route): a compiled facet filter, grounded rows, and honest coverage."""
    facets = await parse_people_facets(question, llm)
    stats = await store.people_index_stats(tenant_id=tenant_id)
    if not facets:
        # Not a people-enumeration query — signal the router to fall through to normal research.
        return {"grounded": False, "not_people_query": True, "people_rows": [],
                "coverage_basis": None, "answer": ""}

    rows = await store.enumerate_by_facets(facets, tenant_id=tenant_id, cap=200)
    coverage = _coverage_basis(facets, stats, len(rows))

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
        people_rows.append({
            "entity_id": r["entity_id"], "name": r["name"],
            "attributes": attrs, "links": links, "citation": cite})

    summary = _facet_summary(facets)
    if people_rows:
        lines = [f"Found {len(people_rows)} people matching [{summary}] in Roster's grounded people index.",
                 "", coverage["population_statement"], ""]
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
