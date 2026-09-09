# Roster AI Search Platform v2

Status: DESIGN APPROVED. Date: 2026-09-08.

This specification defines the target architecture for Roster's next search system across people,
jobs, companies, organizations, and their relationships. It incorporates the comparison with Career
Ops and the existing Roster search review, facet evaluator, evidence model, edge model, and recruiter
workflow specifications.

The central decision is architectural:

> Roster owns the public evidence and search plane. Private candidate context is a separate decision
> plane. Application execution remains an optional client workflow, not part of the public graph.

The purpose of this document is to replace route-by-route search evolution with one set of contracts,
one evaluator, explicit evidence semantics, and independently measurable product layers.

## 1. Executive Summary

Roster should become an evidence-grounded search platform rather than a collection of specialized
people and jobs routes.

The target flow is:

```text
natural-language brief
  -> scoped SearchContext
  -> validated SearchContract
  -> index-aware contract correction
  -> multi-leg retrieval
  -> hard constraint enforcement
  -> relevance and preference ranking
  -> evidence attachment
  -> coverage and freshness calculation
  -> SearchRows
  -> map, dossier, Q&A, or workflow artifact
```

Every search surface must use this flow. The surface may choose a different presentation or a
different post-search workflow, but it must not own a separate interpretation or ranking algorithm.

The platform has five separable planes:

1. **Public corpus plane** — source documents, structured records, entities, claims, evidence, and
   source freshness.
2. **Search plane** — query compilation, contract validation, retrieval, filtering, ranking, coverage,
   and explanation.
3. **Private context plane** — résumé, preferences, authorization, compensation, exclusions, notes,
   and user-specific fit signals.
4. **Decision workflow plane** — maps, review states, dossiers, outreach preparation, applications,
   and interview outcomes.
5. **Evaluation plane** — relevance judgments, adversarial cases, latency, cost, freshness, and
   feedback calibration.

The public corpus and search planes belong in Roster. The private context and decision planes may be
implemented in Roster for convenience, but their data must remain explicitly isolated from public
facts. A client such as Career Ops may consume Roster's stable evidence-preserving export without
becoming the authority for shared people or company facts.

## 2. Current State and Problem

### 2.1 What already exists

Roster already has the important primitives:

- a domain-neutral kernel and a vertical vocabulary layer;
- a Postgres claim graph with evidence;
- a projected facet read model;
- lexical and vector corpus retrieval;
- people enumeration and profile cards;
- jobs search and résumé-to-job matching;
- a contract/evaluator design for `must`, `prefer`, `avoid`, `center`, and `rank_by`;
- typed evidence, source freshness, coverage, and grounded prose gates;
- saved maps, review states, calibration concepts, and bounded graph paths;
- worker-based ingestion and resumable bulk processing.

The existing specifications are directionally correct:

- [Facet contract and evaluator](facet-contract-evaluator.md)
- [Evidence model v2](evidence-model-v2.md)
- [Edge model](edge-model.md)
- [Talent intelligence redesign](talent-intelligence-redesign.md)
- [Recruiter workflows](recruiter-workflows.md)

### 2.2 The architectural defect

The implementation still contains multiple search generations. People search, jobs search,
résumé matching, Q&A population search, and older facet paths can interpret the same brief using
different scopes, hard filters, soft preferences, semantic pools, and score meanings.

This creates five classes of defect:

1. **Semantic drift** — the same words mean different things on different routes.
2. **Ranking drift** — scores and reasons are not comparable across surfaces.
3. **Scope drift** — geography, tenant, status, and exclusions are applied inconsistently.
4. **Evidence drift** — one path may return a row with a citation while another treats the same
   attribute as an uncited facet.
5. **Operational drift** — embedding failure, stale data, and partial ingestion produce different
   fallback behavior depending on the route.

This is not solved by adding another scorer or another prompt. It requires a single search contract
and evaluator, with compatibility adapters around legacy surfaces during migration.

### 2.3 Product distinction

Roster and Career Ops should remain separate products.

Career Ops is a strong reference for:

