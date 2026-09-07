# Jobs and Talent Search Review

**Date:** 2026-09-07  
**Repository:** Roster  
**Comparison implementation:** /Users/sgupta/eigen/apps/api/startups  
**Review type:** Deep code and architecture review; no production code changes

## Executive Summary

Roster has the beginnings of a strong AI search platform: a typed facet contract, a deterministic
evaluator, index-aware compilation, relaxation, multi-recipe retrieval, reciprocal-rank fusion,
coverage reporting, and an optional blind judge. Those pieces are substantially more principled
than the older search code.

The problem is that they are not yet the product's single search system. Jobs and talent currently
span several generations of implementation:

1. Legacy SQL, vector, and heuristic pipelines in apps/api/claimgraph.py and
   apps/api/people_population.py.
2. The newer FacetSQLStore plus kernel contract evaluator.
3. AI-assisted intake and merged search in consultant.py, facets_engine.py, and
   contract_search.py.

The active route and feature flags decide which generation runs. Consequently, the same user intent
can receive different hard-filter behavior, scope semantics, identity handling, score calibration,
coverage statements, and explanations. This is the central design issue: the system is evolving by
adding capable patches, but does not yet have one enforceable search contract.

The recommended direction is to make the contract evaluator and a canonical store protocol the
only search execution path for jobs, talent, and startups. LLMs should compile and expand intent;
retrieval, filtering, identity, evidence, ranking, and calibration should be deterministic or
explicitly bounded model stages behind that protocol.

## Scope and Method

This review examined:

- Jobs routing and search branches in apps/api/app.py.
- Resume-to-job and JD-to-person matching in apps/api/people_population.py.
- SQL, vector, facet, and graph retrieval in apps/api/claimgraph.py.
- The newer generic adapter in apps/api/facet_store.py.
- Contract semantics and evaluation in packages/kernel/roster_kernel/facets.
- Roster's vertical schema and guided intake.
- Eigen's startup search store, router, compiler, and merged search implementation.

The review used source inspection, a code-grounded verification subagent, and local tests. No
production data, production API, or production embedding calls were made; external review calls
were limited to critique of the local document.

## Severity Model

- P1: Can return materially wrong, out-of-scope, suppressed, or misleading results in normal
  operation.
- P2: Degrades relevance, consistency, explainability, or maintainability; may become P1 for
  specific users or feature combinations.
- P3: Local correctness, test, or cleanup issue with limited search impact.

Findings marked P1/P2 are boundary cases: they are P1 when the affected route or flag is active
for a user, but P2 as a platform-wide issue because another route may still behave correctly.

## Findings

### P1. Generic contract scope is inert

Contract has a scope field and serializes it in
packages/kernel/roster_kernel/facets/contract.py:26 and :32-35. However, the generic evaluator
uses only contract.must when it calls semantic and enumeration retrieval, and only re-applies must
when it filters the resulting pool:

- packages/kernel/roster_kernel/facets/evaluate.py:43
- packages/kernel/roster_kernel/facets/evaluate.py:64-84
- packages/kernel/roster_kernel/facets/evaluate.py:106-108
- apps/api/facet_store.py:183-242

The jobs route compiles a country scope at apps/api/app.py:2962-2979, but the generic evaluator
does not lower that scope into retrieval or filtering. The returned contract can therefore say
that it is scoped while the candidates and counts are global.

This is more than a naming problem. Scope often has different semantics from a user-selected
facet:

- Unknown location may be retained for recall while confirmed foreign rows are excluded.
- A country selector may be a hard security or product boundary.
- A query-named location may override the default selector.
- Counts, coverage, and relaxation must all use the same scoped population.

Why Eigen is better here: Eigen's startup router creates a bound store with the contract's
exclusions and passes them through semantic, enumerate, counts, and slice operations at
/Users/sgupta/eigen/apps/api/startups/routes.py:140-160; the store applies them in SQL at
/Users/sgupta/eigen/apps/api/startups/store.py:480-510 and on every read path.

Recommendation: Add an explicit SearchContext to the store/evaluator protocol, containing tenant,
scope, status policy, and exclusion policy. Every retrieval leg, count, slice probe, relaxation
probe, and final filter must receive it. Do not rely on app code to copy scope into must; retain
scope as a first-class typed operation because its unknown-value behavior differs from ordinary
facets.

### P1. Inactive or suppressed people can leak through legacy retrieval

The newer facet adapter checks rs_entity.status = active for people. Several older retrieval
paths do not:

