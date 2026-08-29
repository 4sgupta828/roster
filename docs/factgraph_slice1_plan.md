# Roster north-star: grounded temporal claim-graph + decision engine — panel synthesis & Slice-1 build plan

## Verdict (all three panelists, converged)
BUILD IT. The atomic unit becomes a **grounded, temporal claim**
`(subject_entity, predicate, object[value|entity], valid_time, evidence{document_id, block_id, quote,
authority_tier}, confidence)`. Every product surface — startups table, landscape map, investment memo,
competitor set, trajectory — is a query or reasoning-pattern over the accumulated claim graph. Store in
**Postgres, NOT a graph DB** (bitemporal + JSONB + pgvector + GC continuity with `rs_block`; graph DB adds
ops cost before any query shape needs it). Multi-hop = recursive CTEs (the one net-new query pattern).

## What is REUSED (substrate that already exists — verified file:line)
- **Extraction keystone** — `research/claims_first.py`: `extract_claims()` (batched 10/call, cheap model) +
  `entail_claims()` (batched 12/call, tri-state entailment/binding, fail-closed). Domain-free. ~90% of a
  source-agnostic claim extractor. `precision.py` adds the per-cell triple-gate pattern (span + value-in-quote).
- **Grounding gate** — `research/provenance.py` `BlockSpanVerifier.verify(quote, Locator(kind="block_span",
  document_id, ref={block_id}))`: fail-closed, tenant-scoped. Works UNCHANGED for a block-backed claim.
- **Reasoning gate** — `research/reason.py` `derive()` (propose→structural gate→validity judge→label). It is
  **duck-typed** (reads `.text`/`.quote`) and already flag-wired to `verified_claims` — graph facts feed it
  by exposing `.text`/`.quote`, no gate change.
- **Decision-frame scaffold** — `roster_vertical/reasoned.py` already classifies decision questions into
  coverage dimensions (likely_causes/key_decisions/…) as questions-not-conclusions, pre-retrieval.
- **Graph precedent** — kernel `graph/store.py` + `roster_edge_evidence` (span-grounded, corpus-populated,
  GC-coupled, closed relation vocab injected via `manifest.graph_relations`) proves a domain-free grounded
  graph PASSES kernel purity. Retrieval seam extends via `graph_legs` / late-merge WITHOUT touching the
  closed `action: Literal["search","answer"]`.
- **Authority** — `authority.py` `outranks()`/`is_controlling()`; `evidence_kind.classify()`.

## What is genuinely NET-NEW (honest — this is real multi-week work, not wiring)
1. **A typed, bitemporal, per-claim-grounded claim table** — nothing today has `valid_time`/supersession
   (PeopleStore `period` is in the PK = append-only) or `document_id`+`block_id`+`quote` on attributes/edges.
2. **Typed entities + entity resolution from free-text mentions** — nothing resolves corpus mentions to typed
   entities. Slice 1 = strong-ID bootstrap only (domain/slug/CIK/QID/GitHub-org); unresolved → soft
   `__unresolved:*` node; NO probabilistic LLM merge in slice 1.
3. **Conflict resolution as truth-arbitration** — authority currently ranks *retrieval selection*, not truth.
   New resolver emits winner + conflict set; losing claims stay queryable.
4. **A real structured-tool retrieval seam (L2)** — `StructuredTool.query(name,args)->grounded claims`
   (population / entity-state / traversal / trajectory / compare), returning the existing `VerifiedClaim`
   citation shape so compose + `derive()` consume them first-class. (Today the graph contributes retrieval
   *queries only, no facts into compose*.)
5. **Extraction as a bounded corpus-scale offline job** — relevance-gated, immutable-cached by
   `(block_sha, schema_version, prompt/model, lenses)`, dry-run cost projection + hard per-run caps + flag OFF.
6. **A company-owned-website `source_kind`** (home/about/pricing pages classify rank 0 today, below a forum).
7. **GC coupling** — on `rs_block` hard-delete, mark dependent claim-evidence `stale`, exclude from answers
   until re-extracted (Codex's safe variant — never silently keep a claim whose span can't load).

