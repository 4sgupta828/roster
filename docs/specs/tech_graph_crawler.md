# Roster — Tech Knowledge Graph + Autonomous Crawler (v2, panel-converged)

Status: APPROVED design; build in phases, one verified step at a time. NO autonomy until identity safety
is proven. Panel-converged (Gemini + code-subagent + Codex whole-plan review, 2026-08-27).
See [[roster-strategic-reframe]] (why), [[roster-claim-graph]] (prior graph work — its DATA is discarded).

## 0. Thesis + the moat (sharpened)

Stop competing with ChatGPT on freeform answers (a race roster loses). The moat is **auditable diligence
memory with coverage accounting + change detection** — a persistent, GROUNDED, CURRENT tech world-model
where a VC can inspect *what is known, what is stale, what is missing, and what source backs every claim*.
Not "ChatGPT can't build a graph" (they can add memory) — the wedge is **auditability + coverage honesty +
change detection**, which a plausibility-optimized chat product structurally will not offer. Every diligence
surface (space/thesis map, comparison table, dossier, explorer) is a VIEW over the graph. The engine that
grows it — an autonomous, frontier-guided, GROUNDED crawler — is the foundation. Coverage becomes a
compounding background asset.

**HARD PRODUCT INVARIANT (not a view nicety): coverage honesty.** Every surface states what it knows vs
what it doesn't. A sparse/stale/name-polluted graph that gives a FALSE sense of diligence completeness is
the existential failure mode; the whole design defends against it.

## 1. FRESH graph — discard existing data, keep only the provenance principle

Build a FRESH graph. Do NOT carry over the existing ~120-company graph's DATA — it is name-polluted
(objects soft-minted as canonical `<kind>:<norm>` rows bypassing resolution). Retrofitting is NOT cheaper:
current `entity_id` is a global PK (not tenant-scoped), aliases aren't tenant-scoped, and claims point
directly at object ids — so a cleanup is a surgical rewrite of entity/alias/claim-object refs. For ~120
companies, **re-extraction on a corrected write contract is cheaper than surgical repair.** Keep exactly ONE
thing: the span-gate / provenance PRINCIPLE (every fact = a verbatim, verified quote). The schema tables
(rs_claim/rs_claim_evidence bitemporal, resolution) are sound and reused as SHAPES; the fresh graph is a
clean namespace with the NEW resolution contract (§3) — the data does not migrate.

## 2. Ontology (the value-flow model) — and what's genuinely NEW

NODES: `Company` · `Person` · `Technology` · `Product` · `Investor` · `Market/Segment (ICP)`.
Company = value engine: CONSUMES key People + Tech + Capital → PRODUCES Product/Tech (consumed_by others)
+ Revenue. Value = outputs > inputs.

EDGES (typed, directional, TEMPORAL — bitemporal, latest surfaced, history retained):
`founded_by`, `key_person`, `invested_by`, `consumes_tech`, `produces`, `consumed_by` (reverse closure),
`competes_with`, `operates_in`/`serves`.

DILIGENCE ATTRIBUTES (temporal, latest): stage · funding(amt/round/date) · headcount · revenue/ARR ·
sales/traction · customer traction · GTM · ICP · business_model · moat.

CORRECTION to prior framing (Codex, verified): the tech-flow is NOT ~90% present. `offers_product` and
`uses_technology` are today VALUE predicates, not ENTITY edges, and the vertical mints only company/person/
investor/category nodes — NOT `Product`/`Technology` as real nodes. So the `produces → consumed_by →
consumes_tech` closure (the tech-graph SPINE) is **NET-NEW**: it needs Product/Technology as canonical node
kinds + entity-valued edges, both gated by the resolution contract (§3).

## 3. THE RESOLUTION CONTRACT — "mention first, entity later" (the load-bearing design)

Zero pollution is achievable, but NOT as "resolve every object to a canonical entity" — for a name-only
object you CANNOT guarantee both never-wrong-merge AND never-duplicate-spawn. The contract, three typed
layers, strictly separated:

1. `frontier_candidate` — a thing the model/crawler thinks is worth investigating (may be model-originated,
   UNGROUNDED). NEVER a fact, NEVER read by any view. Feeds the crawl queue only.
