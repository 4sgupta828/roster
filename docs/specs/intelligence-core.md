# ROSTER_INTELLIGENCE_CORE (Phase 1) — adversarial hypothesis-driven retrieval + grounded synthesis

## Contract (Rule 1)
When ON and the question is eligible, the model owns the INQUIRY — it drafts competing HYPOTHESES and
a short analytical FRAME, retrieval tests each hypothesis with FOR-and-AGAINST searches (the one thing
missing today: disconfirmation), facts stay retrieval-authored (span-gate untouched), and the answer
is composed via deep-synthesis with reasoning labeled ([[R]]/derive) and a final cross-family grounding
audit. OFF → byte-identical. Panel-designed (Codex + Gemini + code-grounded subagent, unanimous).

**Why:** roster already has ~9 of the 15 "intelligence" principles (deep-synthesis = thesis/mechanism/
second-order; authority-basis = evidence ladder; derive/[[R]] = Observed→Inferred→Forecast + cruxes;
reasoning-read = calibrated confidence). The one genuinely-missing, high-ROI piece is ADVERSARIAL,
HYPOTHESIS-DRIVEN retrieval — nothing searches for evidence AGAINST a hypothesis. Phase-1 adds exactly
that, reliably, on the existing plumbing. The model owns hypotheses + search agenda; retrieval + the
span-gate own the facts.

## The hard lessons this design encodes (from the parametric post-mortem)
- **Reliability:** the parametric draft flaked because a single `emit` call had to both reason (free
  text) AND enumerate a NESTED object-list → the model filled the prose field and returned `claims=[]`
  (under-population, NOT truncation — a `max_tokens` cutoff RAISES). ⇒ NEVER emit a nested object-list.
  Use a FLAT / LINE-DELIMITED string payload parsed by code (the shape `_Scaffold` already uses reliably).
- **Do NOT inject the model's raw reasoning as the compose spine** (as the current parametric compose
  does at react.py `_PARAMETRIC_ADDENDUM`) — re-base reasoning through `derive` (gated, labeled), or
  present it as "analytical frame tested by retrieval", never as fact.
- **Keep the agentic loop** — add hypothesis legs ON TOP; do not set max_steps=0 (that hurt recall).
- **The prose-audit is hard-token-only** — it misses a laundered qualitative mechanism/entity. The
  cross-family grounding gate (factra pattern) is the mandatory backstop.

## Invariants
- span-gate (`provenance.py::BlockSpanVerifier`) untouched — facts only enter via a verbatim quote.
- Hypotheses/frame/mechanisms are INFERENCE — labeled [[R]]/derive, never asserted as fact.
- Rule 18 (LLM owns hypotheses/meaning; code owns parsing + gates), Rule 20 (flag OFF, byte-identical),
  credit discipline (bounded: +1 draft call + ≤6 targeted searches; charge the REQUEST budget, not a
  hidden fresh BudgetState).

## Tasks

