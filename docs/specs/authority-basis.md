# ROSTER_AUTHORITY_BASIS — base answers on authoritative sources, keep the denoiser's breadth

## Contract (Rule 1)
When ON, an answer's FACTUAL BASIS leans on the highest-authority sources available; opinion/blog/
social (low tier) become supplementary/signal — never the sole basis for a stated fact — while
breadth is preserved (low-tier sources still fill remaining seats + the signal register, never dropped).
OFF → byte-identical. Panel-designed (Codex + Gemini + code-grounded subagent), near-unanimous.

**Repro (A/B gate):** "Is there a market for SaaS companies anymore in the AI era? Give me fresh ideas."
OFF: `burdaprincipalinvestments.com` (a VC blog) supplied 6 of 24 primary-basis citations. ON: the
factual sections are carried by higher-tier hosts; blog/substack/LinkedIn appear only as labeled signal;
AND host-diversity is NOT collapsed toward the whitelist (breadth guard).

**Invariants:** span-gate unchanged (this is basis-selection, not provenance); Rule 18 (tier is a
structural classification; page-role judgment is LLM-owned, fail-safe); Rule 20 (flag default OFF, OFF
byte-identical); keep ROSTER_WEB_OPEN_DENOISE breadth — NEVER re-privilege the 38-domain whitelist.

## Verified mechanics (from the panel)
- Tier ranks (`authority.py::_RANK`): sentiment_signal=1, technical_signal=2, expert_analysis=3,
  analysis=4, verified_structured=5, primary_filing=6; unknown → 0. Social→sentiment_signal(1); an
  UNSTAMPED open-web blog → "" → rank 0. `evidence_ranker` = `authority_policy.rank`.
- Today's ranking (`react.py::_rank_claims_by_relevance` ~498) = `cosine + 0.15*(rank/6)` — BOOST-ONLY,
  never demotes; the boost (≤0.15) can't overcome cosine spread (~0.2–0.5). And it ONLY runs when
  `len(verified_claims) > compose_claim_cap` (~1606/1640/1683) AND `evidence_fitness` is on
  (`_ranker_arg`, ~1213). The SaaS repro had 24 claims < deep cap 48–60 → the ranker NEVER runs.
  ⇒ Enabling ROSTER_EVIDENCE_FITNESS alone is a VERIFIED NO-OP. The fix must be UNCONDITIONAL.
- `_suppress_auth` (~1199, from the answer-contract stance / use-case lens) already turns authority OFF
  for opinion/foresight/wisdom stances — reuse it as the question-type gate.
- Open-web denoise hits are UNSTAMPED (rank 0) at the `_authoritative_subset` / stamp site (~417-421).
- Sentiment-as-signal is prompt-only today (`answer_format.py:20` Market-signal section); nothing routes
  low-tier to it structurally.

## Tasks

### T1 — Flag wiring + UNCONDITIONAL low-basis partition (kernel + app)
- `apps/api/app.py`: `authority_basis_enabled()` (ROSTER_AUTHORITY_BASIS, default OFF, mirror
  `evidence_fitness_enabled`). Pass `authority_basis=authority_basis_enabled()` into ResearchService.
  Echo it in the diag payload for observability.
- `packages/kernel/roster_kernel/runtime/research.py`: ResearchService field `authority_basis: bool=False`;
  pass into `run_react`.