2. `unresolved_mention` — an EVIDENCE-BOUND name occurring in a real, span-verified quote (e.g. "backed by
   Sequoia" inside a grounded block). It has provenance but NO canonical identity yet. Lives in its OWN
   lane; views NEVER read it. It can accrete evidence and later be promoted.
3. `rs_entity` (canonical) — a resolved node. A mention is PROMOTED to canonical ONLY when it has: a STRONG
   ID (domain / CIK / GitHub org / handle), OR a same-tenant EXACT-single-identity backed by evidence, OR a
   HIGH-CONFIDENCE adjudicated match (LLM-with-evidence ≥ threshold, fail-safe: on ANY ambiguity DO NOT
   promote — keep as mention). Never promote on a bare name alone.

RULES:
- RESOLVE-BEFORE-CANONICAL: a claim's SUBJECT and OBJECT are canonical `rs_entity` only after promotion;
  until then an object is an `unresolved_mention` ref (evidence retained, identity pending).
- VIEWS READ ONLY canonical grounded entities + grounded value claims. No mention, no candidate, no
  ungrounded row can EVER surface. (This is the physical leak guard — extends today's active-evidence
  INNER-join to also require canonical identity.)
- NEVER WRONG-MERGE (bias to split); a duplicate is recoverable (merge later on new strong evidence), a
  wrong merge corrupts two real entities. Namesakes stay distinct until disambiguated.
- The existing `canonicalize.py::resolve_entity` (fail-safe, never false-merge) is the promotion adjudicator
  — but it must now be applied to OBJECTS too (today extraction bypasses it: soft-mints `<kind>:<norm>` /
  `__unresolved:<norm>` canonical rows directly — the BUG to fix).

LEAK TEST (gating, required before any crawl): seed a FABRICATED entity into the frontier; run a full cycle;
assert it appears in NO canonical `rs_entity` row and NO view — only (at most) as a candidate/mention.

## 4. The crawler (built LAST, only after §3 is proven)

Frontier-guided grounded accretion: SELECT a frontier task (thin node / dangling edge / seed; LLM proposes
what's worth learning, CODE owns the queue + priority `value × staleness / (hops+1)` + budget gate + stop
condition) → FETCH+GROUND → EXTRACT through the §3 resolution gate → write canonical claims + mentions →
edge-follow (deep-first) → repeat, bounded/idempotent/skip-fresh. Autonomy = a bounded on-demand batch
riding the existing queue-drain pattern (`apps/worker` + `gap_queue`), NOT a daemon. An operator-visible
CRAWL LEDGER records every cycle (seeded → grounded → promoted → quarantined → spend).

## 5. What EXISTS vs NET-NEW (panel-verified)

EXISTS + reusable: bitemporal claim/evidence schema + PHYSICAL active-evidence read gate (claimgraph.py);
fail-safe subject resolver (canonicalize.py::resolve_entity); the durable fetch→persist-`rs_block`→extract→
upsert path (multisource_enrich.py::enrich_company, INSERT INTO rs_block) — UNROUTED; extraction with
dry-run + pre-LLM budget aborts (claim_extract_job.py); worker/queue drain loop; graph explorer.
NET-NEW: the 3-layer resolution contract §3 (frontier_candidate / unresolved_mention / canonical) + OBJECT
resolution through resolve_entity (fix the soft-mint bug); Product/Technology as node kinds + the tech-flow
entity edges (§2 spine); per-cell as-of surfacing; source-policy tiers for diligence; the frontier loop +
crawl ledger; held-out recall eval. The deep-company reader is NOT the write path (it feeds answers only);
the crawler uses the enrich_company-style fetch→persist→extract path.

## 6. Views over the graph (reads; built after the graph has depth)

Diligence Space/Thesis Map (segments → grounded players table → thesis → coverage report), crossviews table,
entity dossier, explorer. Each reads ONLY canonical grounded entities + grounded claims, and each renders
the coverage-honesty invariant (§0).

## 7. Eval — "good" (held-out; before trusting)

Zero wrong-merges + zero ungrounded/mention rows in any view (the leak test — hard gate); grounding 100%
span-verified; graph growth per crawl-dollar + edge-CLOSURE forms; coverage recall vs a held-out gold roster;
currency (as-of recency, latest-tracking correct on a moved person); frontier efficiency (value/$). Record
model/prompt/graph-snapshot/git-SHA per run.

## 8. Build phases (SEQUENCING FLIPPED — identity safety FIRST)

- **P0 — THE RESOLUTION GATE (identity safety FIRST, not the write wire).** Implement the §3 3-layer
  contract: the `unresolved_mention` lane + route OBJECT minting (in claim_extract_job + multisource_enrich)
  through `resolve_entity` / promotion rules; extend the view read-gate to require canonical identity. Ship
  the LEAK TEST green (fabricated entity never canonical/never in a view). Pure identity plumbing + tests,
  no crawl, no autonomy. THIS kills the existential risk before any crawler code.
- **P1 — ONE-COMPANY CLEAN ACCRETION.** Route `enrich_company` (fetch → persist rs_block → extract) for ONE
  seed company through the P0 gate; watch clean canonical nodes + mentions accrete in the explorer. Proves
  the write path on the clean contract.
- **P2 — THE TECH-FLOW SPINE.** Add Product/Technology node kinds + `produces`/`consumed_by`/`consumes_tech`
  entity edges (through the resolution gate). Add first-class revenue/ARR + ICP predicates; per-cell as-of.
- **P3 — THE FRONTIER LOOP.** Frontier table + priority + budget + edge-following + crawl ledger, bounded
  on-demand batch. Held-out recall + leak eval.
- **P4 — VIEWS**: diligence map / crossviews / dossier over the now-rich graph, each with the coverage report.
- **P5 — SCALE**: parallelize extraction, async.
Each flag-gated, OFF byte-identical, bounded, verified before next. Crawler behind an operator-bounded
switch, never auto-on. STANDING: [[noesis-credit-discipline]].

## 9. First step (P0) — concrete

1. Fresh graph namespace/tenant (empty).
2. `unresolved_mention` table (evidence-bound, its own lane) + a `frontier_candidate` table (ungrounded).
3. Object resolution: in the extraction + enrichment object path, replace the direct `<kind>:<norm>` /
   `__unresolved:<norm>` soft-mint with: promote-to-canonical iff strong-id / exact-single-with-evidence /
   high-confidence adjudication (via resolve_entity); else write an `unresolved_mention` (keep evidence).
4. View read-gate: require canonical identity (extend the active-evidence INNER-join).
5. LEAK TEST + a promotion unit-test suite (strong-id promotes; ambiguous namesakes DON'T merge, stay
   distinct/mention; fabricated candidate never canonical). All green before P1.
