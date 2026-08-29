"""Task 4 capstone — the population-query ANSWER ROUTE.

`answer_from_population(...)` answers a landscape/population question END TO END,
GROUNDED, by querying the POPULATION (the accumulated claim graph) rather than a
retrieval sample:

    distinct_categories (scope) → population_claims (grounded companies × cells)
      → build_market_map (per-cell CITED market map)
      → empty-population honest fail-safe (never fabricate)
      → render map + numbered citation panel
      → llm.complete compose (TECH_POPULATION_COMPOSE_PROMPT)   [LLM owns prose]
      → CITATION VALIDATION gate (every surviving [cN] must resolve)  [code owns it]
      → derive() + _render_derivations (grounded reasoning layer)
      → coverage_basis (structured coverage facts)

Grounding is the moat: the compose prompt forbids any uncited proper noun (prompt-
enforced, measured by the live eval), the citation gate GUARANTEES every surviving
`[cN]` resolves to a real citation (code-enforced), and an empty population returns an
HONEST no-data answer rather than parametric fabrication. Fail-safe throughout — an
LLM/DB error returns a clear error dict, never a made-up answer.

Rule 18 split: the LLM owns the meaning/prose; CODE owns the citation-resolution gate
and the coverage facts. Not wired into the main `_do_research` path — it is a separate,
flag-gated endpoint so the OFF path stays byte-identical.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel

from api.claimgraph_tech import TECH_POPULATION_PREDICATES, make_tech_claim_store
from roster_kernel.research.reason import derive
from roster_kernel.runtime.research import _render_derivations
from roster_vertical.population_compose import (
    TECH_POPULATION_COMPOSE_PROMPT,
    render_coverage_facts,
    render_population_flat,
)

_log = logging.getLogger(__name__)

# Matches a citation marker like `[c12]` (the ids build_market_map assigns are c1, c2…).
_CITE_RE = re.compile(r"\[(c\d+)\]")

_POPULATION_STATEMENT = (
    "This map reflects the grounded company population materialized into the claim "
    "graph as of ingest (companies with active-evidence claims only) — NOT all "
    "startups in the market.")


class _PopulationAnswer(BaseModel):
    """Prose compose output — matches the `_PlainAnswer` shape research.py uses so the
    `llm.complete(..., response_format=…)` call is identical in kind."""
    text: str


@dataclass
class _Finding:
    """Duck-typed finding for `derive()` (it reads `.text` and `.quote`). One per
    citation so every derivation rests on a grounded, cited fact."""
    text: str
    quote: str


def _validate_citations(answer: str, valid_ids: set[str]) -> tuple[str, int]:
    """CODE GATE (Rule 18): every `[cN]` marker in the composed answer must resolve to a
    real citation id from the panel. A marker that does not is a HALLUCINATED reference —
    strip that bracket token from the answer and count it. Returns (cleaned, n_dropped).

    This does NOT catch an UNCITED proper noun (that is what the live eval measures), but
    it GUARANTEES that every surviving `[cN]` in the answer resolves to a stored quote."""
    dropped = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal dropped
        if m.group(1) in valid_ids:
            return m.group(0)
        dropped += 1
        return ""

    cleaned = _CITE_RE.sub(_sub, answer or "")
    return cleaned, dropped


async def answer_from_population(*, question: str, tenant_id: str, dsn: str, llm,
                                 top_categories: int = 12, company_cap: int = 150,
                                 judge_llm=None) -> dict:
    """End-to-end grounded population answer. Returns
      {answer, citations, stats, coverage_basis, n_uncited_dropped, meta,
       derivations_rendered}.
    On an unrecoverable error, returns {error, answer:"", ...} — never a fabricated
    answer. See module docstring for the pipeline + grounding contract.

    The compose derives the market SEGMENTS itself from the FLAT grounded population
    (the stored `operates_in_category` labels are too fragmented to cluster on) — so the
    whole population is passed, not a top-N-categories slice."""
    store = make_tech_claim_store(dsn)
    try:
        # 1) POPULATION — the WHOLE grounded company population (not scoped to noisy
        #    top-N categories, which dropped most companies). The compose LLM clusters.
        #    Restricted to the SLICE1 landscape predicates (the map's cells) — the tech
        #    wiring supplies that vocabulary, keeping the map byte-identical post-decouple.
        cats = await store.distinct_categories(tenant_id=tenant_id)
        pop = await store.population_claims(
            tenant_id=tenant_id, category_norms=None, company_cap=company_cap,
            predicates=TECH_POPULATION_PREDICATES)

        # 2) Render FLAT (company → cited cells) + the citation panel the LLM cites against.
        rendered_map, citations = render_population_flat(pop["companies"])
        n_companies = sum(1 for c in pop["companies"]
                          if any((cl.get("evidence") or {}).get("quote") for cl in c.get("claims") or []))
        stats = {"n_companies": n_companies, "n_cited_claims": len(citations),
                 "n_categories_in_graph": len(cats)}

        # coverage_basis: structured facts (the compose ALSO emits a `## Coverage basis`,
        # but the caller gets the facts too). Assembled from cats + pop meta + the honest
        # population statement.
        coverage_basis = {
            "categories_covered": [c["name"] for c in cats[:top_categories]],
            "n_categories_total": len(cats),
            "n_categories_in_scope": len(cats),
            "n_companies": n_companies,
            "truncation": pop.get("meta") or {},
            "population_statement": _POPULATION_STATEMENT,
            "tenant_id": tenant_id,
        }

        # 3) EMPTY-POPULATION FAIL-SAFE — honest no-data, never parametric fabrication.
        if not citations:
            answer = (
                f"No grounded startup population has been ingested into the claim graph "
                f"yet for tenant '{tenant_id}' — there is nothing to map. Once companies "
                f"are extracted into the graph (grounded, with cited evidence), this route "
                f"will cluster them by market category with a citation for every fact.")
            return {
                "answer": answer,
                "citations": [],
                "stats": stats,
                "coverage_basis": coverage_basis,
                "n_uncited_dropped": 0,
                "meta": pop.get("meta") or {},
                "derivations_rendered": "",
                "empty_population": True,
            }

        # 4) COMPOSE — self-contained population-compose prompt as system; the FLAT
        #    population + citation panel + question + coverage facts as user. The LLM
        #    derives the segments itself. (matches research.py llm.complete shape.)
        coverage_facts = render_coverage_facts(coverage_basis)
        user = (f"QUESTION:\n{question}\n\n{rendered_map}\n\n{coverage_facts}")
        comp = await llm.complete(
            system=TECH_POPULATION_COMPOSE_PROMPT,
            messages=[{"role": "user", "content": user}],
            response_format=_PopulationAnswer, max_tokens=8000)
        answer = (comp.parsed.text or "").strip()

        # 5) CITATION VALIDATION GATE (code) — drop any hallucinated `[cN]`.
        valid_ids = {c.get("citation_id") for c in citations if c.get("citation_id")}
        answer, n_dropped = _validate_citations(answer, valid_ids)

        # 6) DERIVE — grounded reasoning layer over the cited facts (additive; a failure
        #    never sinks the answer). Each finding is one citation (text + verbatim quote).
        #    max_tokens raised (candidate generation truncated at the 2000 default).
        derivations_rendered = ""
        try:
            findings = [
                _Finding(
                    text=(f"{c.get('name') or c.get('entity_id') or ''} "
                          f"— {c.get('predicate') or ''}: {c.get('object') or ''}").strip(),
                    quote=c.get("quote") or "")
                for c in citations]
            ds = await derive(question, findings, llm, judge_llm=judge_llm,
                              max_tokens=4000, judge_max_tokens=4000)
            section = _render_derivations(ds)
            if section:
                derivations_rendered = section
                answer = (answer.rstrip() + "\n\n" + section)
        except Exception as e:   # noqa: BLE001 — reasoning is additive, never break the answer
            _log.warning("population_route: derive failed (%s)", str(e)[:120])

        return {
            "answer": answer,
            "citations": citations,
            "stats": stats,
            "coverage_basis": coverage_basis,
            "n_uncited_dropped": n_dropped,
            "meta": pop.get("meta") or {},
            "derivations_rendered": derivations_rendered,
            "empty_population": False,
        }
    except Exception as e:   # noqa: BLE001 — fail-safe: a clear error, never a fabricated answer
        _log.warning("population_route: answer_from_population failed (%s)", str(e)[:200])
        return {
            "answer": "",
            "citations": [],
            "stats": {},
            "coverage_basis": {},
            "n_uncited_dropped": 0,
            "meta": {},
            "derivations_rendered": "",
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        try:
            await store.close()
        except Exception:   # noqa: BLE001
            pass