### T1 — Reliable hypotheses draft + flag wiring + routing
- NEW `packages/kernel/roster_kernel/research/intelligence_draft.py`:
  `async def draft_intelligence(question, llm, prompt, *, budget) -> IntelligenceDraft | None`.
  `IntelligenceDraft` = a FLAT schema: `frame: str = ""` (1-2 sentence analytical frame / world-model,
  prose) + `hypotheses_text: str = ""` (a LINE PROTOCOL — one hypothesis per line:
  `Hn | claim | for_query | against_query | falsifier`). NO nested object-list. Provide a code parser
  `parse_hypotheses(hypotheses_text) -> list[Hypothesis]` (a dataclass) that splits on lines/`|`,
  tolerant of missing trailing fields; keep 2-3 well-formed lines. Fail-safe: llm None / blank prompt /
  error / <2 parsed hypotheses → None (caller falls back to today's retrieval-led path).
- Vertical `PRIOR`-style prompt `INTELLIGENCE_DRAFT_PROMPT` (a new vertical file or reasoned.py):
  instruct the model to (a) state a 1-2 sentence FRAME, and (b) emit 2-3 COMPETING HYPOTHESES, one per
  line in EXACTLY the pipe format `H1 | <claim> | <for-search-query> | <against-search-query> |
  <falsifier: the observation that would disprove H1>`. Emphasize: hypotheses must be genuinely
  competing answers; the against-query must seek DISCONFIRMING evidence. Expose via manifest slot
  `intelligence_draft_prompt`.
- Flag: `apps/api/app.py` `intelligence_core_enabled()` (ROSTER_INTELLIGENCE_CORE) → ResearchService →
  run_react (mirror `parametric_led` wiring). Charge the request budget (do NOT create a fresh
  BudgetState like the parametric draft did — thread the real budget or reserve).
- Routing (`runtime/research.py::ask_reasoned`): same eligibility as parametric (stance=established +
  kind∈{understanding,management} + subject!=specific_entity). When eligible + flag: draft, parse; if
  >=2 hypotheses, thread `hypotheses=<list>` + `intelligence_frame=<frame>` into run_react (declare
  inert params in run_react for T1; T2/T3 consume). Empty/degenerate → fall back (byte-identical).
- Tests: parser (line protocol, tolerant, <2 → None-ish); draft fail-safe; flag→service; routing predicate.

### T2 — Adversarial FOR/AGAINST retrieval (on top of the loop)
- `react.py`: when `hypotheses` present, BEFORE or alongside the agentic loop, add bounded retrieval
  legs — for each hypothesis (cap 3): a FOR-query leg and an AGAINST-query leg (`k=4`, no query
  variants, no extra open-web expansion), reusing the existing contract-leg / late-merge retrieval
  pattern (the source.search + aux web leg + screen + `atoms.add_hits`). Do NOT skip the agentic loop
  (keep recall) — these are ADDITIVE legs whose atoms join the pool. Emit progress + diag
  {hypotheses, for_hits, against_hits}. All facts still pass claims_first + span-gate + binding +
  authority — unchanged.
- Tests: for/against legs fire per hypothesis (capped); OFF (no hypotheses) → no legs, byte-identical.

### T3 — Compose: hypotheses as analytical frame, reasoning through derive, cruxes
- `react.py` compose: when `hypotheses`/`intelligence_frame` present, append an addendum that structures
  the answer around the COMPETING HYPOTHESES + the frame — but as "an analytical frame TESTED by the
  evidence", NOT as facts. Every FACT cites [n]; the model's synthesis is labeled [[R]] and, where it's
  a real inference over findings, flows through the existing derive-weave (deep-synthesis already runs
  derive before compose). Do NOT inject raw reasoning as the fact-spine.
- Cruxes: surface, per the leading hypothesis, the CRUX = the concrete observable (its falsifier) that
  would flip the preferred hypothesis — reuse derive `falsifier` + reasoning-read `what_would_change_this`.
  Render a short "What would change this read" line tied to the hypotheses.
- Reuse deep-synthesis as the composer (it should be on for these kinds); the frame shapes structure.
- Tests: addendum present only when hypotheses present; frame/hypotheses in compose_user; OFF byte-identical.

### T4 — Cross-family grounding gate (MANDATORY de-risk)
- Port factra's `finding_grounding_gate` pattern (`/Users/sgupta/factra/app/research_system/services/
  agents/research_orchestrator/finding_grounding_gate.py`) into roster as a post-compose Layer-1 prose
  audit: a DIFFERENT-model-family judge (reuse the `derive_judge_llm` / a cross-family client) re-reads
  the composed answer + ONLY the verified_claims, and flags any sentence asserting a mechanism, entity,
  date, outcome, or causal claim NOT supported by the verified claims. On a hit: recompose once
  instructing removal/relabel, else demote the offending assertion to labeled [[R]] or drop (fail-safe).
  This upgrades the hard-token-only prose-audit to a SEMANTIC cross-family gate. Gate fires only under
  `intelligence_core` (and reuse for parametric/deep later). Fail-CLOSED: judge unavailable → keep the
  hard-token audit (today's behavior), never weaken.
- Tests: a fake cross-family judge flags an ungrounded mechanism → recompose/demote; judge error →
  falls back to hard-token audit; OFF → not invoked.

### T5 — Held-out eval (gates the flip)
- Frozen set: eligible (established + understanding/management) + a lookup/current control. Metrics OFF
  vs ON: (a) adversarial coverage — the answer surfaces evidence for AND against, resolves tensions;
  (b) depth up; (c) verifiability NOT regressed (every grounded claim span-gated); (d) hallucination /
  laundering NOT up (cross-family gate catches ungrounded mechanisms); (e) cruxes present + specific;
  (f) control routes retrieval-led. Provenance (Rule 11), contamination-held-out (Rule 5). Flip ON only
  after it clears.

## Phase-2 (deferred): a ForecastRead register (scenarios/trajectory + probability BANDS, basis findings,
crux ids — no free numeric probabilities in prose), the capability→adoption ladder, a world-model
decomposition artifact — only after the flat-hypothesis core is proven reliable in prod.

## Biggest risk + de-risk
Ungrounded model reasoning laundered as intelligence. De-risk (ship together): facts retrieval-authored
(span-gate untouched); reasoning through derive (gated + labeled); the cross-family grounding gate (T4)
as the semantic backstop; reliability via the line-protocol draft (never a nested object-list); the
empty-draft guard (fall back byte-identical). Over-build risk: keep Phase-1 to hypotheses+adversarial-
retrieval; defer forecasts/world-model/ladder.