- private candidate context;
- liveness checks before expensive reasoning;
- explicit human approval;
- application and interview lifecycle;
- local, auditable work product;
- feedback from outcomes.

Roster is the shared platform for:

- public data ingestion;
- identity resolution;
- claim and evidence storage;
- population search;
- jobs, people, and company relationships;
- coverage and provenance.

Roster should borrow workflow concepts, not merge data models.

## 3. Goals

### 3.1 Primary goals

1. One validated search semantics for people, jobs, companies, maps, résumé matching, and Q&A.
2. First-class person, company, organization, institution, and job entities with stable identifiers.
3. Every material result attribute traceable to typed evidence or explicitly marked unknown.
4. Separate retrieval relevance, hard requirement satisfaction, evidence strength, freshness, and
   private-context fit.
5. Honest coverage: distinguish “no matching indexed evidence” from “not present in the corpus.”
6. Predictable degraded behavior when a model, embedding provider, connector, or source is unavailable.
7. Explicit isolation between public corpus facts and private user context.
8. Search artifacts that can be saved, reviewed, revised, exported, and refreshed.
9. Offline and online evaluation strong enough to prevent patch-driven regression.
10. A stable integration boundary for clients such as Career Ops.

### 3.2 Non-goals

This specification does not make Roster:

- a general ATS replacement;
- an automated hiring or rejection system;
- an application submission agent;
- a social network;
- a generic web-search proxy that ignores the corpus;
- a system that treats public artifacts as a requirement for talent quality;
- a single opaque “candidate score” product;
- a replacement for specialized application-writing tools.

## 4. Non-Negotiable Invariants

### 4.1 Contract invariant

All search-like operations execute a validated `SearchContract`.

No route may introduce private interpretation fields that bypass the contract. Legacy route inputs
must be compiled into the contract at the boundary.

### 4.2 Scope invariant

Scope is applied before retrieval ranking and before counts are reported.

Scope includes:

- tenant and workspace;
- entity kind;
- geography;
- entity status;
- job open/closed status;
- source visibility;
- explicit exclusions;
- private-context visibility where applicable.

Unknown geography is not silently treated as in-scope fact. It may remain in a result set only when
the contract permits unknowns and the response labels the uncertainty.

### 4.3 Evidence invariant

Every material factual result field is one of:

- supported by a typed evidence reference;
- explicitly inferred from supported evidence;
- unknown;
- stale;
- contradicted;
- unavailable because the required source is outside corpus coverage.

An ungrounded value is never silently promoted to fact.

### 4.4 Entity invariant

Stable identifiers, not names, are the identity boundary.

Name-only matches may create unresolved mentions or candidate links, but may not merge people. A
strong identifier or an evidence-backed resolution is required for a canonical person merge.

### 4.5 Ranking invariant

Hard constraints filter. Preferences rank. Evidence and freshness explain. None of these may be
silently collapsed into a hiring decision.

### 4.6 Degradation invariant

Provider failure must produce one of:

- a deterministic lexical/facet fallback;
- a clearly labeled degraded result;
- an honest empty result with a coverage explanation.

Provider failure must never return an arbitrary unlabelled page that looks like a high-confidence
semantic result.

### 4.7 Human decision invariant

Search and evidence may support a human decision. They must not state or imply an automated hiring
decision. Review states are user-owned workflow state, not truth about a person.

## 5. Target Architecture

### 5.1 Component map

```text
                         +---------------------------+
                         | Web / API / client export |
                         +-------------+-------------+
                                       |
                         +-------------v-------------+
                         | Search surface adapters   |
                         | jobs, people, maps, Q&A   |
                         +-------------+-------------+
                                       |
                         +-------------v-------------+
                         | Search orchestrator       |
                         | compile -> validate ->    |
                         | evaluate -> explain       |
                         +------+------+-------------+
                                |      |
               +----------------+      +----------------+
               |                                      |
      +--------v---------+                    +-------v--------+
      | Public search    |                    | Private context |
      | contract/eval    |                    | contract/eval   |
      +--------+---------+                    +-------+--------+
               |                                      |
      +--------v--------------------------------------v--------+
      | Candidate pool, ranking, evidence attachment, coverage |
      +----------------------+---------------------------------+
                             |
                 +-----------v------------+
                 | Claim/evidence graph   |
                 | facet read model       |
                 | corpus/vector index    |
                 +-----------+------------+
                             |
                 +-----------v------------+
                 | Connector + ingestion |
                 | identity + extraction |
                 +------------------------+
```

