"""Task D3 — single-company DILIGENCE answer route (Slice-2 depth).

`answer_diligence(...)` answers a "diligence this company" question END TO END,
GROUNDED, by reading ONE company's accumulated claims from the graph and organizing
them BY DIMENSION (breadth of dimensions is the priority):

    resolve company → subject_id (strong id / alias / exact normalized name)
      → entity_claims(subject_id, SLICE1+DILIGENCE predicates)          [single-company read]
      → render_entity_state_for_compose (dimension-grouped, per-cell CITED)
      → empty / unresolved honest fail-safe (never fabricate)
      → llm.complete compose (TECH_DILIGENCE_COMPOSE_PROMPT)            [LLM owns prose]
      → CITATION VALIDATION gate (every surviving [cN] must resolve)    [code owns it]
      → derive() + _render_derivations (grounded reasoning layer)
      → coverage_basis (dimensions covered vs not_collected)

This is a NEAR-VERBATIM clone of `population_route.answer_from_population`: it REUSES
that module's `_Finding`, `_validate_citations`, and `_PopulationAnswer` verbatim so the
compose call, the citation gate, and the derive layer are identical in kind. The only
net-new parts are the single-company read + the dimension-grouped renderer/coverage
(both in `roster_vertical.diligence_compose`).

Grounding is the moat: the compose prompt forbids any uncited proper noun / number, the
citation gate GUARANTEES every surviving `[cN]` resolves to a stored quote, and an
unresolved company OR an empty (no-grounded-claim) company returns an HONEST no-data
answer rather than parametric fabrication. Fail-safe throughout.

Rule 18 split: the LLM owns the meaning/prose/assessment; CODE owns the dimension map
(config), the citation-resolution gate, and the coverage facts. Not wired into the main
`_do_research` path — a separate, flag-gated endpoint so the OFF path stays byte-identical.
"""
from __future__ import annotations

import logging

from api.claimgraph import ClaimGraphStore
from api.claimgraph_tech import make_tech_claim_store
# Reuse the population route's gate + finding + response shape VERBATIM (do not reinvent).
from api.population_route import _Finding, _PopulationAnswer, _validate_citations
from roster_kernel.research.reason import derive
from roster_kernel.runtime.research import _render_derivations
from roster_vertical.claim_predicates import active_predicates
from roster_vertical.diligence_compose import (
    DIMENSIONS,
    TECH_DILIGENCE_COMPOSE_PROMPT,
    dimension_coverage,
    render_diligence_coverage_facts,
    render_entity_state_for_compose,
)

_log = logging.getLogger(__name__)

_POPULATION_STATEMENT = (
    "This brief reflects the grounded facts materialized into the claim graph as of "
    "ingest for this one company (active-evidence claims only) — NOT a complete picture.")


def _empty_result(*, answer: str, subject_id: str | None, coverage_basis: dict,
                  stats: dict, meta: dict, unresolved: bool) -> dict:
    """Honest no-data shape (unresolved company OR company with no grounded claim).
    Same key set as the success path; never fabricates an answer."""
    return {
        "answer": answer,
        "citations": [],
        "stats": stats,
        "coverage_basis": coverage_basis,
        "n_uncited_dropped": 0,
        "meta": meta,
        "derivations_rendered": "",
        "subject_id": subject_id,
        "unresolved": unresolved,
        "empty": True,
    }


async def _resolve_subject(store: ClaimGraphStore, company: str,
                           tenant_id: str) -> tuple[str | None, str | None]:
    """company → (subject_id, display_name). Strong id first (`yc:…`/`domain:…`/any
    `scheme:id`), then exact alias, then exact normalized company-name match. No fuzzy ER
    (Rule 18: computable resolution only). Unresolved → (None, None)."""
    if ":" in company:
        ent = await store.get_entity(company, tenant_id=tenant_id)
        if ent:
            return ent["entity_id"], ent["name"]
    # Alias resolution, but the alias table is NOT tenant-scoped — only ACCEPT a hit whose
    # entity actually validates as active in THIS tenant, else fall through (a stale
    # cross-tenant alias must never resolve a company that isn't in this graph).
    ent_id = await store.resolve_alias(company)
    if ent_id:
        ent = await store.get_entity(ent_id, tenant_id=tenant_id)
        if ent:
            return ent["entity_id"], ent["name"]
    ent = await store.find_company(company, tenant_id=tenant_id)
    if ent:
        return ent["entity_id"], ent["name"]
    return None, None


