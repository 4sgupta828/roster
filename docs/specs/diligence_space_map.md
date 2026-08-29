# Roster — VC-Diligence Space/Thesis Map (design spec)

Status: DRAFT for panel review. NO code until reviewed. One verified step at a time after.
Wedge: VC diligence (the workflow where wrong = expensive, so ChatGPT's unverifiable output is a
liability, not an asset). This is the FIRST diligence artifact. See [[roster-strategic-reframe]].

## 1. The artifact (contract)

INPUT: a space / thesis question — "map the AI-SRE startup landscape and where the durable advantage
lies", "who's building text-to-video and who wins".

OUTPUT (structured, grounded, current — an IC-memo-grade market map):
1. SEGMENTS — how the space breaks down (the frame a partner would use).
2. PLAYERS TABLE — per segment, the real companies; each a row with grounded, SOURCED, DATED columns:
   what they do · funding/stage · traction signal · team · differentiation/moat. Every cell clickable to
   its verbatim source; unverified cells MARKED AS GAPS ("not collected"), never fabricated.
3. THESIS READ — a grounded "where the durable advantage is / who's positioned / what's still contested"
   synthesis, as LABELED inference over the cited table (never a new fact).
4. COVERAGE HONESTY — "found N players; roster completeness estimate; these cells are thin; as-of dates."
   For a VC this is gold and the one thing ChatGPT never says.

WIN CONDITION vs ChatGPT: current (recent entrants + latest rounds, no cutoff), fully sourced (click any
cell), structured as a real comparison table, honest about its own gaps. ChatGPT gives a fluent landscape
from memory — stale, unverifiable, silently incomplete — unusable in an IC memo.

## 2. What EXISTS (build on — verified via code survey, file:line)