## Disagreements surfaced + my calls
- **Kernel-generic claim table vs vertical-owned.** Gemini + code-subagent: kernel-generic (domain-free,
  vocab injected via manifest slots — the `graph/store.py` precedent). Codex: vertical-owned `rs_*` first,
  promote a generic protocol only when a 2nd vertical proves it (YAGNI). **MY CALL:** kernel-generic *store
  mechanics* (a `roster_claim`/`roster_entity` claim table + span-gate — the shape is already proven domain-free
  in `graph/store.py`, low risk), with ALL vocabulary (entity kinds, predicate registry, decision frames,
  tool schemas) vertical-supplied. Honors Codex's concern by keeping every domain-specific piece vertical-side
  while not reinventing the store per vertical.
- **Replace vs supplement block retrieval.** Gemini: graph must NOT replace `rs_block` hybrid retrieval —
  qualitative intents ("their open-source philosophy") degrade as triples; graph supplements. Codex: graph as
  a first-class structured TOOL, not a mere retrieval hint. **Reconciled (no real conflict): DUAL-TRACK** —
  structured graph queries for enumerable/relational/temporal facts; block hybrid retrieval for qualitative
  nuance; both feed compose.
- **Slice-1 predicate count:** Gemini 3, Codex ~10. **MY CALL: ~6** load-bearing for a real landscape answer.
- **GC mechanism:** Gemini FK-cascade; Codex mark-stale-and-exclude. **MY CALL: Codex's** (safer).
- **Provenance precision (Codex, adopted):** canonical entity IDs/aliases are internal resolution artifacts;
  every USER-VISIBLE fact must cite a quote; the canonical ID is backed by identity claims, not itself a quote.

## SLICE 1 — competitive landscape, end-to-end through all 4 layers (flag OFF, worktree, subagent-driven)
Contract: given "map/cluster the competitive landscape of category C (or company X's competitors)", the answer
is a grounded market map where **every company appears because a claim places it in C**, every cell is a cited
claim or explicit `not_collected`, distinct grounded entities ≫ the 19-finding block-sample baseline, and the
coverage basis names the real population ("companies discovered from source set S for category C as of run R").

Build:
1. **Tables** (kernel-generic, domain-free): `roster_entity` (typed, natural-key, alias sidecar),
   `roster_claim` (subject/predicate/object/valid_from/valid_to/confidence), `roster_claim_evidence`
   (document_id/block_id/quote/quote_hash/source_key/authority_tier/evidence_status),
   `roster_claim_resolution` (winner + conflict set), `roster_predicate` (registry: active/candidate),
   `roster_extraction_run` (audit + cost).
2. **Predicate registry (vertical, ~6):** `operates_in_category`, `offers_product`, `targets_customer`,
   `uses_technology`, `claims_differentiator`, `compared_to`. LLM owns predicate assignment; code validates
   schema/ids/dates/allowed-names/quote-span.
3. **Extractor job** (offline worker, flag OFF): rs_block → relevance-gate → `extract_claims` → span-verify →
   `entail_claims` → strong-ID entity resolve → idempotent upsert. Dry-run cost projection + per-run caps.
4. **L2 structured tools (vertical schemas, kernel-generic seam):** `population(category)`,
   `entity_state(entity)`, `compare(entities, predicates)` → grounded `VerifiedClaim`s.
5. **L3 competitive-landscape decision frame** (vertical): decompose → query predicates → cluster/whitespace/
   direct-vs-substitute → `derive()` conclusions → state "what fact would change the call" (from missing
   dimensions / conflicts). Dual-track: block retrieval still fills qualitative nuance.
6. **Compose:** reuse `TECH_LANDSCAPE_COMPOSE_BLOCK`; cells cite claims; `## Coverage basis` names population.

Held-out eval (frozen BEFORE prompt tuning): 15-25 landscape Qs + a gold population from source-set S.
Pass bars: span-grounding 100%; uncited proper-nouns 0; every entity ≥1 cited claim; every cell cited-or-
`not_collected`; category recall ≥0.80 vs frozen population; distinct cited entities ≫ 19; cost/block capped
(target <$0.002) with dry-run approval; report `N entities / M claims / K blocks` + coverage basis.

Then Slice 2 (investment diligence on one company) and Slice 3 (threat analysis) widen predicates + frames +
the compare/trajectory tools along the SAME rails — no rebuild.
