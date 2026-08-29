# ROSTER_PARAMETRIC_LED — parametric-drafts-reasoning, retrieval-verifies-every-fact

## Contract (Rule 1)
When ON and the question is parametric-eligible, the model's integrated knowledge LEADS the answer's
structure + reasoning; **retrieval VALIDATES every asserted fact** (targeted search → the existing
span-gate + a ruthless binding gate). A fact that grounds becomes a normal cited claim; a fact that
can't be corroborated is **labeled "model asserts — unverified", never presented as grounded fact**;
the model's synthesis/structure stays in the `[[R]]` inference register. OFF → byte-identical (today's
retrieve-first path). Panel-designed (Codex + Gemini + code-grounded subagent, near-unanimous).

**Why:** a frontier model often reasons/structures better from its integrated knowledge than we can by
scouting + denoising the wild (the "top VCs" ChatGPT comparison). But a parametric answer hallucinates
facts confidently. roster's differentiator is TRUST — so the model may LEAD, but every FACT it asserts
must pass the same retrieval-backed gate a retrieved claim passes today. Retrieval is NOT removed — it
is repurposed from primary AUTHOR to directed VERIFIER + freshener + gap-filler.

**Invariants (do not violate):**
- The span-gate (`provenance.py::BlockSpanVerifier`) is UNTOUCHED — the fact wall. A model fact reaches
  `verified_claims` ONLY by finding a verbatim quote in a retrieved block that ENTAILS it.
- No hallucinated fact may be laundered into grounded prose. Unverifiable model facts → a visibly
  labeled register, or dropped — never asserted. This includes correct-but-unverifiable knowledge (the
  accepted cost of the trust guarantee).
- Rule 18 (LLM owns the draft/decomposition/binding judgment; code owns the gate mechanics + labels).
- Rule 20 (flag default OFF, OFF byte-identical). Credit discipline (bounded extra calls).
- Reuse existing machinery; keep the denoise/liveness/authority stack as the verifier.

## The mechanism — decompose → verify → recompose (NOT "validate prose as a blob")
1. **Prior draft.** One strong-model call → a structured `PriorDraft`: an answer OUTLINE (sections/axes,
   structure only) + a list of `AssertedClaim{text, kind: fact|reasoning, needs_freshness, verify_query}`.
   The model separates checkable FACTS (numbers/dates/names/events/attributions) from REASONING/structure.
