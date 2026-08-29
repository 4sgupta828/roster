"""Population-query COMPOSE prompt + renderer (VERTICAL-owned — Task 4 capstone).

Task 4's answer route does NOT go through `run_react`; it aggregates the grounded
claim graph into a compact, per-cell-CITED market map (`build_market_map` over
`ClaimGraphStore.population_claims`) and composes a clustered market-map answer in a
single, self-contained LLM call. This module owns the two vertical-specific pieces:

  * `TECH_POPULATION_COMPOSE_PROMPT` — the self-contained system directive. It is the
    grounding contract in prose: cite EVERY company and EVERY cell with the
    `[citation_id]` from the provided panel; NEVER name a company or state a fact that
    is not in the panel (no uncited proper nouns — grounding is the moat); cluster by
    the category axis; a missing cell is `not_collected`, never invented; END with a
    `## Coverage basis` section built from the caller-supplied coverage facts.
  * `render_market_map_for_compose` — the DETERMINISTIC renderer that turns the
    market-map dict into the compact map + a numbered citation panel the LLM cites
    against. Pure (no DB/LLM), so it is directly unit-testable.

Rule 18 split: the LLM owns the PROSE (which companies to cluster, what to say); code
owns the citation panel it must cite against and the post-hoc citation-resolution gate
(in the route). The prompt cannot GUARANTEE no uncited proper noun — that is what the
live eval measures — but it makes the contract explicit and the panel makes compliance
cheap.
"""
from __future__ import annotations

from typing import Any


# ── The self-contained population-compose system directive ─────────────────────────
# Adapted from `reasoned.TECH_LANDSCAPE_COMPOSE_BLOCK`, but STANDALONE (this route has
# no run_react compose directive to append to) and pinned to the market-map + citation
# panel contract rather than raw retrieved findings.
TECH_POPULATION_COMPOSE_PROMPT = """\
You compose a GROUNDED market-map answer to a landscape / population question. You are
given (1) the user's question, (2) a FLAT LIST of the grounded company population with
each company's cited facts, and (3) a numbered CITATION PANEL. You cluster this
population into clean market segments yourself (see STRUCTURE). Every company and every
fact you may use comes ONLY from that list and panel — this is a grounded product
surface, not a from-memory market overview.

HARD GROUNDING RULES (grounding is the moat — violating these breaks the product):
- CITE EVERY COMPANY AND EVERY CELL. When you name a company, cite at least one of its
  `[citation_id]` from the panel. When you state a fact about a company (its product,
  customer, technology, differentiator, or who it is compared to), cite the exact
  `[citation_id]` that carries that fact. Put the marker inline, e.g. `Acme [c1]`.
- NEVER name a company, product, founder, funding figure, person, or any other proper
  noun that is not in the citation panel. Do NOT add well-known companies from memory to
  look more complete. If it is not in the panel, it does not exist for this answer.
- Use ONLY facts present in the panel. Do not infer or embellish a cell (no guessed
  funding, stage, headcount, or valuation). A company with no fact for a dimension is
  `not_collected` — say so or omit the dimension for that company; never invent it.

STRUCTURE:
- DERIVE THE CLUSTERS YOURSELF. You are given a FLAT list of grounded companies with
  their facts (product / customer / technology / differentiator / compared_to). Read
  across the whole population and group the companies into 6–12 COHERENT market segments
  that YOU name cleanly (e.g. "Developer & code tooling", "Healthcare & clinical AI",
  "Fintech compliance & KYC", "Robotics & physical automation"). Do NOT reuse the raw
  category tags verbatim — many are one-off, over-specific, or noisy; your job is to
  impose a clean, useful clustering on the population. Base each company's placement ONLY
  on its cited facts; a company too thin to place goes in a final "Other / thinly
  documented" section (still cited). Every company in the panel MUST appear in exactly
  one segment.
- Lead with a SHORT synthesis of how the space breaks down (the few structural
  conclusions that matter — which segments are crowded, where the population is thin),
  then the segment-by-segment map.
- Within each segment, compare the companies on the dimensions present (product /
  customer / technology / differentiator). Where a dimension is absent for a company, say
  `not_collected` — never guess.
- SCALE TO THE POPULATION — do NOT write a paragraph for every single company when the
  population is large. For each segment, state its company COUNT, then profile the few
  most representative / most-documented companies (cited), and list the remaining members
  by name with their single most salient cited fact. A scannable market map, not an
  exhaustive roster. Every company still appears somewhere (in a profile or the member
  list), each with at least one `[citation_id]`.
- General-audience prose — clear and scannable, NOT a VC memo. A compact table per
  segment is fine when it reads cleanly; put the `[citation_id]` in the cell.
- END with a `## Coverage basis` section. Build it from the COVERAGE FACTS the user
  message provides: name the categories covered, note anything thin or truncated, and
  state PLAINLY that this map reflects the grounded company population materialized into
  the claim graph as of ingest — NOT the entire market / all startups. This honest
  coverage statement is REQUIRED.
"""


def _entity_names(market_map: dict) -> dict[str, str]:
    """entity_id -> display name, harvested from the map's clustered company structs
    (the citation rows carry only `entity_id`). Deterministic; last non-empty wins."""
    names: dict[str, str] = {}
    for cat in market_map.get("categories") or []:
        for comp in cat.get("companies") or []:
            eid = comp.get("entity_id") or ""
            nm = comp.get("name") or ""
            if eid and nm:
                names[eid] = nm
    return names


def _cell_display(entry: dict) -> str:
    """A cell entry renders its stated value, else the referenced entity id."""
    return entry.get("value") or entry.get("entity_id") or ""