- semantic_people() scans rs_person_vec without joining rs_entity or checking status at
  apps/api/claimgraph.py:1860-1879.
- match_people_scored() has the same vector-table-only shape at
  apps/api/claimgraph.py:1965-1978.
- people_by_geo() reads facet rows without an active-entity join at
  apps/api/claimgraph.py:1736-1761.
- people_with_artifacts() also returns entity IDs without status filtering at
  apps/api/claimgraph.py:1763-1776.

The inconsistency is visible within the same module: name lookup does check active status at
apps/api/claimgraph.py:1803, while vector and geographic recall do not.

This can surface suppressed people in semantic-first, local-recall, artifact-recall, resume-match,
or JD-to-person flows depending on flags and query shape. It is also an identity and privacy issue,
not only a ranking issue.

Recommendation: Make active status and tenant binding mandatory at the storage boundary. Vector
tables should be joined to the canonical entity table in SQL, with the status predicate in the
same query. Add regression fixtures for suppressed people appearing in vectors, artifacts, and
facets.

### P1/P2. Job retrieval does not return a canonical row shape

The legacy job methods return different subsets of job data:

- search_jobs() selects company, title, location, department, URL, source, and date but omits
  id, skills, facets, body text, and similarity at apps/api/claimgraph.py:1606-1636.
- semantic_jobs() makes the same omission and does not return the similarity it uses to order
  rows at apps/api/claimgraph.py:1881-1897.
- jobs_local() and match_jobs_scored() return richer rows, including IDs, skills, facets,
  body snippets, and similarity at apps/api/claimgraph.py:1899-1963.

Downstream code expects those fields for materially important behavior:

- Job identity, exclusions, and rotation at apps/api/people_population.py:843-852 and :897-906.
- Skill overlap and field penalties at apps/api/people_population.py:865-889.
- Deduplication and company diversity at apps/api/people_population.py:910-936.
- Evaluator card hydration and session persistence in apps/api/app.py:2994-3005 and :3159-3166.

The current behavior often degrades quietly: composite fallbacks may keep a card visible, but the
card cannot be reliably excluded, rotated, reranked, or explained.

Recommendation: Define one internal SearchRow shape for every vertical:

    id, kind, tenant_id, sim, score, match_pct
    facets, numeric, display, provenance
    title/name, organization/company, location
    source, url, updated_at, evidence, reasons

Every retriever may return a sparse candidate, but hydration must produce the canonical shape before
ranking or rendering. Missing identity should be a rejected candidate, not a tolerated fallback.

### P1/P2. Multiple search engines produce route-dependent semantics

/jobs can execute at least these branches:

- Facet contract evaluation at apps/api/app.py:2962-3005.
- Agentic multi-angle retrieval at apps/api/app.py:3002-3095.
- Direct semantic or keyword retrieval at apps/api/app.py:3100-3166.
- Resume-to-job matching at apps/api/app.py:2870-2960.
- LinkedIn and saved-profile special cases earlier in the route.

Talent similarly has multiple engines:

- Legacy facet enumeration and semantic-first population search.
- Topic-anchor, evidence, and local-recall branches in
  apps/api/people_population.py:3166-3267.
- JD-to-person matching at apps/api/people_population.py:1768-1892.
- The new contract path in apps/api/app.py:4038-4085.

The implementations disagree about:

- Whether a facet is a hard filter or a soft boost.
- Whether unknown geography is retained.
- Whether status is enforced.
- Which fields are included in the candidate pool.
- Whether semantic similarity or seniority/evidence leads.
- How match_pct is calibrated.
- Whether coverage and counts describe the actual returned population.

Feature flags are therefore not merely rollout controls. They change product semantics.

Recommendation: Let AI planning generate a contract, query angles, and retrieval hints, but route all
execution through one evaluator. Keep old engines only as explicitly named migration adapters with
parity tests, not as competing user-visible products.

### P2. Direction classification in the v3 consultant is mismatched

The vertical prompt returns looking | hiring | null at
packages/vertical_roster/roster_vertical/intake.py:123-132, and defines the correct mapping at
:135. The v3 consultant reads the response but accepts only job | candidate at
apps/api/consultant.py:77-90.

The older intake correctly uses DIRECTION_TOKENS at apps/api/intake.py:261-274. Existing
consultant tests often pass an explicit direction or use prompts that bypass the automatic read,
which is why the defect can survive the suite.

Recommendation: Apply the vertical mapping in the consultant, then add tests for automatic
first-turn classification with looking, hiring, and null responses.

