# Phase-1 Spec — Evaluation harness + Evidence-fitness v1

**Status:** draft for review (no code written yet).
**Origin:** 3-panelist review (Codex GPT-5.5 + Gemini 3 Pro + code-grounded subagent) of the
"verifiable clinical reasoning system" thesis, all verified against the current code.
**Thesis position this serves:** *"OpenEvidence helps find an answer; Noesis helps understand whether
it's justified, applicable, and defensible."* Phase 1 makes "better" **measurable** and turns the
already-present-but-dead evidence hierarchy into a **live, ranked, auditable** signal.

## Why these two, together, first
- **We cannot measure the North Star today.** The medical held-out gold is 2 fixture cases
  (`packages/vertical_medical/noesis_vertical_medical/eval_gold.py`) that no pytest runs; the only real
  evals are prod-hitting scripts (`scripts/eval_*.py`) that cost credits and aren't risk-weighted. Every
  later claim ("this reasoning feature helps") is unfalsifiable until a held-out CI benchmark exists.
- **Evidence-fitness — the thesis's "core innovation" — is nearly free here.** The discriminators
  (`phase`, `study_type`, `pub_type`, `year`, `source_kind`) are **already denormalized onto every
  retrieved hit's facets** (`trial_doc.py`, `europepmc.py`, etc.). A full evidence-pyramid ranker
  already exists — `MedicalAuthorityPolicy` in `authority.py` — but is **dead code** (registered in the
  manifest, consumed by nothing). v1 is mostly *wiring code that already exists*, at **zero added LLM
  calls**.
- They **compound**: the eval's `evidence_floor` check is what proves evidence-fitness actually helps.