def render_market_map_for_compose(market_map: dict) -> str:
    """Render the grounded market map into the compact map + numbered citation panel the
    compose LLM cites against. PURE + deterministic (mirrors the map's own stable
    ordering). Returns a single string with two sections: `## Market map` and
    `## Citation panel`. A cell with no grounded fact is simply absent (the prompt reads
    that as `not_collected`)."""
    names = _entity_names(market_map)
    categories = market_map.get("categories") or []
    citations = market_map.get("citations") or []

    lines: list[str] = ["## Market map (grounded company population)"]
    if not categories:
        lines.append("_(no grounded companies in scope)_")
    for cat in categories:
        comps = cat.get("companies") or []
        lines.append("")
        lines.append(f"### {cat.get('name') or cat.get('object_norm') or 'Uncategorized'} "
                     f"({len(comps)} companies)")
        for comp in comps:
            nm = comp.get("name") or comp.get("entity_id") or "(unnamed)"
            lines.append(f"- **{nm}**")
            cells = comp.get("cells") or {}
            for pred, entries in cells.items():
                rendered = "; ".join(
                    f"{_cell_display(e)} [{e.get('citation_id')}]" for e in entries
                    if _cell_display(e))
                if rendered:
                    lines.append(f"    - {pred}: {rendered}")

    lines.append("")
    lines.append("## Citation panel")
    lines.append("_Cite these ids. Every company/fact you state MUST carry one of these "
                 "`[cN]` markers; nothing outside this panel may appear in the answer._")
    for c in citations:
        cid = c.get("citation_id") or ""
        company = names.get(c.get("entity_id") or "", c.get("entity_id") or "?")
        pred = c.get("predicate") or ""
        obj = c.get("object") or ""
        quote = (c.get("quote") or "").replace("\n", " ").strip()
        if len(quote) > 240:
            quote = quote[:237] + "…"
        lines.append(f"[{cid}] {company} — {pred}: {obj}  (quote: \"{quote}\")")

    return "\n".join(lines)


def render_population_flat(companies: list[dict]) -> tuple[str, list[dict]]:
    """Render the WHOLE grounded company population as a FLAT list (no pre-grouping) plus
    a numbered citation panel, and return `(rendered, citations)`. The compose LLM derives
    the market segments itself from this flat view (fixing category fragmentation — the
    stored `operates_in_category` labels are too specific to cluster on). Deterministic:
    companies keep `population_claims` order; citation ids are assigned c1,c2,… in a stable
    walk (company order, then that company's claim order). Only claims with ACTIVE evidence
    (the read already excludes others) become citations. PURE (no DB/LLM)."""
    citations: list[dict] = []
    lines: list[str] = ["## Grounded company population (cluster these yourself)"]
    n = 0
    for comp in companies or []:
        eid = comp.get("entity_id") or ""
        nm = comp.get("name") or eid or "(unnamed)"
        rows: list[str] = []
        for cl in comp.get("claims") or []:
            ev = cl.get("evidence")
            if not ev or not (ev.get("quote") or "").strip():
                continue
            n += 1
            cid = f"c{n}"
            obj = cl.get("object_entity_id") or cl.get("object_value") or ""
            quote = (ev.get("quote") or "").replace("\n", " ").strip()
            citations.append({
                "citation_id": cid, "entity_id": eid, "name": nm,
                "predicate": cl.get("predicate") or "", "object": obj,
                "document_id": ev.get("document_id"), "block_id": ev.get("block_id"),
                "quote": ev.get("quote"), "authority_tier": ev.get("authority_tier"),
            })
            rows.append(f"    - {cl.get('predicate')}: {obj} [{cid}]")
        if not rows:
            continue                      # a company with no grounded cell is not renderable
        lines.append(f"- **{nm}**")
        lines.extend(rows)

    lines.append("")
    lines.append("## Citation panel")
    lines.append("_Cite these ids. Every company/fact you state MUST carry one of these "
                 "`[cN]` markers; nothing outside this panel may appear in the answer._")
    for c in citations:
        quote = (c.get("quote") or "").replace("\n", " ").strip()
        if len(quote) > 240:
            quote = quote[:237] + "…"
        lines.append(f"[{c['citation_id']}] {c['name']} — {c['predicate']}: {c['object']}  "
                     f"(quote: \"{quote}\")")
    return "\n".join(lines), citations


def render_coverage_facts(coverage_basis: dict[str, Any]) -> str:
    """Render the structured coverage facts the route assembles into the compact block
    the compose LLM turns into its `## Coverage basis` section. Pure + defensive."""
    cats = coverage_basis.get("categories_covered") or []
    trunc = coverage_basis.get("truncation") or {}
    parts = [
        "COVERAGE FACTS (build the required `## Coverage basis` from these):",
        f"- categories covered ({len(cats)}): " + (", ".join(cats) if cats else "none"),
        f"- categories in graph total: {coverage_basis.get('n_categories_total', 0)}; "
        f"in this map: {coverage_basis.get('n_categories_in_scope', len(cats))}",
        f"- companies mapped: {coverage_basis.get('n_companies', 0)}",
    ]
    if trunc.get("companies_truncated"):
        parts.append(f"- COMPANY LIST TRUNCATED at cap={trunc.get('company_cap')}: the map "
                     "shows only the first companies by name, not the whole population.")
    if trunc.get("claims_truncated"):
        parts.append("- some companies had their per-company facts truncated (cap "
                     f"{trunc.get('claims_per_company_cap')}).")
    parts.append("- POPULATION STATEMENT (state plainly): " +
                 coverage_basis.get("population_statement", ""))
    return "\n".join(p for p in parts if p)