### P2. Company normalization is inconsistent

The F500 set is normalized to slug-like values at
apps/api/people_population.py:124-135, but resume job ranking compares the display company
directly at :855:

    is_f500 = co in _F500

The job-must path correctly normalizes both sides at :1733-1735:

    con = _norm_co(co)
    f500 = co in _F500 or con in _F500

Thus the same company may pass an explicit F500 filter but fail the corresponding ranking bonus.
There are similar normalization differences between search_jobs() and jobs_for_companies() in
claimgraph.py:1613-1647.

Recommendation: Define one canonical organization key and use it for storage, comparisons,
deduplication, filters, and display aliases. Keep the original display name separately.

### P2. Geography is applied after candidate truncation in some flows

Resume matching builds a candidate pool, ranks it, truncates it, then applies final job scope at
apps/api/people_population.py:937-951. Direct jobs search retrieves a capped semantic pool at
apps/api/app.py:3121-3129 and applies local widening/filtering afterward at :3130-3142.

If the top semantic candidates are globally strong but geographically wrong, local candidates that
were below the retrieval cut cannot enter the result set. This is a recall failure caused by plan
ordering, not by the ranking model.

Recommendation: Every hard scope must be pushed into retrieval where possible. When unknown
location recall is intentionally retained, use separate bounded legs:

1. Exact scoped candidates.
2. Unknown-location candidates.
3. Global semantic candidates as a clearly labeled fallback.

Merge before the final limit, then rank and partition.

### P2. Refined direct job search embeds stale text

The direct route parses a refinement into q at apps/api/app.py:3105-3120, but then embeds
body.question at :3123. Lexical search uses the refined query while semantic search remains
anchored to the original question.

Recommendation: Create a normalized query text once after parsing and refinement. Use that text
for embedding, lexical expansion, logging, session history, and contract display.

### P2. JD-to-talent country filtering reads only one country

At apps/api/people_population.py:1824-1829, the helper fval() returns the first matching facet
value. Country filtering then compares only that value at :1829. A person with multiple countries
or affiliations can be dropped even when one country satisfies the query.

The same function correctly checks all company facets for exclusions at :1843-1849, which gives a
clear local pattern for how country should be handled.

Recommendation: Use _person_countries() or an equivalent all-values helper for every multi-valued
country/location facet.

### P2. Vibe-search geography contradicts semantic-first geography

Semantic-first talent search retains unknown country/location for recall and drops only confirmed
foreign rows at apps/api/people_population.py:3166-3225. The vibe branch instead requires every
requested geo facet to be present in _geo_ok() at :3256-3264.

The same natural-language location intent therefore has different unknown-data behavior depending
on whether the query contains enough non-geo facets to enter semantic-first or vibe mode.

Recommendation: Encode unknown handling in the contract/context policy, not in branch-local helpers.
All retrieval modes must call the same geo predicate.

### P2. Match percentages do not have one meaning

Examples of incompatible score presentation:

- Generic evaluator uses calibrated similarity at
  packages/kernel/roster_kernel/facets/evaluate.py:149-155.
- Semantic-first talent uses raw sim * 100 at
  apps/api/people_population.py:3383-3387.
- Job matching uses a job-specific baseline and fit adjustment at
  apps/api/people_population.py:875-907.
- JD-to-person matching uses another calibration path around
  apps/api/people_population.py:1850-1864.

A user cannot reliably compare an 80% talent result, an 80% resume-job result, and an 80%
contract-evaluator result.

Recommendation: Separate raw retrieval similarity, rank score, and user-facing fit confidence.
Calibrate the latter per vertical and query class using held-out judged data. Expose the confidence
definition and abstain/weak-result state rather than implying universal probability.

### P2. Final ordering and persisted history can diverge

The direct /jobs route saves the session at apps/api/app.py:3159-3161, then applies level
preference ranking at :3162-3166. History can therefore store a different order from the response.
Similar divergence exists in legacy talent where answer text is assembled before final evidence,
level, country, and topic partitions.

Recommendation: Persist only after all ranking, deduplication, partitioning, and card projection
steps are complete. Treat the final SearchResultSet as the source for both response and history.

## What Is Already Good

The newer architecture contains several strong ideas worth preserving:

- FacetContract makes must/prefer/avoid semantics explicit.
- FacetSQLStore re-applies musts after retrieval, reducing leaky-pool risk.
- Semantic failure degrades to facet enumeration rather than returning an unexplained empty result
  in packages/kernel/roster_kernel/facets/evaluate.py:90-104.
