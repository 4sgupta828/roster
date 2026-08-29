# Roster Claim-Graph — Design Improvement Spec

> Status: PARKED for later. Captures the 3-panel review (Codex + Gemini + code-grounded
> subagent) of the grounded claim-graph's **scalability, flexibility, cross-vertical
> extensibility**, plus the **canonical knowledge-graph** foundation assessment.
> Nothing here is built yet. File:line refs are against `main` as of the review
> (tags `factgraph-1..8`, `diligence-1,2`).

## 0. What exists today (the thing being reviewed)

A grounded, **bitemporal claim graph**, live in prod, flag-gated surfaces:
- `POST /research/population` (breadth: cluster the grounded company population)
- `POST /research/diligence` (depth: single-company brief across 7 dimensions)

Atomic unit = a grounded claim
`(subject_entity, predicate, object[value|entity], valid_time, evidence{document_id,block_id,quote,tier}, confidence)`.

Store (`apps/api/claimgraph.py`), 7 Postgres tables: `rs_entity`, `rs_entity_alias`,
`rs_claim`, `rs_claim_evidence`, `rs_claim_resolution`, `rs_predicate`,
`rs_extraction_run`. Extraction job `apps/api/claim_extract_job.py` (per-block, LLM owns
predicate/object, code owns span+entail+in-set gates). Vocabulary in
`roster_vertical` (`claim_predicates.py`, `diligence_compose.py`, `population_compose.py`).

Live scale at review: rs_block 568k, rs_claim ~512, rs_entity ~245, rs_claim_evidence ~529,
rs_predicate 6 (SLICE1 only), rs_claim_resolution 0. YC is the only source ingested so far.

Design decisions locked earlier: store is app/vertical-owned (Codex YAGNI, not kernel-lifted);
funding = value-string predicate (not event-entity); ER = strong-id + exact-name only (no
fuzzy); stage = inferred label, not a predicate.

---

## 1. Panel verdict (headline)

**The design is SOUND and genuinely extensible in shape. What it needs is additive
HARDENING, not a redesign.** Reads that matter (population/diligence) are bounded +
index-driven and hold at scale. The gaps are: one aggregation query, extraction
throughput, three vertical-coupling seams, an unwired conflict table, a decorative
predicate registry, and a single real migration (tenant PK). All fixes are additive
except the PK change.

---

## 2. SCALABILITY — findings + additive fixes

Target stress: 10M–100M claims / millions of entities+evidence (full YC + EDGAR + news +
GitHub, multi-source).

### Findings (verified in code)
- **Bounded reads are fine.** `population_claims`/`entity_claims` are hard-capped
  (`company_cap=400`, `claims_per_company_cap=40`) and `subject_id = ANY(...)` index-driven.
  The winning-evidence `LATERAL` uses the `UNIQUE(claim_id, block_id, quote_hash)` btree
  (led by `claim_id`) — index range, not seq-scan.
- **`distinct_categories` is the seq-scan risk** (`claimgraph.py:578-594`): uncapped
  `count(DISTINCT subject_id) GROUP BY object_norm` + per-row `EXISTS` into evidence, no
  covering index. Heap-fetches every `operates_in_category` claim at scale. The partial
  "current" index (`:163`, subject-leading) does NOT help a predicate-first aggregate.
- **Extraction is the throughput wall** (`claim_extract_job.py:281` `for b in blocks:`):
  strictly sequential (~25 blk/min). `_select_blocks` (`:166-174`) is `ORDER BY document_id,
  block_id` + `LIMIT` only — **no OFFSET/watermark**, so restarts reprocess from the start;
  writes are row-idempotent (deterministic `claim_id`) but LLM calls are **re-spent**.
- **Cost projection omits entail calls** (`project_cost` `:126-128,131`): caps only outer
  extract calls; `rs_extraction_run.entail_calls` (`:221`) is never written by `finish_run`
  (`:353-357`) either.
- **No partitioning anywhere**; `tenant_id` is only a column/leading index term.
- **Global `entity_id` PK** (`claimgraph.py:111-112`, no tenant in the key): cross-tenant
  global dedup by design → a hard multi-tenant isolation constraint, not just tuning.
- **Sync compose endpoints** (~40–90s) approach gateway limits as populations grow.