### 5.2 Kernel and vertical boundary

The kernel owns mechanics:

- contract grammar and validation;
- search orchestration;
- facet types and matching semantics;
- candidate pooling and deterministic ranking mechanics;
- coverage calculation;
- evidence reference shape;
- graph traversal mechanics;
- evaluation interfaces.

The vertical owns judgment and vocabulary:

- entity kinds and labels;
- facet schemas and values;
- source authority policy;
- relation predicates;
- extraction guidance;
- ranking weights;
- answer and dossier presentation;
- compliance and workflow language.

No recruiting, company, job, or talent noun belongs in the domain-neutral kernel.

## 6. Canonical Data Contracts

The following contracts are logical API contracts. Their exact Python representation may differ, but
the fields and semantics are stable.

### 6.1 Entity

```json
{
  "entity_id": "person:github:ada",
  "kind": "person",
  "canonical_name": "Ada Nguyen",
  "strong_ids": ["github:ada", "orcid:..."] ,
  "aliases": ["Ada N."],
  "status": "active",
  "created_at": "...",
  "updated_at": "..."
}
```

Required kinds:

- `person`
- `company`
- `organization`
- `institution`
- `job`
- `artifact`

The public graph may add vertical-specific kinds, but the above set is the minimum shared model.

### 6.2 Claim

```json
{
  "claim_id": "claim:...",
  "subject_id": "person:github:ada",
  "predicate": "works_at",
  "object_id": "company:stripe",
  "value": null,
  "valid_from": "2022-01-01",
  "valid_to": null,
  "asserted_at": "2024-05-10",
  "status": "active",
  "confidence_band": "corroborated",
  "evidence_refs": ["evidence:..."],
  "schema_version": "..."
}
```

Claims are append-oriented. A new observation does not rewrite history; it creates a new assertion
or changes the active-read interpretation with an auditable reason.

### 6.3 Evidence

```json
{
  "evidence_id": "evidence:...",
  "claim_id": "claim:...",
  "document_id": "github-profile:ada",
  "block_id": "block:...",
  "quote": "...",
  "evidence_kind": "self_stated",
  "source_family": "github",
  "authority_tier": "profile",
  "observed_at": "2024-05-10",
  "valid_from": null,
  "valid_to": null,
  "freshness_state": "current",
  "span_verified": true,
  "independence_group": "github_profile"
}
```

`evidence_kind` and `authority_tier` are distinct. A company page may be employer-stated but not
independently corroborated. A structured registry may be highly authoritative for an officer role
but irrelevant to technical capability.

### 6.4 SearchContext

```json
{
  "tenant_id": "tenant:...",
  "workspace_id": "workspace:...",
  "user_id": "user:...",
  "visibility": "public_corpus_plus_private_context",
  "entity_kind": "person",
  "geography": {"country": "us", "state": null, "metro": "bay_area"},
  "private_context_ref": "private-context:...",
  "source_policy": {"include": [], "exclude": []},
  "as_of": "2026-09-08"
}
```

`SearchContext` defines who may see the data and which corpus slice is eligible. It is not a ranking
object and must not be inferred from UI defaults after the request reaches the evaluator.

### 6.5 SearchContract

```json
{
  "version": 2,
  "kind": "person",
  "text": "senior infrastructure leaders with distributed systems evidence",
  "must": {
    "field": ["software"],
    "geo": ["us/ca/bay_area"]
  },
  "prefer": {
    "level": ["staff_plus"],
    "specialty": ["distributed systems"],
    "evidence": ["artifact_backed"]
  },
  "avoid": {
    "work_type": ["sales"]
  },
  "center": {"key": "level", "value": "senior", "span": 1},
  "rank_by": "match",
  "scope": {"country": "us"},
  "exclude_ids": [],
  "limit": 60,
  "angles": [],
  "provenance": {
    "compiled_from": "user_brief",
    "user_stated_keys": ["geo"],
    "model_inferred_keys": ["level", "specialty"]
  }
}
```