- CROSSVIEWS (flag ROSTER_CROSSVIEWS): grounded dynamic tables. `apps/api/crossviews.py::build_grid`,
  `crossviews_agent.py::crossview_turn` (goal-first agent + SERVER re-validation gate: LLM proposes
  columns only from the graph's covered-predicate catalog, code drops anything uncovered). Rows ∈
  {company,person,category}; every CELL = {collected, value, citations[{quote,document_id,authority_tier}]}
  or not_collected. **Rows read the ALREADY-materialized claim graph (`store.population_claims`) — NO
  on-demand enumeration.**
- POPULATION MAP (flag ROSTER_STARTUP_POPULATION): `/research/population` →
  `population_route.py::answer_from_population` → `landscape_map.py::build_market_map` → compose + citation
  gate + derive. **Ships an explicit honesty statement** ("only the grounded population materialized into
  the graph as of ingest, NOT all startups"). `operates_in_category` is the placement axis. This is ~70%
  of the artifact already.
- CLAIM-GRAPH (`claimgraph.py::ClaimGraphStore`, `ROSTER_CORPUS_DSN`): BITEMPORAL grounded store —
  `rs_claim` (valid_from/observed_at/ingested_at/superseded_at) + `rs_claim_evidence` (verbatim
  span-verified quote per claim). Diligence predicates exist (`claim_predicates.py`): has_founder,
  has_investor, raised_funding, headcount, business_model, moat_claim, traction_metric, customer_evidence,
  risk_factor, go_to_market. Entity page `GET /entity/{id}` (read-only, cited).
- DEEP-COMPANY READER (flag ROSTER_DEEP_COMPANY_READER): `deep_company.py::retrieve_deep_company` — resolves
  a company's own domain + reads facets (founders_team, product, technology, pricing, customers,
  funding_investors_valuation, competitors_traction) → span-grounded BlockHits → extraction → claim graph.
- DISCOVERY (flag ROSTER_DISCOVERY): `/discover` → `aggregate_entities` — "which companies work on X" but
  attributes ONLY EDGAR + GitHub (thin roster).
- SPAN-GATE (`provenance.py::BlockSpanVerifier`): every claim verbatim-quote grounded; grounded reads
  exclude claims with no active evidence. "Every emitted cell is grounded" is TRUE by construction.
- CURRENCY: ROSTER_PULSE (source-level change ledger + demotion) + ROSTER_FRESHNESS_RANKING (answer-level
  as_of/stale_warning). Per-CELL "as of" data is in `rs_claim` but NOT rendered anywhere yet.

## 3. The crux — COVERAGE (the moat-critical hard part, and the ONLY real gap)

The rendering is solved; the graph is only as complete as ingestion. The gaps (do NOT assume otherwise):
- NO on-demand enumerator for "all real players in space X". Roster = EDGAR/GitHub-attributable ∪
  already-extracted `operates_in_category` members.
- NO Crunchbase/PitchBook/Dealroom connector; funding = EDGAR Form D (US-only) + deep-company press reads.
- NO per-cell as-of rendering.

So the product is a COVERAGE + INGESTION problem. The design must make coverage a first-class, HONEST
output — completeness is never claimed, it's measured and disclosed.

## 4. The design — a grounded coverage loop (parametric PRIOR, retrieval-grounded)

Parametric knowledge is the SEARCH PRIOR (what to look for), never the product (what we assert). The output
is the grounded population map + an honest coverage report.

1. SEED (prior): the model proposes candidate SEGMENTS + candidate PLAYERS for the space (labeled a
   hypothesis, never emitted as fact). UNION with `/discover` (EDGAR+GitHub) + a bounded open-web roster
   search. This is a candidate SET to go verify — its size/quality is disclosed, not trusted.
2. GROUND (the ingestion loop): for each candidate player not already grounded, run the deep-company reader
   → extraction → claim graph (funding/team/traction/moat facts, each span-verified). Bounded + budgeted +
   idempotent (skip players already fresh in the graph). A candidate that can't be grounded is dropped from
   the roster and COUNTED as an unverified-candidate gap — never listed unsourced.
3. RENDER: `build_market_map` / crossviews over the now-populated graph → segments → players table with the
   diligence columns, every cell cited or not_collected.
4. THESIS: a grounded synthesis over the CITED table only (labeled inference / derive) — "who's positioned,
   where the durable advantage is, what's contested" — never a new uncited fact.
5. COVERAGE HONESTY: report {seeded candidates, grounded players, roster-completeness estimate, thinnest
   dimensions, per-cell as-of range}.

## 5. Enforcement (the moat, made VISIBLE)

- PROVENANCE: every cell span-verified (existing gate). Uncited → not_collected, never faked.
- CURRENCY: surface per-cell as-of from `rs_claim` (NEW render) + freshness/pulse. A stale cell is labeled.
- COVERAGE: completeness measured + disclosed. "Silent incompleteness" is the failure mode we refuse.
- Make the difference from ChatGPT UNMISSABLE: show sources, show dates, show gaps.

## 6. What's NEW to build (only the gaps)

A. Coverage loop SEED+GROUND: on-demand space→roster enumeration feeding the graph (§4.1-4.2). Biggest.
B. Per-cell as-of rendering (schema data exists; surface it in crossviews/entity/map).
C. Thesis synthesis over the grounded table (labeled inference).
D. Coverage-honesty report object + its render.
E. (defer) funding-signal depth (Form D + press is thin) — Crunchbase-style is recommend-only for now.

## 7. Eval — what "good" MEANS (held-out, before trusting it)

Adversarial, per the failure modes:
- COMPLETENESS: for a known space with a curated gold roster (held out), what fraction of real players did
  the coverage loop surface + ground? (The core metric — this is where we beat/lose ChatGPT.)
- PROVENANCE: 100% of emitted cells carry an active verbatim citation (automatic via the gate — regression
  guard).
- CURRENCY: each grounded cell carries an as-of; median recency vs the space's real pace.
- GAP-HONESTY: a deliberately sparse space must yield a SMALL map + an explicit thin-coverage report — NOT
  a padded/hallucinated one (the anti-ChatGPT test).
- THESIS: grounded (every load-bearing claim cites the table), non-obvious, and it does NOT assert beyond
  the cells (adversarial verify: try to find a thesis sentence with no cell support → must be 0).
Record model/prompt/graph-snapshot/git-SHA per run (Rule 11).

## 8. Build phases (deliberate; each flag-gated, OFF byte-identical, verified before next)

- P0 — BASELINE ON EXISTING GRAPH: wire population/crossviews to render the full artifact shape (segments →
  table → coverage honesty) for a space THAT'S ALREADY WELL-COVERED in the graph. No new coverage. Proves
  the artifact end-to-end + gives an honest floor. (Mostly assembly of existing parts.)
- P1 — COVERAGE LOOP (§4.1-4.2): seed (prior + discovery + web) → ground (deep-company → extraction) →
  graph, bounded/budgeted/idempotent. The core new capability.
- P2 — CURRENCY + THESIS + COVERAGE REPORT (§6 B,C,D): per-cell as-of, grounded thesis, honesty object.
- P3 — funding-signal depth (defer / recommend-only).
Flag: ROSTER_DILIGENCE_MAP (new), default off, OFF byte-identical. Credits: the coverage loop is the
expensive part (deep reads + extraction per player) — bound it hard + cache in the graph ([[noesis-credit-discipline]]).

## 9. Open questions for the panel

Q1. Is P0-on-existing-graph the right honest floor, or does the coverage loop (P1) have to come first for
    the artifact to be non-trivially better than today's `/research/population`?
Q2. Coverage-loop SEED: how much to trust the model's candidate roster vs discovery vs web — and how to
    MEASURE roster completeness without a gold set (the "we don't know what we don't know" problem)?
Q3. Grounding the roster is deep-read-per-player expensive. Right bound / caching / incremental strategy so
    a space map is affordable and gets better over time (graph accretes)?
Q4. Funding is the thinnest signal (no Crunchbase). Is a US-only/Form-D+press funding column honest enough
    for an IC, or does thin-funding make the artifact untrustworthy for the wedge?
Q5. Biggest risk: the coverage loop surfaces a plausible-but-incomplete roster and the map LOOKS
    authoritative → a VC over-trusts it. How does the honesty report make incompleteness impossible to miss?