## Non-goals (explicitly deferred — panel consensus)
- ❌ Per-claim LLM population/outcome/directness scoring → **fitness v2** (needs an LLM call).
- ❌ Contradiction clustering/adjudication → **Phase 2** (build on the tier signal).
- ❌ Patient-specific applicability / individualized advice → conflicts with the persona; not built.
- ❌ Separate retrieve/judge/write agents, per-claim evidence-planning, 5-dim uncertainty → over-engineered.
- ❌ Clinician-feedback endpoint → **Phase 2** (moat #4 seed).

---

## Part A — Evidence-fitness v1 (flag `ROSTER_EVIDENCE_FITNESS`, default OFF)

### Contract
Given the SAME verified findings, when the flag is ON: (1) each finding carries a deterministic
`evidence_kind` + tier; (2) the relevance ranking that trims findings to the compose cap **boosts
stronger, more-current evidence** (boost-only, never demotes below today's cosine order); (3) compose
sees each finding's tier/year and the diagnostics trace reports it. OFF → byte-identical to today.
**Grounding is never touched** — this reorders/annotates already-span-verified claims only.

### A1. Deterministic `facets → evidence_kind` mapper  *(new)*
New module `packages/vertical_medical/noesis_vertical_medical/evidence_kind.py`.
Pure structural mapping (Rule 18 — computable metadata, NOT semantic inference):
```
classify(source_key: str, facets: dict[str,str]) -> str   # returns an authority.py taxonomy key or ""
  - source_key "clinicaltrials" + study_type "interventional"      -> "rct"      (grade by phase, A3)
  - facets pub_type contains "systematic review"/"meta-analysis"   -> "systematic_review"
  - facets pub_type contains "randomized"/"rct"/"clinical trial"   -> "rct"
  - facets pub_type contains "cohort"/"case-control"               -> "cohort"
  - facets pub_type contains "case report"/"case series"           -> "case_report"/"case_series"
  - source_key "openfda"/"dailymed" (source_kind "drug_label")     -> "guideline"  (label = normative)
  - source_key "cdc" (source_kind "public_health")                 -> "guideline"
  - source_key "faers" (source_kind "adverse_event")               -> "cohort"     (safety signal)
  - unknown / missing                                              -> ""           (rank 0, never demotes)
```
Notes: string-match on the RAW `pub_type` is a **known-messy** input → conservative (unknown → "");
add a `recency_year(facets) -> int|None` reading the `year` facet. This module is the ONLY new
"logic"; everything else is wiring.

### A2. Carry `evidence_kind` + key facets onto the claim  *(enabling change)*
`VerifiedClaim` (`react.py`) today has `source_key/document_title/document_id` only. Add:
```
evidence_kind: str = ""      # from evidence_kind.classify(...)
year: int | None = None      # from facets
facets: dict = field(default_factory=dict)   # the atom's facets (for eval citation_constraints)
```
Populate where atoms → VerifiedClaim (both `_apply_answer` and the claims-first extraction path), using
the atom's `facets`. This single change unblocks BOTH the ranking boost (A3) and the eval `evidence_floor`
(B2). No behavior change until a consumer reads it.

### A3. Wire the pyramid into ranking  *(resurrect `authority.py`)*
In `_rank_claims_by_relevance` (`react.py:279`), when `ROSTER_EVIDENCE_FITNESS` is on, combine the
existing cosine score with a **bounded** evidence boost:
```
final = cosine                                   # today's signal, unchanged baseline
      + w_tier   * (authority.rank(kind)/6)       # 0..1, guideline/SR = apex
      + w_phase  * (authority.phase_weight(phase)/4)
      + w_recent * recency_score(year)            # e.g. newer within last ~7y
# weights small (e.g. 0.15 each) so cosine still dominates; boost-only.
```
Rules: **boost-only, never subtractive** (a strong-but-less-similar finding rises; a weak one is never
pushed below its cosine rank). Unknown kind (rank 0) = no boost = today's behavior. Keep it inside the
EXISTING ranking stage — **no new stage, no new LLM call.** Instantiate `MedicalAuthorityPolicy` once
and pass it in (it's already on `manifest.authority_policy`).

### A4. Compose + diagnostics visibility
- Annotate the findings block compose already builds (`react.py:706`): `[n] {text} (quote… — source:
  {source_key}; evidence: {evidence_kind}, {year})`. The model already sees findings; this just adds the
  tier/year tokens so its evidence-quality section and Reasoning-Read confidence can cite the real tier.
- Add each finding's `evidence_kind`/`year` to the **diagnostics trace** (the `funnel`/`trace` I just
  shipped) so the tiering is prod-observable without a UI change. Optionally show a small tier chip on
  each evidence card later (Phase-1.5, cosmetic).

### A5. Fix the false docstring
`authority.py:5` claims "The verification gate's criteria consume this…" — it doesn't (dead code today).
Correct it to describe the ranking-boost consumer once A3 lands.

### A6. (cheap add) Hard-token-validate the prose answer
Codex's correction: emitted claims/charts/interpretation are code-validated, but the **final prose
`composed_answer` is not** — it's held only by prompt + citation-index check. Add a deterministic
`_scan_answer_hard_tokens(composed_answer, verified_claims)` reusing `extract_hard_tokens`: any number/
dose/date/% in the prose that is absent from the union of verified findings is **reported** (a
diagnostics `failures`/warning entry + a coverage-gap-style flag), NOT auto-dropped (dropping the whole
prose is worse than surfacing). This closes the honesty gap and feeds the eval a real signal.

### Files touched (Part A)
`evidence_kind.py` (new) · `react.py` (VerifiedClaim fields + populate + `_rank_claims_by_relevance`
boost + findings annotation + prose token scan + diagnostics) · `authority.py` (docstring) ·
`apps/api/app.py` (flag helper `evidence_fitness_enabled()` + /config echo + pass into ResearchService) ·
`runtime/research.py` (thread the flag). All flag-gated, default OFF.

---

## Part B — Held-out medical eval harness (CI, risk-weighted, zero prod credits)

### Contract
A pytest that runs ~40–60 held-out medical cases through the agent with **scripted LLMs** (free, CI) and
scores them deterministically; plus a one-time `record`-mode baseline script (the single budgeted-credit
run). Reports pass-rate, **risk-weighted** score, and a hard **"zero critical failures"** gate. The
infra already exists (`run_qa_eval`/`summarize` in `eval/runner.py`, `score_qa` in `qa_scoring.py`) —
this enriches the gold schema and authors the held-out medical case set.

### B1. Gold schema (extend `eval_gold.py` GOLD; most fields already supported by `QaCase`)
Per case:
```
question:            str
expect:              "value" | "refuse" | "rank"
expected_values:     [str]        # deterministic standalone-number/substring match (already supported)
forbidden_values:    [str]        # must NOT appear (already supported)
required_phrases:    [str]        # already supported
forbidden_phrases:   [str]        # SAFETY: contraindicated/overclaim strings that must be absent
citation_constraints:[{...}]      # facet maps a verified claim must satisfy (already supported)
evidence_floor:      str          # NEW-as-convention: encode as a citation_constraint on evidence_kind,
                                  #   e.g. {"evidence_kind_min": "rct"} → top cited finding tier >= rct
clinical_risk:       "low"|"med"|"high"   # NEW: risk weight
category:            str          # treatment|safety|diagnosis|epidemiology|comparative|refuse
pico:                {population, intervention, comparator, outcome}   # NEW metadata (not scored in v1)
```
`evidence_floor` is checked by extending the scorer's facet path (B2) — the top cited claim's
`evidence_kind` rank must be ≥ the floor. This is the check that measures whether Part A helps.

### B2. Wire evidence_kind into the scorer input
`_answer_from_result` (`eval/runner.py:19`) currently maps only `source_key → {"source_class": …}`. Add
`evidence_kind`/`year` (now on VerifiedClaim from A2) into `citation_facets`, and add an
`evidence_floor` evaluation to `score_qa` (a min-rank check over verified claims). Small, deterministic.

### B3. Author ~40–60 held-out cases  *(the real work)*
Stratified: treatment-efficacy, safety/contraindication, comparative, diagnosis, epidemiology, and
**8–10 high-risk** cases including explicit **refuse** cases (no published evidence / should abstain) and
**safety** cases (a contraindicated claim in `forbidden_phrases`). Cases target the committed sample
corpus so the run is deterministic. **Contamination guard (Rule 5):** gold lives in a file NEVER shown
to the model at inference; add a test asserting no gold question/expected string appears in any prompt
or directive. Keep few-shots (if any) shaped differently from eval cases.

### B4. Risk-weighted summary + critical gate
Extend `summarize` (or a medical wrapper): report `fully_correct` pass-rate AND a **risk-weighted score**
(high-risk failures weighted heaviest), plus a hard gate: **any high-risk safety/refuse case failing
fails the suite** (expected clinical harm, not average rubric — thesis §11). LLM-judge stays **advisory
only**, never the pass gate (Rule 6: provenance ≠ correctness).

### B5. Tests + baseline
- `packages/vertical_medical/noesis_vertical_medical/test_eval_run.py` — scripted-LLM CI run (free),
  asserts the suite passes on known-good scripted answers and FAILS on a
  fabricated/wrong-tier answer (regression proof).
- `scripts/record_medical_baseline.py` — one budgeted `record` run against the real model to capture the
  live baseline pass-rate + risk-weighted score (the North-Star proxy number).

### Files touched (Part B)
`eval_gold.py` (schema + ~40–60 cases) · `eval/runner.py` (`_answer_from_result` facets + `evidence_floor`)
· `eval/qa_scoring.py` (`evidence_floor` min-rank check) · `test_eval_run.py` (new, medical) ·
`scripts/record_medical_baseline.py` (new) · a contamination test.

---

## Execution order (measure → improve → re-measure)
1. **Part B first (harness + cases), fitness OFF** → record the baseline pass-rate + risk-weighted score.
2. **Part A (evidence-fitness v1)** → flip `ROSTER_EVIDENCE_FITNESS` ON in the eval.
3. **Re-run the harness** → require: `evidence_floor` pass-rate **rises**, `fully_correct` does **not
   regress**, **zero critical failures**. That delta is the first quantitative evidence Noesis is moving
   toward "the right evidence for the claim."
4. Prod-verify: with the flag ON in prod, one research call — confirm the diagnostics trace shows each
   finding's tier and the ranking changed (observable via the trace shipped this week).

## Acceptance criteria
- OFF path byte-identical (both flags default OFF); offline suite green.
- Eval runs in CI with **zero prod credits**; a fabricated or wrong-tier answer **fails** the suite.
- With fitness ON: measurable `evidence_floor` improvement, no `fully_correct` regression, no critical
  failure; the prose hard-token scan reports 0 unsupported tokens on the gold set (or flags them).
- Every new behavior flag-gated, prod-observable via the diagnostics trace.

## Top risks (panel)
1. **Mapper misclassification** erodes the exact trust that is the pitch → conservative defaults
   (unknown→0), boost-only-never-demote, `evidence_floor` catches regressions, flag OFF by default.
2. **Eval contamination / overfit** (small N) → physical separation, a contamination test, never
   prompt-tune against gold.
3. **Provenance ≠ correctness** (Rule 6) → gold-value + `evidence_floor` + risk-weighted safety/refuse
   cases are the gate; LLM-judge advisory only.
4. **Compose-directive bloat** (the historical 2048→8000 truncation bug) → A4 adds only short tier/year
   tokens to existing findings; watch token budget, keep the diagnostics-side visibility primary.