Rules:

- OR within a key, AND across keys.
- `must` filters before ranking.
- `prefer` and `avoid` influence ordering only.
- `center` influences ordinal distance only.
- unknown never satisfies a `must` and is neutral under preference.
- user-explicit constraints cannot be silently downgraded.
- model-compiled constraints may be downgraded when coverage or index-aware checks show that the
  promise cannot be kept; the response must explain the change.
- the contract is canonicalized, hashed, stored with maps, and included in every response.

### 6.6 SearchRow

```json
{
  "id": "person:github:ada",
  "kind": "person",
  "display": {"name": "Ada Nguyen", "title": "Staff Engineer"},
  "facets": {},
  "match": {
    "retrieval_score": 0.82,
    "match_band": "strong",
    "preference_adjustment": 0.07,
    "rank_reasons": ["specialty: distributed systems"]
  },
  "fit": null,
  "evidence": {
    "supporting": [],
    "gaps": [],
    "freshness": "mixed",
    "coverage": "partial"
  },
  "review": {"state": "unreviewed"}
}
```

`fit` is null for public population search unless a private `SearchContext` explicitly requests a
fit computation. The public search score must never be presented as candidate quality.

### 6.7 SearchResponse

```json
{
  "contract": {},
  "rows": [],
  "counts": {},
  "coverage": {
    "indexed_population": 0,
    "eligible_population": 0,
    "returned": 0,
    "unknown_by_key": {},
    "source_families": [],
    "freshness": {},
    "degraded": null
  },
  "diagnostics": {
    "legs": {},
    "timings_ms": {},
    "provider_status": {}
  }
}
```

## 7. Query Compilation and Intake

### 7.1 Compiler responsibilities

The LLM may:

- classify the entity kind;
- extract named entities and user-stated constraints;
- map language to vertical schema keys;
- propose semantic angles;
- label assumptions and ambiguity.

The LLM may not:

- execute filtering;
- invent facet values outside the schema;
- decide whether evidence supports a factual claim;
- change tenant or visibility scope;
- silently convert a preference into a hard constraint;
- assign a hiring decision.

### 7.2 Compiler output validation

Code validates:

- kind;
- key vocabulary;
- value vocabulary;
- numeric ranges;
- ordinal centers;
- scope;
- limit;
- exclusion identifiers;
- provenance of each contract component.

Invalid model output is repaired deterministically or rejected with a user-visible clarification. A
model failure must not fall through to a route-specific parser.

### 7.3 Clarification behavior

Clarification is additive, not blocking.

Roster must return an initial map under explicit assumptions, then ask at most one high-value
question when the answer could materially change the cohort. The response records:

- the original brief;
- the assumption made;
- the question asked;
- the contract before the answer;
- the contract after the answer.

## 8. Retrieval and Evaluation Pipeline

### 8.1 Retrieval legs

The evaluator may use these legs:

1. semantic vector retrieval over the eligible entity/document representation;
2. lexical retrieval over title, body, aliases, and extracted terms;
3. facet enumeration over `must` constraints;
4. semantic angle retrieval for compiler-proposed alternate phrasings;
5. preference-aware retrieval for high-value preferences;
6. graph expansion for explicit relationship queries.

Every leg receives the same scope and hard constraints. The evaluator re-applies `must` semantics to
the union so a leaky adapter cannot return a forbidden row.

### 8.2 Candidate pool

The candidate pool is the union of eligible leg results, deduplicated by stable entity or job ID.
Each row retains:

- best retrieval score;
- legs that produced it;
- source documents used;
- whether it came from fallback mode;
- contract constraints evaluated.

The pool must be large enough to support facet re-ranking but bounded by latency and cost budgets.
The chosen cap and actual pool size are returned in diagnostics.

### 8.3 Hard filtering

Hard filters are applied:

1. in the database adapter where possible;
2. again in evaluator code;
3. before ranking;
4. before counts and coverage are reported.