- Index-aware compilation can demote sparse compiled constraints while preserving user-owned
  constraints in apps/api/contract_search.py:71-135.
- Merged search probes alternative recipes, fuses them with RRF, and judges only a blind head in
  apps/api/contract_search.py:151-239.
- Coverage reports pool size, unknowns, weak matches, degradation, and diagnostics.
- Eigen's startup implementation provides a working pattern for a vertical-specific schema over a
  shared evaluator/store protocol.

These should become the common platform rather than another parallel branch.

## Target Architecture

### 1. Typed intent contract

The compiler should produce a versioned contract with:

- kind: job, person, startup, company, or future entity kind.
- text: semantic query text after removing purely structural constraints.
- must, prefer, avoid, and center.
- scope: country, workspace, tenant, visibility, and unknown-data policy.
- exclude_ids and explicit user exclusions.
- angles: optional query rewrites, not independent execution semantics.
- user_keys: constraints explicitly supplied by the user versus inferred by the compiler.
- contract_version and compiler provenance.

The contract is a plan input, not an SQL fragment and not a model-generated final result.

The compiler must be treated as an untrusted parser. A deterministic schema-validation middleware
must reject or map out-of-vocabulary facets, normalize aliases, enforce allowed scope keys, and
preserve the original text when a structured interpretation is uncertain. A malformed or
overconfident contract must degrade to semantic or lexical retrieval with an explicit diagnostic,
not silently become an empty must-slice.

### 2. Search context and store protocol

Every store call should receive a context equivalent to:

    SearchContext(
        tenant_id,
        visibility_policy,
        status_policy,
        scope,
        unknown_policy,
        freshness_policy,
        excluded_ids,
    )

The protocol should expose:

    enumerate(kind, must, context, cap)
    semantic(kind, text, must, context, cap)
    lexical(kind, text, must, context, cap)
    graph(kind, anchors, must, context, cap)
    counts(kind, must, context)
    slice_size(kind, must, context)
    hydrate(kind, ids, context)

No route should call a raw vector table or facet table directly for user-visible search.

### 3. Multi-leg retrieval

Use separate recall legs, all bound by the same hard context:

1. Exact facet enumeration for structured constraints.
2. Lexical retrieval using PostgreSQL full-text/trigram search over titles, profiles, skills, and
   evidence text.
3. Dense retrieval over normalized entity/job/profile representations.
4. Graph retrieval for employer, founder, colleague, repository, paper, and organization edges.
5. Query-angle retrieval for semantic alternatives proposed by the compiler.

Each leg returns stable IDs and raw retrieval features. The union is deduplicated before ranking.

### 4. Ranking

The ranking stack should be:

1. Hard contract and scope filtering.
2. Candidate-level feature extraction.
3. Dense/lexical/graph fusion, preferably with explicit feature logging.
4. Cross-encoder or learned reranker over the top bounded candidate set.
5. Vertical-specific boosts for evidence strength, freshness, seniority, completeness, or diversity.
6. Final calibration and abstention/weak-result classification.

An LLM judge can remain a bounded head-quality check, but it should not be the only reranker and
must never override hard constraints or typed evidence.

### 5. Execution budgets and failure containment

The five retrieval legs are a recall design, not a requirement that every query fan out without
limits. The planner should select legs by query class and enforce budgets for:

- Maximum candidates per leg and maximum union size.
- Per-leg and total latency deadlines.
- Maximum cross-encoder batch size.
- Maximum judge rows and model/API spend.
- Cancellation of slow optional legs after the primary result is viable.

The response should expose which legs completed, timed out, or degraded. This prevents a principled
multi-leg design from becoming a tail-latency and memory problem at production scale.

### 6. Evidence and coverage

Every result should distinguish:

- What was retrieved semantically.
- What hard constraints it satisfied.
- What evidence supports each displayed facet.
- What is unknown because the corpus lacks data.
- What is weak because the semantic match is below the query noise floor.
- Whether the result came from the main, local, topic, graph, or fallback leg.

This is especially important for talent: a semantic resemblance is not proof of a skill, employer,
title, or connection.

## Vertical Responsibilities

The kernel should own mechanics and remain domain-free. The vertical should own judgment:

| Concern | Kernel | Jobs/talent/startups vertical |
| --- | --- | --- |
| Contract grammar | Yes | Vocabulary and prompt |
| Retrieval protocol | Yes | Schema-specific adapter |
| Tenant/status/scope enforcement | Yes | Policy values |
| Facet vocabulary | No | Yes |
| Evidence authority | No | Yes |
| Score calibration machinery | Yes | Calibration data and parameters |
| Card projection | No | Yes |
| Query compiler | Framework | Vertical prompt and normalization |
| Graph traversal mechanics | Yes | Edge vocabulary and authority |

This preserves the repo's kernel/vertical split while allowing the same search platform to support
people, jobs, companies, and startups.

## Migration Plan

### Phase 0: Restore invariants

Implement and test the following before adding more AI behavior:

- Fix consultant direction mapping.
- Add active-status joins to all legacy people retrieval.
- Add tenant/status/scope context to the store protocol.
- Enforce scope in evaluator retrieval, counts, slices, and final filtering.
- Normalize organization keys centrally.
- Require canonical IDs in all job rows.
- Use the normalized/refined query for embeddings and history.
- Persist sessions only after final ranking.
- Use all values for multi-valued country/location facets.

### Phase 1: Contract convergence

- Compile plain jobs searches into job contracts.
- Compile resume searches into the same job contract plus profile-derived preferences.
- Compile JD-to-person searches into the same person contract.
- Route guided intake, direct search, map navigation, and refinement through one evaluator.
- Keep legacy retrieval only behind adapters while parity tests run.

### Phase 2: Retrieval quality

- Add lexical retrieval as a first-class leg.
- Add entity/profile field-specific embeddings alongside combined embeddings.
- Add graph retrieval for employer, founder, colleague, paper, repository, and organization edges.
- Add cross-encoder reranking over a bounded union.
- Keep RRF and blind judging as measurable, optional stages rather than hidden behavior.

### Phase 3: Evaluation and learning

Create held-out query sets for jobs, talent, and startups covering:

- Exact structured queries.
- Ambiguous natural-language queries.
- Sparse and impossible constraints.
- Multi-location and unknown-location cases.
- Suppressed/inactive entities.
- Same-name people and duplicate job postings.
- Employer aliases and company groups.
- Evidence-backed versus merely semantically similar talent.
- Connection and graph queries.

Track:

- Recall@50.
- Precision@10 and nDCG@10.
- Constraint violation rate.
- Scope/status leakage rate.
- Duplicate rate.
- Coverage and unknown-data honesty.
- Weak-result and abstention accuracy.
- P50/P95 latency.
- Embedding, reranker, and judge cost.

Run old and new pipelines in shadow mode, compare per-query-class metrics, then migrate by route
only when the new path meets invariant and relevance thresholds.

## Test and Verification Notes

Focused search tests passed:

    .venv/bin/python -m pytest \
      apps/api/test_search_evaluate.py \
      apps/api/test_contract_search.py \
      apps/api/test_consultant.py \
      apps/api/test_jobs_intake.py \
      apps/api/test_match_pct.py -q

    46 passed

The full API suite produced:

    434 passed, 44 skipped, 6 failed

The six failures were in focus, graph-expander, and media-text tests rather than jobs/talent search.
They should still be treated as tracked repository failures, not normalized away.

The code-grounded verification subagent independently confirmed the principal findings and their
file/line locations. The external CLI panel was partially unavailable in this environment:

- Codex CLI could not use the installed gpt-5.5; its fallback gpt-5.6-sol required a newer CLI.
- Gemini launched but could not read /Users/sgupta/eigen because that path was outside its trusted
  workspace during the first repository-wide attempt. A second document-only Gemini pass completed
  and identified the need for explicit P1/P2 labeling, retrieval budgets, and compiler validation.
- Claude was installed but not authenticated; its non-interactive review returned `Not logged in`.

The Eigen comparison in this document is therefore based on direct source inspection, not a claimed
Gemini or Claude endorsement. A future review pass should run Gemini and Claude with both repositories
mounted in their trusted workspace and ask them to critique this document specifically for missed scope,
identity, ranking, and evaluation risks.

## Final Assessment

Roster is not starting from zero. The contract/evaluator/merged-search work contains the right
mechanical direction, and Eigen's startup search provides a useful reference implementation.

The highest-leverage work is convergence, not another retrieval heuristic. Until every search path
shares canonical identity, scope, active-status, evidence, and score contracts, adding more agents,
query variants, or judges will increase surface area faster than quality.

The platform becomes SOTA when AI expands and interprets intent while a typed, observable,
vertical-neutral search core guarantees what may enter the candidate set, how it is ranked, what the
system knows, and what it is uncertain about.