- `packages/kernel/roster_kernel/research/react.py`: `run_react` param `authority_basis: bool=False`.
  Add an UNCONDITIONAL stable partition of the verified claims BEFORE the compose-cap trim (place it
  beside the congruence `kind_mismatch` demote ~1623, which is the precedent — a stable sort that runs
  regardless of pool-vs-cap), gated `if authority_basis and not _suppress_auth and evidence_ranker`:
  ```
  # low-basis (rank<=1: unattributed blog / social) → the BACK, so authoritative tiers fill the
  # compose cap first; cosine order is preserved WITHIN each bucket (stable sort).
  def _tier(vc): return int(evidence_ranker(getattr(vc,"evidence_kind","") or "") or 0)
  result.verified_claims.sort(key=lambda vc: 0 if _tier(vc) >= 2 else 1)  # stable
  ```
  (Two buckets: rank>=2 front, rank<=1 back. Do NOT drop anything — reorder only. Cosine/first-come
  order within a bucket is preserved by Python's stable sort.) Also make the flag part of the
  ranking-trigger conditions so the cap-trim prefers front-bucket claims. OFF (or suppress) → no sort →
  byte-identical.
- Tests: OFF byte-identical claim order; ON with mixed-tier fakes → rank<=1 claims move to the back,
  intra-bucket order preserved; suppress_auth ON → no partition.

### T2 — Compose authority-basis directive (the floor)
- `packages/vertical_roster/roster_vertical/answer_format.py`: add `TECH_AUTHORITY_BASIS_DIRECTIVE`:
  "Ground every FACTUAL sentence in the highest-tier source available. Sources tagged opinion / blog /
  social / unknown-provenance are SUPPLEMENTARY — they may corroborate or add color but are NEVER the
  sole basis for a stated fact; where only such a source supports a point, present it as signal
  ('X argues…', 'coverage suggests…') in the market-signal register, not as an established fact."
- Wire it: expose via manifest (a slot or reuse), and in `react.py` APPEND it to the compose directive
  ONLY when `authority_basis and not _suppress_auth` (same gate as T1). OFF → not appended → byte-identical.
- Tests: directive present in compose_user only when the flag is on and stance not suppressed.

### T3 — Open-web screen provenance tag → real tier (breadth de-risk)
- `packages/kernel/roster_kernel/research/web_quality.py`: extend the screen verdict schema with an
  OPTIONAL coarse role field per kept hit: `provenance ∈ {"official","independent_analysis",
  "expert_opinion","social","aggregator"}` (default "" → today's rank 0). Keep the keep/drop decision
  about relevance+junk (do NOT make it authority-filter). Fail-safe: no tag → "".
- `packages/vertical_roster/roster_vertical/web_quality.py`: extend WEB_QUALITY_PROMPT to ALSO emit the
  role for each KEPT hit (official/self-reported page; reputable independent analysis/press; expert
  opinion/blog; social post; thin aggregator).
- Map role → `source_kind` at the open-web hit-stamp site (`react.py` ~417-421 / where screened hits are
  merged): official→"corp_eng"(tier technical_signal=2) or a suitable stamp; independent_analysis→"news"
  (analysis=4); expert_opinion→"essay"/expert_analysis(3); social→"social"(sentiment_signal=1);
  aggregator→leave "" (rank 0). Confirm the exact source_kind strings that map to those tiers in
  `evidence_kind.py::classify` and use those. This gives a GOOD non-whitelisted substack/company-blog a
  real tier (2–3) so the T1 partition doesn't sink it — the breadth root-fix. Gate the stamping behind
  `authority_basis` so OFF is byte-identical (open-web hits stay unstamped rank-0 as today).
- Tests: a fake screen verdict with provenance stamps the mapped source_kind; OFF → unstamped.

### T4 — Verification / A/B
- Unit tests above green; OFF byte-identical (claim order + no directive + unstamped) confirmed; the 2
  known pre-existing suite failures unchanged, no 3rd.
- A/B (after deploy, flag toggled): the SaaS repro OFF vs ON — assert (a) the VC-blog's share of
  primary-basis citations drops and factual sections are carried by higher-tier hosts, (b) blog/social
  appear only as labeled signal, (c) HOST DIVERSITY is preserved (not collapsed to the whitelist —
  the over-prune guard). Only flip ON in prod after this clears.

## Biggest risk + de-risk
Over-pruning breadth (a good substack and a bad blog both arrive rank-0 from the denoiser). De-risk:
demote-and-RELABEL, never drop (T1 reorders, never removes; low-tier still fills remaining seats + the
signal register); T3's provenance tag gives good open-web sources a real tier so they're not in the
rank<=1 bucket; the `_suppress_auth` stance gate exempts ideation/opinion questions; the A/B host-
diversity assertion is the gate before flipping ON.