This double application is intentional defense against inconsistent store implementations.

### 8.4 Ranking layers

Ranking must remain decomposable:

```text
retrieval relevance
  + preference adjustment
  - avoidance adjustment
  - ordinal distance penalty
  + evidence-depth tie-breaker
  + freshness tie-breaker
  + diversity adjustment
```

The system must preserve each component in diagnostics. The UI may show a concise reason strip, but
the API must not expose only the final scalar.

### 8.5 Diversity

Population results must support configurable diversity constraints:

- maximum rows per company in the first page;
- diversity across source families;
- optional diversity across geography or role family;
- no diversity penalty when the user explicitly searches for one company.

Diversity is a presentation/ranking policy, not a hard fact about relevance.

### 8.6 Degraded retrieval

If embeddings are unavailable:

- use lexical and facet retrieval;
- preserve hard filters;
- label `coverage.degraded = semantic_unavailable`;
- do not call the result semantic ranking;
- expose the provider failure in diagnostics without leaking secrets.

If the database is unavailable, return a typed service-unavailable response. Do not substitute live
web search for the corpus silently.

## 9. Search Dimensions and Scores

Roster must not produce a universal “fit score.” It should expose named dimensions.

### 9.1 Public relevance

How closely the indexed representation matches the brief. This is retrieval quality, not truth or
employment suitability.

### 9.2 Constraint satisfaction

Which explicit hard requirements passed, failed, or were unknown. A hard requirement with unknown
data is not a pass.

### 9.3 Evidence strength

Per claim axis, show:

- self-stated;
- employer-stated;
- artifact-backed;
- structured;
- corroborated;
- inferred;
- weak;
- stale;
- gap;
- contradicted.

Evidence strength must be claim-specific. A person can have artifact-backed capability evidence and
uncorroborated seniority evidence simultaneously.

### 9.4 Freshness

Freshness is per claim axis, not just per entity. Current employer, technical artifact, location, and
education may have different dates.

### 9.5 Private-context fit

Only computed when the caller provides a private context. It may include:

- résumé-to-requirement support;
- work authorization;
- compensation preferences;
- location and work-mode preferences;
- culture and company-stage preferences;
- user exclusions.

Private fit never changes public claims and is never written back into public ranking features without
explicit aggregation and privacy review.

## 10. Entity, Identity, and Relationship Graph

### 10.1 Required entities

The graph must support first-class:

- people;
- companies;
- organizations;
- institutions;
- jobs;
- artifacts;
- sources/documents.

### 10.2 Professional predicates

The initial vertical predicate registry should include:

- `works_at`;
- `founded`;
- `founder_of`;
- `board_member_of`;
- `advisor_to`;
- `educated_at`;
- `authored`;
- `contributed_to`;
- `invented`;
- `invested_in`;
- `spoke_at`;
- `colleague_of` as a derived relation only.

Symmetric or derived relations must not be independently extracted when they can be computed from
grounded base claims.

### 10.3 Relationship grounding

A path is grounded only if every hop has active evidence. Derived relations carry the underlying
claim IDs and citations that justify the derivation.

Graph traversal remains bounded by:

- maximum depth;
- maximum degree per node;
- relation allowlist for the query intent;
- maximum paths;
- tenant and visibility scope.

### 10.4 Identity resolution

Strong IDs are namespace-qualified:

```text
github:login
orcid:id
linkedin:public-id
wikidata:id
domain:example.com
cik:number
companies_house:number
```

Name-only resolution creates a mention or candidate link. It does not merge a person. Candidate
links become canonical only after a second independent key or an evidence-backed resolution.

## 11. Evidence Ingestion and Freshness

### 11.1 Connector contract

Every connector must produce normalized source observations with:

- source family;
- canonical URL or stable source ID;
- retrieval timestamp;
- source timestamp when available;
- raw snapshot reference;
- parsed document and blocks;
- candidate entity links;
- extraction status;
- error status.

Connector-specific behavior must not leak into search routes.

### 11.2 Extraction contract

Extraction is schema-driven and versioned. The model owns semantic normalization; code owns:

- schema validation;
- identity keys;
- dates and structural fields;
- numeric normalization;
- citation resolution;
- projection;
- idempotency.

An extraction batch that fails validation writes nothing for that batch and is retried through the
worker. Partial malformed envelopes are not projected.

### 11.3 Evidence portfolio

Each person and company dossier should expose evidence by claim axis rather than one headline tier:

```text
Affiliation: company X — employer-stated, 2024; corroborated by artifact
Capability: distributed systems — artifact-backed, 3 repos, 1 talk
Seniority: staff — self-stated, uncorroborated
Freshness: newest supporting artifact 2025-11
Gaps: no independent current-employer evidence
```

The absence of public artifacts must remain a visible coverage gap, not a negative capability signal.

### 11.4 Liveness

Jobs require a liveness state:

- active;
- recently checked;
- possibly stale;
- closed;
- unavailable;
- unknown.

Closed jobs are excluded from open-job search by default. The result explains whether exclusion came
from a verified source status or stale-data policy.

## 12. Private Candidate Context and Career Workflow

### 12.1 Private context object

```json
{
  "context_id": "private-context:...",
  "owner_id": "user:...",
  "resume_evidence": [],
  "target_roles": [],
  "authorized_regions": [],
  "needs_sponsorship": null,
  "compensation": {},
  "work_modes": [],
  "culture_preferences": [],
  "company_preferences": [],
  "exclusions": [],
  "updated_at": "..."
}
```

Private context has its own evidence boundary. A résumé assertion is not automatically a public
claim, and a public claim is not automatically a résumé assertion.

### 12.2 Two-stage job fit

Job search remains two-stage:

1. **Public discovery:** retrieve open roles using the public search contract.
2. **Private fit:** evaluate the discovered roles against the user's private context.

The response shows both layers separately. A high public relevance result with poor private fit is
still useful and must not disappear without explanation.

### 12.3 Application workflow boundary

Roster may generate:

- requirement-to-proof maps;
- evidence-grounded outreach drafts;
- interview preparation prompts;
- exportable job dossiers.

Roster does not submit applications or mutate the user's source résumé without explicit confirmation.
Career Ops can consume the export and own local reports, PDFs, applications, interviews, and outcomes.

## 13. Maps, Dossiers, and Review

### 13.1 Search artifact

A saved map stores:

- original brief;
- canonical SearchContext;
- canonical SearchContract;
- contract digest;
- row snapshot;
- evidence packet references;
- coverage snapshot;
- source freshness snapshot;
- review state;
- revision history.

Reopening a map shows the saved snapshot. Refreshing creates a new revision; it does not mutate the
historical artifact.

### 13.2 Review states

Review states are user-owned:

```text
unreviewed
shortlist
maybe
needs_more_evidence
not_relevant
```

They are not training labels about a person's quality unless explicitly included in an evaluation
dataset with reviewer identity, task, and consent.

### 13.3 Dossier generation

Dossiers are generated from evidence packets, not unconstrained web research. Every material
sentence must reference an existing evidence ID. Uncited sentences are dropped or labeled as an
open question.

## 14. Feedback and Outcome Events

### 14.1 Feedback types

Feedback can edit the search contract without altering the public corpus:

- more like this;
- less like this;
- wrong seniority;
- wrong domain;
- wrong company target;
- evidence too weak;
- location mismatch;
- role mismatch.

The mapping from feedback to contract edits is code-owned and explainable. An LLM may phrase a
summary of the diff but may not invent the edit.

### 14.2 Outcome events

Private workflow clients may emit:

```text
search_shown
saved
shortlisted
contacted
applied
responded
interviewed
rejected
offer
accepted
```

Events are used for:

- personal workflow views;
- aggregate ranking evaluation after privacy review;
- calibration of search contracts;
- source and freshness diagnostics.

Events never rewrite public claims.

## 15. Integration Boundary for Career Ops

Roster exposes an evidence-preserving export, either JSON or Markdown generated from JSON.

Minimum export shape:

```json
{
  "export_version": 1,
  "brief": "...",
  "contract": {},
  "coverage": {},
  "items": [
    {
      "entity_id": "job:123",
      "company_id": "company:stripe",
      "canonical_url": "https://...",
      "title": "...",
      "status": "active",
      "public_match": {},
      "requirements": [],
      "evidence": [],
      "freshness": {},
      "unknowns": []
    }
  ]
}
```

Career Ops may turn this into a local report, CV tailoring task, or application tracker entry. It
must preserve Roster IDs and citations. Generated candidate material must remain private and must
not be ingested as public evidence without explicit provenance.

## 16. Migration Plan

### Track A: Search convergence

Deliverables:

1. Make `SearchContext`, `SearchContract`, `SearchRow`, and `SearchResponse` canonical.
2. Route people search through the evaluator.
3. Route jobs search through the evaluator.
4. Route résumé matching through public discovery plus private fit.
5. Route Q&A population answers through the same evaluator.
6. Remove or disable duplicate route-specific rankers after parity tests.
7. Make all saved maps store the canonical contract.

Exit criteria:

- identical contract and corpus state produce identical rows regardless of entry route;
- hard filters and scope have one test suite;
- legacy paths are compatibility adapters only;
- no result contains a route-specific score field with undocumented semantics.

### Track B: Entity and evidence graph

Deliverables:

1. Add first-class organization, institution, job, and artifact entities.
2. Complete professional predicate registry.
3. Finish person-as-subject ingestion.
4. Carry temporal fields from extraction through claims and paths.
5. Harden strong-ID identity resolution.
6. Add structural-record evidence with stable synthetic citations.
7. Expose bounded grounded connection queries.

Exit criteria:

- no person merge occurs from name alone;
- every active edge has active evidence;
- temporal conflicts are visible;
- a path is rejected when any hop lacks evidence.

### Track C: Ingestion and freshness

Deliverables:

1. Normalize connector output to source observations.
2. Add source snapshots and idempotent ingestion keys.
3. Version extraction envelopes and facet schemas.
4. Add job liveness and current-affiliation freshness policies.
5. Build shortlist-first artifact enrichment.
6. Track connector health and coverage per source family.

Exit criteria:

- every result can report its source family and observation date;
- re-running an unchanged source is idempotent;
- failed extraction batches do not partially project;
- coverage statements include source families and known gaps.

### Track D: Private context and workflow

Deliverables:

1. Add isolated private context storage.
2. Separate public job discovery from résumé fit.
3. Add requirement-to-proof maps.
4. Add evidence-grounded dossier and outreach preparation.
5. Add map revisions and review feedback.
6. Add export for Career Ops and other clients.

Exit criteria:

- private context cannot appear in public corpus queries;
- public relevance and private fit are separately displayed;
- review edits produce explainable contract diffs;
- exports preserve stable IDs and evidence refs.

### Track E: Evaluation and operations

Deliverables:

1. Build a held-out gold set for jobs and talent.
2. Add adversarial cases for wrong-company attribution, stale affiliation, scope leakage, unknown
   geography, unsupported seniority, and embedding failure.
3. Measure recall, precision, NDCG/MRR where judgments exist, evidence coverage, abstention, latency,
   and model cost.
4. Add route-parity tests during migration.
5. Add production diagnostics for contract, legs, provider state, degradation, and counts.
6. Add projected-cost checks for expensive ingestion, enrichment, and refresh operations.

Exit criteria:

- every material search change has a regression case;
- route parity is tested before legacy deletion;
- ranking quality is reported separately from evidence quality;
- spending runs have a projected budget and bounded tranche.

## 17. Evaluation Framework

### 17.1 Search quality

For each gold query, record:

- relevant IDs;
- required exclusions;
- acceptable near matches;
- expected hard constraints;
- expected preference behavior;
- expected coverage statement;
- expected evidence types.

Report:

- recall at K;
- precision at K;
- MRR or NDCG where graded labels exist;
- hard-filter violation count;
- scope-leakage count;
- duplicate-identity count;
- unexplained-row count.

### 17.2 Evidence quality

Report:

- percentage of material fields with evidence refs;
- span verification rate;
- wrong-subject rate;
- wrong-evidence-kind rate;
- stale-claim disclosure rate;
- unsupported inference rate;
- corroboration independence rate.

