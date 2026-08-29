# Spec: the Roster edge model (people + companies + how they connect)

Status: DESIGN AGREED (judge panel: Codex + Gemini + code-grounded subagent, 2026-08-29).
Branch: `edge-model`. Flag: `ROSTER_EDGE_MODEL` (default OFF, Rule 20).

## Goal / contract (Rule 1)
Answer "who is X connected to, and how" / "how is X connected to Y" over a corpus of PUBLIC
professional/company signals, with every edge and every connection PATH grounded (each hop cites
active evidence). Given two resolved entities, `/graph/path?from=&to=` returns the bounded set of
grounded connection paths, each hop carrying `{predicate, subject_id, object_id, valid interval,
claim_id, evidence_kind, citation{document_id, block_id, quote, authority_tier}}`. Invariants that
must not break: Rule 18 (LLM owns meaning, closed relation vocab validated in code), span-grounding
(no ungrounded edge ships), append-only audit, kernel domain-agnostic, additive/forward-only schema.

## Decision (panel-synthesized)
**One authoritative graph = the claim graph** (`apps/api/claimgraph.py`). An entity-object claim IS a
directed typed edge. We extend it; we do NOT build a new store and do NOT run `PeopleStore` as a second
relationship graph (reuse only its `derive_shared_key_edges` SQL *pattern* to GENERATE structural
claims into the claim graph). Verified premises: new predicates need no store change
(`rs_predicate` registry is authoritative); the store is import-clean/domain-neutral; the substring
span-gate lives in the extractor, not the store, so structural (non-corpus) edges are writable as
`rs_claim`+`rs_claim_evidence` with a stable synthetic citation.

### Per-question resolution
1. **Foundation** — extend the claim graph with a professional predicate registry in the vertical:
   base predicates only (`works_at`, `founder_of`, `board_member_of`, `advisor_to`, `educated_at`,
   `authored_work`, `invested_in`); DERIVE symmetric relations, don't extract them.
2. **Structural vs prose** — one store. Structural records → claims with
   `evidence_kind='structural_record'`, evidence citing the registry row via a stable synthetic
   `document_id/block_id` (e.g. `companies_house:<num>#officer:<id>`), and read-side re-verification
   exempts `structural_record`. Prose edges → existing span-gated extractor lane.
3. **Path queries** — bounded `WITH RECURSIVE` over active entity-edge claims, depth ≤ 3–4, mandatory
   per-node degree cap (skip hubs), relation allowlist per intent, seeded by the two endpoints. Uses
   the existing `ix_rs_claim_pred_oent` partial index. A path is grounded iff every hop is a claim
   with active evidence (derived hops carry the underlying claim ids + citations for each side).
4. **Symmetric/derived edges** — compute at READ, never materialize. `colleague_of` = two `works_at`
   claims, same employer, temporal overlap (`max(from) <= min(to)` when bounds exist; else labeled
   `possible_shared_affiliation`). Grounding = the two underlying grounded claims; overlap is
   code-computed structure. Optional materialized read-model later (with `derived_from_claim_ids`).
5. **Person identity** — add strong-id namespaces (`github:`, `orcid:`, `linkedin:`, `wikidata:`;
   companies `domain:`, `cik:`, `companies_house:`, `lei:`, `wikidata:`); PASS `strong_ids` into
   `resolve_entity`; DELETE the global `person:<norm>` merge branch; keep split-first (mention lane /
   per-company) ONLY when no strong id. Add `tenant/kind` to `rs_entity_alias`.
6. **Placement** — PROMOTE the engine to `roster_kernel.claim_graph` (store, schema, evidence
   validation, read contracts). Keep routes (`apps/api`), wiring (`claimgraph_tech.py`), predicate
   registry + prompts (vertical). Blockers to clear: reword `competitor`/`cik` comments
   (`claimgraph.py:6,128,507`) that trip `check_kernel_invariant.sh`; make tech-leaning constructor
   defaults required params. HARDEN store invariants during the move: `add_evidence` should verify
   `quote ⊆ block.text` for `block_span` evidence; `upsert_claim` should validate predicate membership.
7. **Consumption / acceptance** — dedicated flag-gated `GET /graph/path?from=&to=` (+ `/connections?
   entity=`), returning grounded multi-hop paths. That single prod-observable surface is the E2E
   acceptance test. CROSSVIEWS / ReAct evidence-legs consume it later.

## Reality-check (verified) — this is a multi-slice epic, not one change
- Person-as-subject predicates have NO ingestion path (job mints one company subject per document,
  `claim_extract_job.py:331`).
- Temporal edges are schema-only (extractor emits no dates, `claim_extract.py:203-209`).
- Two conflicting person-identity models collide (per-company split vs exact-norm global merge) — a
  namesake-collision bug that must be fixed before a person-centric product is trustworthy.

## Phased slice plan (each slice: flag-gated, TDD, prod-observable, ends verifiable)
- **Slice 1 — Grounded connection PATHS over EXISTING edges. ✅ BUILT (branch `edge-model`).**
  `api/graph_path.py` = a pure, bounded, grounded pathfinder (BFS over an injected `neighbors`
  callable; max_depth/degree_cap/max_paths; simple paths; each hop carries its citation).
  `ClaimGraphStore.neighbors` = bidirectional 1-hop grounded edges (active-evidence INNER-lateral,
  vocabulary-neutral) + `ClaimGraphStore.find_entity` (id/name resolve). Endpoints `GET /graph/path?
  from=&to=` and `GET /connections?entity=`, flag `ROSTER_EDGE_MODEL` (default OFF, echoes `enabled`),
  read-only (never 500s). Verified: 8 unit tests (pathfinder, offline) + 3 integration tests (store
  SQL + e2e find_paths, against a real Postgres `roster_test`) green; kernel guardrails green.
  OUTSTANDING: prod E2E (no roster Railway/prod provisioned yet).
  Deferred to later slices as designed: colleague_of derivation (Slice 3, needs `works_at`), recursive
  SQL at very large scale (current app-level BFS is fine for existing edge volume), name→entity
  cross-source resolution (Slice 2).
- **Slice 2 — Person identity unification.** Strong-id namespaces; plumb `strong_ids` into
  `resolve_entity`; delete global `person:<norm>` merge; `rs_entity_alias` tenant/kind. The
  correctness foundation for people.
- **Slice 3 — Professional predicates + person-as-subject + temporal.** Vertical professional
  predicate registry; person-subject ingestion path; wire `valid_from/valid_to` end to end.
- **Slice 4 — Structural-record edge lane.** `evidence_kind='structural_record'` + synthetic stable
  citation + read-side exemption; a first structural connector (Companies House or co-authorship) via
  the `derive_shared_key_edges` pattern emitting claims.
- **Slice 5 — Kernel promotion + invariant hardening.** Move engine to `roster_kernel.claim_graph`;
  clear comment blockers; make domain defaults required; harden `add_evidence`/`upsert_claim`.

Slice 1 is the current build target.