async def answer_diligence(*, company: str, tenant_id: str, dsn: str, llm,
                           judge_llm=None) -> dict:
    """End-to-end grounded single-company diligence brief. Returns
      {answer, citations, stats, coverage_basis, n_uncited_dropped, meta,
       derivations_rendered, subject_id}.
    On an unresolved / empty company returns an HONEST no-data answer; on an
    unrecoverable error returns {error, answer:"", ...} — never a fabricated answer.
    See module docstring for the pipeline + grounding contract (mirrors
    `answer_from_population`)."""
    store = make_tech_claim_store(dsn)
    try:
        # 1) RESOLVE company → subject_id (strong id / alias / exact normalized name).
        subject_id, name = await _resolve_subject(store, company, tenant_id)
        if subject_id is None:
            answer = (
                f"No grounded company named '{company}' has been ingested into the claim "
                f"graph for tenant '{tenant_id}'. There is nothing to diligence yet — once "
                f"the company is extracted into the graph (grounded, with cited evidence), "
                f"this route will produce a dimension-by-dimension brief with a citation for "
                f"every fact.")
            return _empty_result(
                answer=answer, subject_id=None,
                coverage_basis={
                    "dimensions_covered": [],
                    "dimensions_not_collected": [d for d, _ in DIMENSIONS],
                    "n_cited_claims": 0, "company_name": company, "subject_id": "",
                    "population_statement": _POPULATION_STATEMENT, "tenant_id": tenant_id},
                stats={"n_dimensions_covered": 0, "n_dimensions_total": len(DIMENSIONS),
                       "n_cited_claims": 0, "n_founders": 0, "n_investors": 0},
                meta={"resolved": False, "company": company, "n_claims": 0},
                unresolved=True)

        # 2) SINGLE-COMPANY READ — active grounded claims across ALL slice predicates.
        pred_names = [p["name"] for p in active_predicates(include_diligence=True)]
        claims = await store.entity_claims(
            subject_id, predicates=pred_names, tenant_id=tenant_id)

        # 3) Render dimension-grouped state + the citation panel the LLM cites against.
        rendered, citations = render_entity_state_for_compose(name, claims)

        # coverage + stats (computed from the grounded claims / citations).
        cov = dimension_coverage(claims)
        n_founders = sum(1 for c in citations if c.get("predicate") == "has_founder")
        n_investors = sum(1 for c in citations if c.get("predicate") == "has_investor")
        stats = {
            "n_dimensions_covered": len(cov["covered"]),
            "n_dimensions_total": len(DIMENSIONS),
            "n_cited_claims": len(citations),
            "n_founders": n_founders,
            "n_investors": n_investors,
        }
        coverage_basis = {
            "dimensions_covered": cov["covered"],
            "dimensions_not_collected": cov["not_collected"],
            "n_cited_claims": len(citations),
            "company_name": name,
            "subject_id": subject_id,
            "population_statement": _POPULATION_STATEMENT,
            "tenant_id": tenant_id,
        }
        meta = {"resolved": True, "company": company, "subject_id": subject_id,
                "n_claims": len(claims)}

        # 4) EMPTY FAIL-SAFE — resolved but no grounded claim: honest no-data.
        if not citations:
            answer = (
                f"'{name}' is in the graph but has no grounded, cited claims across any "
                f"diligence dimension for tenant '{tenant_id}' — there is nothing to report "
                f"yet. Once facts about it are extracted (grounded, with cited evidence), "
                f"this brief will fill in by dimension.")
            return _empty_result(
                answer=answer, subject_id=subject_id, coverage_basis=coverage_basis,
                stats=stats, meta=meta, unresolved=False)

        # 5) COMPOSE — self-contained diligence-compose prompt as system; the grouped
        #    state + citation panel + question + coverage facts as user (matches the
        #    population route's llm.complete shape).
        coverage_facts = render_diligence_coverage_facts(coverage_basis)
        user = (f"QUESTION:\nProduce a grounded diligence brief on {name}.\n\n"
                f"{rendered}\n\n{coverage_facts}")
        comp = await llm.complete(
            system=TECH_DILIGENCE_COMPOSE_PROMPT,
            messages=[{"role": "user", "content": user}],
            response_format=_PopulationAnswer, max_tokens=6000)
        answer = (comp.parsed.text or "").strip()

        # 6) CITATION VALIDATION GATE (code) — drop any hallucinated `[cN]`.
        valid_ids = {c.get("citation_id") for c in citations if c.get("citation_id")}
        answer, n_dropped = _validate_citations(answer, valid_ids)

        # 7) DERIVE — grounded reasoning layer over the cited facts (additive; a failure
        #    never sinks the answer). Each finding is one citation (text + verbatim quote).
        derivations_rendered = ""
        try:
            findings = [
                _Finding(
                    text=(f"{c.get('name') or c.get('entity_id') or ''} "
                          f"— {c.get('predicate') or ''}: {c.get('object') or ''}").strip(),
                    quote=c.get("quote") or "")
                for c in citations]
            ds = await derive(f"Diligence: {name}", findings, llm, judge_llm=judge_llm,
                              max_tokens=4000, judge_max_tokens=4000)
            section = _render_derivations(ds)
            if section:
                derivations_rendered = section
                answer = (answer.rstrip() + "\n\n" + section)
        except Exception as e:   # noqa: BLE001 — reasoning is additive, never break the answer
            _log.warning("diligence_route: derive failed (%s)", str(e)[:120])

        return {
            "answer": answer,
            "citations": citations,
            "stats": stats,
            "coverage_basis": coverage_basis,
            "n_uncited_dropped": n_dropped,
            "meta": meta,
            "derivations_rendered": derivations_rendered,
            "subject_id": subject_id,
            "unresolved": False,
            "empty": False,
        }
    except Exception as e:   # noqa: BLE001 — fail-safe: a clear error, never a fabricated answer
        _log.warning("diligence_route: answer_diligence failed (%s)", str(e)[:200])
        return {
            "answer": "",
            "citations": [],
            "stats": {},
            "coverage_basis": {},
            "n_uncited_dropped": 0,
            "meta": {},
            "derivations_rendered": "",
            "subject_id": None,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        try:
            await store.close()
        except Exception:   # noqa: BLE001
            pass