### 17.3 Operational quality

Report:

- p50/p95 search latency;
- count-query latency;
- provider failure rate;
- degraded-result rate;
- ingestion freshness lag;
- cost per compiled query;
- cost per extracted entity;
- refresh cost per map;
- queue age and retry rate.

### 17.4 Human workflow quality

Report separately:

- map save/reopen fidelity;
- review completion;
- evidence inspection;
- contract revision clarity;
- export integrity;
- application or interview outcome signals where consented.

No aggregate workflow metric may be presented as evidence that a person is qualified.

## 18. Security and Privacy

1. Tenant and workspace filters execute before any corpus row is returned.
2. Private context is encrypted or access-controlled according to the application data policy.
3. Private résumé text is never sent to public search providers as an unscoped query.
4. Exports contain only the selected rows and evidence allowed by the map owner.
5. Logs redact résumé text, contact details, tokens, and private notes.
6. Reviewers are identified by account or explicit share-session identity when feedback is retained.
7. Public source text is treated as untrusted input and never as executable instructions.
8. Connector redirects, host validation, and source canonicalization are enforced before ingestion.

## 19. Rollout and Compatibility

The migration is additive and feature-flagged.

### Phase 0: Contract shadowing

- Compile every existing search request into a canonical contract.
- Continue using legacy execution.
- Record contract, route, result IDs, and diagnostics.
- Compare legacy and evaluator outputs offline.

### Phase 1: Evaluator behind read-only comparison

- Run both paths for sampled requests.
- Do not expose evaluator rows yet.
- Investigate differences in scope, hard constraints, identity, and ranking.

### Phase 2: Surface-by-surface cutover

Cut over in this order:

1. people population search;
2. jobs search;
3. saved map refresh;
4. résumé-to-job public discovery;
5. Q&A population answers;
6. graph-assisted search.

### Phase 3: Legacy deletion

Delete legacy scorers only after:

- route-parity tests pass;
- golden-set quality does not regress beyond the agreed threshold;
- fallback behavior is verified;
- production diagnostics are available;
- saved-map snapshots remain readable.

## 20. Architectural Decisions

### Decision 1: Do not merge Career Ops into Roster

The products have different ownership, privacy, and deployment models. Integrate through a stable
export and optional client adapter.

### Decision 2: Do not use one universal score

The product must show relevance, evidence, freshness, coverage, and private fit separately.

### Decision 3: One evaluator, many surfaces

The evaluator is the platform primitive. A new search dimension is a schema, extraction, and weight
change, not a new route-specific scorer.

### Decision 4: Claims are the graph authority

Relationships are grounded claims. A second relationship store would create conflicting truth.

### Decision 5: Search is closed-world by default

Corpus search returns what is grounded in the indexed corpus. Web retrieval may enrich or identify a
gap, but it must be labeled and must not silently masquerade as corpus coverage.

### Decision 6: Evidence is claim-specific

An entity does not have one confidence value. Each material claim axis has its own support, freshness,
and contradiction state.

## 21. Definition of Done for AI Search v2

AI Search v2 is ready for general use when all of the following are true:

- jobs, talent, companies, maps, résumé discovery, and Q&A share one validated contract/evaluator;
- no route-specific ranker changes row semantics;
- every result states why it surfaced;
- every material claim has evidence or an explicit gap;
- scope and tenant isolation are enforced before ranking;
- unknown values are visible and never treated as positive evidence;
- stale jobs and affiliations are labeled;
- embedding/provider failures produce deterministic labeled degradation;
- public relevance and private fit are separate;
- identity resolution has adversarial namesake tests;
- graph paths are bounded and fully grounded;
- saved maps preserve contracts, snapshots, evidence, coverage, and revisions;
- Career Ops-compatible exports preserve IDs and citations;
- gold evaluations cover both jobs and talent;
- latency, cost, freshness, and evidence metrics are observable;
- no automated hiring decision is emitted.

This is the platform boundary: Roster becomes a principled evidence search system, while specialized
clients remain free to build private workflows on top of stable, grounded results.