### Fixes (all additive except the PK)
- Covering index for `distinct_categories`: `(tenant_id, predicate, object_norm, subject_id)`;
  eventually a materialized "current-claim / category-rollup" view.
- Partial covering index for the winning-evidence join:
  `rs_claim_evidence(claim_id) WHERE evidence_status='active' INCLUDE (authority_tier, retrieved_at)`.
- Parallelize extraction: bounded `asyncio.Semaphore(~10)` + a durable work-queue watermark
  (`FOR UPDATE SKIP LOCKED` over `(tenant_id, document_id, block_id, predicate_set_version)`),
  resumable cursors; count extract+entail+retries in the cost ledger; real-time cost abort.
- Async job/ticket pattern + SSE-resume for compose (reuse the app's long-run pattern).
- **Migration (only non-additive change):** composite PK `(tenant_id, entity_id)` for strict
  tenant isolation.

**Verdict: not 100M-ready as-is; every fix is additive except the tenant-PK migration.**

---

## 3. FLEXIBILITY — findings + fixes

**Flexible SHAPE, rigid WIRING in ~4 spots.**

- **New value predicates = zero code** (extractor renders prompt from the passed predicate
  list, `claim_extract.py`). **New entity-kinds = code change**: `_MINTABLE_ENTITY_KINDS =
  {category,person,investor,company}` is hardcoded (`claim_extract_job.py:60`); an
  out-of-set kind silently falls back to the company path (`:317-323`).
  → Fix: drive from manifest `entity_types`.
- **Conflict resolution UNWIRED**: `rs_claim_resolution` exists (`claimgraph.py:187`) with
  no writer anywhere. Contradictions (headcount 50 vs 100) hash to different `claim_id`s and
  just coexist; winner is chosen at READ time via `ORDER BY ev.authority_tier DESC`.
  → Fix: a resolver keyed by `(tenant, subject, predicate, valid_bucket)`, winner by
  `is_controlling` + authority tier + confidence + recency, **alternates kept visible**;
  read queries prefer the resolution. JSON conflict-ids OK v0; a normalized `rs_claim_conflict`
  table is better at scale. Never silently drop losers.
- **Predicate registry is decorative**: `ensure_schema` seeds `SLICE1_PREDICATES` only
  (`claimgraph.py:270`); the 12 diligence predicates are used only when passed to extraction
  (`claim_extract_job.py:148`); the extractor reads the **Python list**, never `rs_predicate`;
  and `rs_predicate` lacks an `object_entity_kind` column (`:201`).
  → Fix: make the DB registry authoritative — seed ALL active predicates, add columns
  `object_entity_kind`, `conflict_policy`, `dedup_policy`, `dimension`, `version`; extraction
  reads active predicates from the table for a requested predicate-set/version.
- **Funding = value string** (`claim_predicates.py:128-134`, `raised_funding` object_kind=value).
  Not a blocker; evolvable additively to a `funding_round` **event entity** with
  `round_amount`/`round_date`/`round_type`/`has_investor` claims — but that hits the
  entity-kind rigidity above.
- **Extractor over-produces near-duplicate soft claims** (e.g. 7 near-dup `claims_differentiator`
  for one company).
  → Fix: **read-time** semantic dedup/grouping in compose; keep the base graph append-only
  & lossless. Write-time canonicalization ONLY for predicates with an explicit `dedup_policy`
  (false merges are worse than extra alternates).

---

## 4. EXTENSIBILITY (sectors + verticals)

### Sectors within tech (AI / fintech / biotech / climate): WORKS TODAY
No `sector` column in `rs_entity`/`rs_claim` — sector is **facet-only** (`claimgraph.py:120`),
and `sector_profiles` (`manifest.py`) compose orthogonally at answer time. A fintech/biotech
company populates the same tables unchanged. Caveat: the extraction job hardcodes company
subjects (mint `kind="company"`, `claim_extract_job.py:289`) — fine for company-subject
sectors, not for non-company subjects.

### Other verticals (legal / medical / finance): FAILS THE LITMUS TODAY
The "domain-neutral" store is coupled to the tech vertical at **exactly 3 seams**:
1. Imports `roster_vertical.claim_predicates` in `ensure_schema` (`claimgraph.py:270`)
   AND `population_claims` (`:623`).
2. Hardcodes `predicate='operates_in_category'` in SQL (`distinct_categories:585`,
   `population_claims:633`) — a vertical predicate name baked into aggregation logic
   (the strongest coupling).
3. Hardcodes `kind='company'` in reads (`find_company:366`, `population_claims:638,654`) and
   the subject mint (`claim_extract_job.py:289`); plus `_MINTABLE_ENTITY_KINDS` (`:60`).

**The fix is PROVEN infrastructure, not speculation.** The kernel already ships two
domain-neutral stores that inject vocabulary via the manifest:
- `GraphStore(relations=manifest.graph_relations)` — relation vocab constructor-injected,
  validated on write, wired from `apps/api/app.py:210-211`. Zero vertical import.
- `PeopleStore` — `roster_entity` carries `vertical`/`kind` columns; labels caller-supplied.
The claim-graph store's SHAPE even passes the kernel purity banlist
(`tools/check_kernel_invariant.sh` — `company`/`category`/`operates_in_category` are not
banned nouns; the vocabulary `moat_claim`/`competitor` correctly lives in the vertical).
(Nuance from Codex: `PeopleStore` is NOT a perfect purity precedent — it carries medical
defaults in kernel code; hold the claim graph to the stricter "legal reuses it untouched" bar.)

### Kernel-generic design (what a 2nd vertical supplies vs reuses)
- **Move app→kernel (shape is generic):** `ClaimGraphStore` + the 7-table DDL + deterministic
  ids + lifecycle + resolution + generic grounded reads + registry seeding + extraction-job
  skeleton + citation-validation + job/SSE mechanics.
- **New manifest slots:** `claim_predicates`, `claim_entity_kinds`, `claim_dimensions`,
  `claim_compose_prompts`, `claim_subject_resolver`, `claim_placement_predicate` (replaces the
  `operates_in_category` literal), `claim_dedup_policies`, `claim_conflict_policy` + reuse the
  existing `authority_policy`, `evidence_classifier`, `entity_types`, `structured_tools`.
- **Stays vertical (data/policy):** the predicate lists, `object_entity_kind` routing, the
  dimension→predicate map, compose prompts, authority/evidence policy, subject resolver.
- **Legal vertical would supply:** entity kinds `case/statute/regulation/court/party/judge`,
  predicates `cites/overrules/holds/applies_rule`, and reuse the store untouched.
- **Medical vertical would supply:** `condition/intervention/outcome/guideline/trial`,
  predicates `studies/recommends/contraindicated_for/improves`, with a medical authority policy.

### Timing (the one place the panel differed)
- Subagent + Gemini: **decouple the 3 seams NOW (in place under `apps/`), relocate to the
  kernel only when vertical #2 lands** (YAGNI on the MOVE, not the decoupling).
- Codex: **lift the store to the kernel now** — the same lift also fixes the scale +
  observability issues, so it's not pure YAGNI.
- **Synthesis / recommendation: decouple the 3 seams in place now** (prerequisite for both the
  scale fixes and any future vertical, low-risk), **physically relocate to the kernel when a
  second vertical actually arrives.**

---

## 5. Can this be the foundation of a CANONICAL knowledge graph?

**Yes — an unusually strong foundation — because the atomic unit is already the grounded,
temporal, reified statement that canonical KGs converge on. But "canonical" is a higher bar
than "grounded fact store," and the defining piece is currently deferred.**

### Why the foundation is right (already there)
- The claim = a **reified statement with references + time** — Wikidata's "statement +
  reference + qualifiers" model, from the atom up.
- **Per-fact verbatim provenance** (document_id + block_id + span-gated quote + authority
  tier) — *stronger* than Wikidata (references, not span-verified) and Google's KG (opaque).
  An **auditable** canonical KG is a genuine differentiator.
- **Bitemporal + supersession** — canonical KGs handle "the value changed over time" poorly;
  built-in here.
- **Edges are claims** (entity-valued objects) — attributes + relationships unified, both
  grounded and time-scoped.
- **Domain-neutral shape** (§4) — a canonical KG must be domain-general.

### What "canonical" additionally demands (the honest gaps, in order of definingness)
1. **Entity resolution / canonicalization — THE defining piece, and the most deferred.**
   "Canonical" = one node per real-world entity, resolved across every source + mention.
   Today = strong-id + exact-name only, `__unresolved:` soft nodes, NO semantic/fuzzy merge
   → "OpenAI" from a tech article, a Form D, and a news story = three nodes. This is the
   make-or-break layer.
2. **Ontology layer** — predicate definitions with domain/range, type hierarchies, **inverse
   predicates** (`has_founder` ↔ `founder_of`), functional constraints. The registry is flat,
   per-vertical, not authoritative.
3. **Global canonical IDs + external crosswalks** — IDs are source-local (`yc:slug`,
   `person:name`); canonical = stable global IDs (à la Wikidata QIDs) crosswalked to CIK / ROR
   / ORCID / LEI / Wikidata so the graph interoperates instead of being an island.
4. **General graph query / traversal** — arbitrary multi-hop path queries; today's reads are
   bounded/purpose-built (no recursive traversal). A canonical KG needs a real query surface.
5. **Governance + curation** — correction workflows, quality/trust signals, versioned truth.
   Primitives exist (status flips, authority tiers, resolution table); no curation surface.

### Recommendation
**Don't chase "canonical KG" as a goal in itself** (boil-the-ocean). Grow toward it
**demand-driven**: **entity resolution becomes unavoidable the moment a SECOND source ingests**
(Form D vs YC = same company, must merge) — that is the natural forcing function to build the
canonicalization layer properly rather than speculatively. Ontology + global-IDs + query
layers follow when predicate count / interop actually demand them. The grounded-temporal-
statement foundation is exactly what belongs underneath — this is layering, not rebuilding.

**Sequencing implication:** multi-source enrichment (the next SOTA lever) is ALSO the trigger
for the canonicalization layer. Do them together: when you add source #2, build real ER +
conflict resolution (§3) as the first bricks of the canonical layer.

---

## 6. Prioritized backlog

| Bucket | Item | Risk | Trigger |
|---|---|---|---|
| **Quick wins** | Seed ALL predicates into `rs_predicate` (+`object_entity_kind`); make DB registry authoritative | low | now |
| **Quick wins** | Covering indexes (`distinct_categories`; partial active-evidence) | low | now |
| **Quick wins** | Count entail calls in cost projection + ledger | low | now |
| **Decouple** | Remove the 3 vertical-coupling seams (inject predicates / placement-predicate slot / subject entity-kind); manifest-drive `entity_types`/`_MINTABLE_ENTITY_KINDS` | low | now (enables verticals) |
| **Wire** | Conflict resolver → `rs_claim_resolution` (winner + visible alternates) | medium | with source #2 |
| **Wire** | Read-time semantic dedup of near-dup soft claims (in compose) | low-med | when noise hurts |
| **Scale** | Parallelize extraction (bounded concurrency + watermark/offset) | medium | before full-pop scale |
| **Scale** | Async compose endpoints + SSE-resume | medium | as populations grow |
| **Migration** | Composite `(tenant_id, entity_id)` PK (tenant isolation) | needs migration | before multi-tenant |
| **Canonical** | Real entity resolution (LLM-owned semantic merge, strong-id bootstrap, alias authority) | high | with source #2 |
| **Canonical** | Ontology layer (inverse predicates, domain/range, type hierarchy) | med-high | when predicates proliferate |
| **Canonical** | Global canonical IDs + external crosswalks (Wikidata/CIK/ROR/ORCID/LEI) | high | when interop needed |
| **Canonical** | General graph-query/traversal surface (recursive CTEs / graph layer) | high | when multi-hop demanded |
| **Vertical move** | Physically relocate store to `packages/kernel/roster_kernel/claimgraph/` | med | when vertical #2 lands |

## 7. Recommended sequencing
1. **Harden now (quick-wins + decouple)** — low-risk, makes it vertical-ready + fixes registry/indexes.
2. **Multi-source enrichment + canonicalization together** — source #2 forces real ER + conflict
   resolution; build them as the first canonical-layer bricks (highest SOTA value).
3. **Scale (parallelize + async + PK migration)** — when actually scaling the population.
4. **Kernel lift + 2nd vertical** — prove the manifest abstraction with legal/medical.
5. **Full canonical layer (global IDs, ontology, query)** — demand-driven, last.

---
_Panel: Codex (GPT-5.5) + Gemini 3 Pro + code-grounded subagent. Reviewed against `main`
(factgraph-1..8, diligence-1,2). All findings verified file:line by the code-grounded seat._