2. **Verify each FACT.** For each `kind=fact` claim: run its `verify_query` as a TARGETED retrieval; the
   returned atoms run the EXISTING span-gate + the binding/entailment gate (claim_congruence). Outcomes:
   - **grounded** → the quote proves it → promote to a normal cited `VerifiedClaim` (indistinguishable
     from today's findings; no longer "parametric").
   - **contradicted** → drop (or correct to the source + cite).
   - **not found** → an `unverified` register entry (labeled), never a `VerifiedClaim`.
   Fail-CLOSED: if the binding judge is unavailable/ambiguous → `unverified`, never grounded.
3. **Recompose.** The prior OUTLINE shapes structure/ordering; the grounded facts + labeled inferences
   carry the prose; unverified facts appear ONLY in a labeled register. Deep-synthesis is the composer.
   The corrective prose-audit re-runs so no unsupported figure re-enters grounded prose.

## Routing (reuse existing LLM-owned classifiers — no new one)
- Parametric-led ELIGIBLE: `stance="established"` AND `kind ∈ {understanding, management}` AND
  `subject_kind != "specific_entity"` (general). (`answer_contract.py` stance + `runtime/research.py`
  `_Scaffold.kind` + `subject_kind`.)
- Retrieval-led (OFF path, hard): `stance="current"` (stale prior), `kind="lookup"`,
  `subject_kind="specific_entity"` (long-tail diligence), or the flag off. These take today's pipeline
  byte-identical.

## Tasks

### T1 — Types, flag wiring, routing, PriorDraft generation
- `apps/api/app.py`: `parametric_led_enabled()` (ROSTER_PARAMETRIC_LED, default OFF); pass into
  ResearchService; echo resolved value on the API response so the UI can label the register.
- `runtime/research.py`: ResearchService field `parametric_led: bool=False`; in `ask_reasoned`, when the
  flag is on AND the routing predicate holds (stance/kind/subject_kind above), take the parametric path;
  else today's path. Compute the routing from signals already available in the reasoned flow.
- New module `packages/kernel/roster_kernel/research/prior_draft.py`:
  `async def draft_prior(question, llm, prompt, *, budget) -> PriorDraft | None` — one structured call →
  `PriorDraft{outline: str, claims: list[AssertedClaim]}`, `AssertedClaim{text, kind, needs_freshness,
  verify_query}`. Fail-safe → None (caller falls back to today's path). Domain-free (prompt injected).
  The vertical supplies `PRIOR_DRAFT_PROMPT` (a manifest slot `prior_draft_prompt`).
- Tests: flag→service; routing predicate (eligible vs not); draft parsing + fail-safe.

### T2 — The verify loop (reuse span-gate + binding)
- In the parametric path (kernel, likely a function in react.py or a sibling that reuses its retrieval +
  `_apply_answer`/`BlockSpanVerifier` + `claim_congruence`): for each `kind=fact` AssertedClaim, run a
  targeted retrieval with its `verify_query`, then attempt to ground the claim — find a retrieved atom
  whose verbatim quote ENTAILS the claim (span-gate + the ruthless binding judge). Produce:
  `verified_claims` (grounded, cited) and `unverified_priors` (not grounded, labeled). `needs_freshness`
  or `stance=current`-ish claims MUST be retrieval-grounded (never shipped from prior alone).
- The binding judge must be adversarial: reject unless the source EXPLICITLY + UNEQUIVOCALLY proves the
  specific claim (not keyword overlap). Reuse/extend `claim_congruence`. Fail-closed → unverified.
- Bound cost: one draft call + one targeted retrieval + one grounding/binding check per fact claim,
  capped (e.g. top-N fact claims); charge budget.
- Tests: a grounded claim → verified+cited; a claim with only a tangential quote → unverified (binding
  rejects); a fresh/entity claim → forced retrieval; fail-closed on judge error.

### T3 — Compose from verified + labeled unverified register
- New result field `unverified_priors: list` (parity with `derivations`). Compose the answer from the
  prior OUTLINE (structure) + `verified_claims` (grounded prose, cited) + the `[[R]]` reasoning; render
  `unverified_priors` in a SEPARATE, visibly-labeled register (e.g. a "Model's read — not yet verified"
  section), never merged into grounded prose. Reuse the deep-synthesis composer + the CORRECTIVE
  prose-audit (extend it to fire in parametric mode so a qualitative model sentence can't ride in
  ungrounded). Surface `unverified_priors` on the API response for the FE.
- FE: render the unverified register distinctly (muted / "unverified model knowledge" label + tooltip).
- Tests: unverified priors never in verified_claims; the labeled register renders; prose-audit corrective.

### T4 — Held-out eval + verification (gates the flip)
- Frozen set stratified: established/understanding (parametric-eligible) + current/lookup/entity (must
  route retrieval-led). Metrics OFF vs ON:
  - DEPTH up on eligible questions (richness/coverage judge, different family).
  - HALLUCINATION-rate NOT up: on a fact-checkable held-out set, count facts in the GROUNDED stream that
    are false — target ≤ baseline.
  - VERIFIABILITY not regressed: every grounded claim still passes the span-gate (invariant); % of prose
    sentences traceable to a verified finding not lower than OFF.
  - "unsupported prior leaked into grounded prose" = 0.
  - Recency guard: `stance=current` questions ship no prior-only fact.
- Provenance (Rule 11): model/prompt/flag/SHA/question-ids/OFF+ON. Contamination-held-out (Rule 5).
- Flip ON in prod only after the eval clears.

## Biggest risk + de-risk
"Synthetic laundering" — a weak binding step maps a confidently-wrong model fact to a tangential quote,
passing the gate mechanically while violating its spirit; and the prose per-sentence hole. De-risk
(non-negotiable, ship together): (1) ruthless adversarial binding judge, fail-closed to unverified;
(2) the span-gate stays untouched (a fact with no verbatim entailing quote can NEVER be grounded);
(3) the corrective per-sentence prose-audit runs in parametric mode. Runner-up architecture (deferred):
parallel retrieval-first + parametric-draft reconciliation — safer for shadow eval, costlier; use for
eval, not production.
