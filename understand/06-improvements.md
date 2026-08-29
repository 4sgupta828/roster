# 06 · Improvements — knobs, gaps, and ideas

← [05-design-philosophy](05-design-philosophy.md) · [Back to README](README.md)

Now that you understand the system, here's how to *change* it — the tuning knobs
and their effects, the recall/precision/cost tradeoffs, the known gaps (some
flagged right in the code), and concrete improvement ideas with rationale. This
doc is deliberately honest about limitations.

---

## 1. The knobs and what they do

### Retrieval / loop shape

| Knob | Where | Default | Effect · turn it up when… |
|------|-------|---------|----------------------------|
| `max_steps` | `research.py:72`, `react.py:191` | 8 | Max search/answer iterations. More steps = more chances to gather evidence for hard multi-hop questions, at more latency + cost. |
| `k` | `react.py:190` | 10 | Results returned per search. Higher `k` = more evidence per step (recall ↑), bigger prompts (cost ↑), more noise the model must sift. |
| `fetch_pool` | `dto.py:95` | 60 | Per-leg candidate pool before fusion. Higher = better ranking has more to work with; more DB work. |
| `planner_atom_window` | `react.py:193` | 60 | Atoms *shown* to the planner per step. Higher = the model sees more accumulated evidence (better late-step synthesis), bigger prompts. Note: verification always has the full store regardless. |
| `RRF_K` | `fusion.py:11` | 60 | Reciprocal-rank-fusion constant. Lower = top ranks dominate more; higher = flatter fusion. |
| `ORIGINAL_WEIGHT` / `REPEAT_BONUS` | `multi.py:19-20` | 1.5 / 0.15 | How much the original query outweighs reformulations, and the bonus for a block hit by multiple variants. |

### Extraction / compose caps

| Knob | Where | Default | Effect |
|------|-------|---------|--------|
| `atom_cap` (`ROSTER_ATOM_CAP`) | `react.py:189`, `app.py:230` | 1600 (6000 if evidence-select) | Per-atom character window the extractor sees. **Too low silently truncates the sentence holding an effect size / CI** (`claims_first.py:27-31`) — a real recall bug on full-text papers. |
| `_COMPOSE_CLAIM_CAP` | `react.py:33` | 30 | Max verified findings sent to compose. Module constant — not env-tunable (see gaps). |
| `_EXTRACT_COLLECT` | `react.py:34` | 80 | Under evidence-select, how many verified claims to gather before ranking down to the compose cap. |
| `_ATOMS_PER_CALL` / `_ENTAIL_CHUNK` | `claims_first.py:25-26` | 10 / 12 | Batch sizes for extraction / entailment. Bigger batches = fewer calls (cheaper), risk of the model losing track within a batch. |

### Feature flags (all default OFF)

| Flag | Effect |
|------|--------|
| `ROSTER_CLAIMS_FIRST` | Mine every atom for claims (recall ↑↑, cost ↑↑ — see budget gap below). |
| `ROSTER_EVIDENCE_SELECT` | Rank verified claims by question-relevance before the cap + widen `atom_cap` to 6000. Better claim selection + fewer truncated figures. |
| `ROSTER_STRUCTURED_ANSWERS` / `ROSTER_CLINICAL_SYNTHESIS` | Shape the answer into medical sections / sharpen in-section discipline. |
| `ROSTER_COUNTRY_SCOPE` | Hard-filter retrieval on `source_country`. **Do not enable until every block is tagged** (`app.py:64-69`). |
| `ROSTER_CONVERSATION` / `ROSTER_VISION` / `ROSTER_GAP_HEALING` / `ROSTER_STREAM` | Threads / image reading / self-healing corpus / live SSE. |

### Model selection

`ROSTER_LLM_MODEL` (`anthropic_llm.py:17`, default `claude-sonnet-5`),
`ROSTER_EXTRACT_MODEL` (`gpt-4o-mini`), `ROSTER_ENTAIL_MODEL` (`gpt-4o`),
`ROSTER_FALLBACK_MODEL` (`gpt-4o`), `ROSTER_PLANNER_MODEL` (empty → use the strong
model for planning too). Note the comment at `app.py:220-223`: a *cheaper* planner
(haiku) once **paraphrased quotes**, which then failed the verbatim span check — a
grounding regression. So the planner is intentionally left as the strong model.
Lesson: **the model that emits quotes must be strong enough to copy verbatim.**

---

## 2. The recall / precision / cost triangle

Most knobs trade along this triangle. A map:

```
            RECALL (find more real evidence)
                      /\
                     /  \
   k↑, max_steps↑,  /    \  claims_first, evidence_select,
   fetch_pool↑,    /      \  atom_cap↑, full-text ingest
   web aux on     /        \
                 /__________\
          COST              PRECISION
   (fewer/cheaper      (entailment gate,
    calls, batching,    authority ranking,
    caps, spinning      directly_addresses
    detector)           honesty signal)
```

- **Recall levers:** more/bigger searches, multi-query fusion, the web aux source,
  claims-first extraction, a wider `atom_cap`, and ingesting full text rather than
  abstracts. Every one of these is *safe* to push because the provenance gate sits
  downstream — you can't buy recall with fabrication.
- **Precision levers:** the entailment gate (claims-first), the authority/evidence
  pyramid (`authority.py`), and the `directly_addresses` honesty signal that flags
  tangential answers. These don't add facts; they *filter* or *label* them.
- **Cost levers:** the budget governor, batched embedding, model tiering, the
  once-per-step web query, and the spinning detector. These bound spend without
  touching correctness.

The elegant property of the architecture: **recall and precision are largely
decoupled from safety.** You can turn recall way up and the worst outcome is a
tangential-but-grounded answer that flags its own gap — never a fabrication.

---

## 3. Known gaps and limitations (be honest)

These are real, and several are flagged in the code itself.

### 3a. The budget governor undercounts real spend

`BudgetState` meters the *primary* loop, compose, and vision. But the
**claims-first extraction/entailment calls and the fallback grounder run on
separate OpenAI clients and are never charged to the budget** — they're gated only
by `budget.exhausted` as an on/off switch, not metered per call
([03-answering §9](03-answering-questions.md); `claims_first.py`,
`fallback_grounder.py`). With `ROSTER_CLAIMS_FIRST` on, a single question can fire
many uncounted `gpt-4o-mini` + `gpt-4o` calls. **`max_calls=40` is not a true
spend ceiling when those flags are on.**
*Fix idea:* thread the same `BudgetState` (or a second token/dollar budget) into
`extract_claims`/`entail_claims`/`ground_claimless` and charge each call, or add a
`max_tokens`/dollar ceiling that spans all clients.

### 3b. Token budget is checked only reactively

Call count is checked predictively (`spent + 1 > max` before the call), but token
cost can only be charged *after* the response returns (`budget.py:39` vs `42`). A
single call can overshoot a `max_tokens` ceiling; only the *next* reserve blocks.
For hard token caps you'd want a pre-call estimate.

### 3c. Compose caps are module constants, not tunable

`_COMPOSE_CLAIM_CAP = 30` and `_EXTRACT_COLLECT = 80` are hardcoded
(`react.py:33-34`). A dense question with 50 genuinely relevant findings loses 20
of them to a constant no operator can raise without a code change.
*Fix idea:* make them env-tunable (like `atom_cap`) or scale with question
complexity.

### 3d. `coverage_gap` gating is a no-op stub

`MedicalGatingPolicy.coverage_gap` always returns `None` (`gating.py:21-34`). So
the *search-time* gap signal is dead; all real gaps come from the compose
`directly_addresses` honesty judgment. That's a defensible design (LLM-owned, per
Rule 18) but it means the in-loop "try another source" nudge
(`react.py:388-392`) essentially never fires for medical.
*Fix idea:* implement `coverage_gap` via a small LLM plan-extraction that names
the question's required condition/intervention and checks whether any retrieved
hit's facets cover it — the ontology-validated version the docstring anticipates.

### 3e. The gap-fill → answer loop isn't closed automatically

Gap-healing *enqueues* ingest jobs, a background thread ingests them, and the new
blocks land in `rs_block` — but nothing automatically **re-runs the original
question** once its evidence arrives. The user has to ask again. The loop is
"heal the corpus," not "heal *this answer*."
*Fix idea:* attach the originating `session_id` to gap jobs (the queue already has
a `question` column, `gap_queue.py:31`) and re-answer + notify when the jobs
complete.

### 3f. RxNorm cross-linking is built but dormant

`rxnorm.py` can map a drug name → canonical RxCUI so the same drug links across
trials/labels/papers/adverse-events, but it's **not wired into ingest facets**
(`coverage.py:17`). Today a query for "semaglutide" won't automatically pull
"Ozempic"/"Wegovy" evidence unless the text mentions the queried term.
*Fix idea:* call `rxnorm.enrich` in `materialize_to_postgres`/ingest to stamp an
`rxcui` facet, then let retrieval expand a drug query by RxCUI.

### 3g. Corpus dedup is exact-bytes / exact-text only

Documents dedup by raw-byte sha256, blocks by text sha256 (`repository.py:33-39`,
`splitter.py:74`). Near-duplicates (same trial fetched with a trivially different
field, the same paragraph with one reflowed word) are *not* merged. This is
fine and deterministic, but it means the corpus can carry semantic duplicates
that dilute retrieval diversity.

### 3h. The offline fixture corpus is 2 trials

Without a DSN, the medical corpus is *two sample trials* (`source.py:25-45`). The
held-out eval (`eval_gold.py`) has exactly 2 cases. This proves the *mechanics*
offline but is far too small to measure real answer quality — the meaningful eval
requires a populated Postgres corpus.

### 3i. HTML/PDF parsing is minimal in-kernel

The kernel ships only a stdlib HTML text-extractor and plain-text parser
(`parsers.py`). The medical vertical sidesteps this by synthesizing clean markdown
from structured APIs — but a vertical that must ingest real PDFs needs to register
a heavier parser (docling), which isn't in-tree here.

---

## 4. The "effort multiplier" (a knob being added)

The task brief mentions an **effort multiplier** being introduced. As of the code
state these docs describe, there is **no `effort` knob in `apps/` or `packages/`**
(grep finds only "best-effort" prose) — so treat it as *in-flight / proposed*, not
shipped. Conceptually it would be a single dial that scales the recall/cost knobs
together — e.g. multiply `max_steps`, `k`, and the compose/extract caps by an
"effort" factor so a user can ask for a quick answer vs. a deep one without tuning
five separate values.

*Design note if you build it:* keep it flag-gated and default to 1.0×
(no-op) per Rule 20, echo the resolved value in `/config`, and — critically —
make it also scale the **budget** (`max_calls`) so a 3× effort answer gets a 3×
spend ceiling rather than starving mid-loop. Pair it with the budget fix in §3a so
"effort" reflects *true* spend including the uncounted extraction calls.

---

## 5. Higher-leverage improvement ideas

Ranked roughly by value-to-effort:

1. **Close the budget accounting (§3a).** Highest priority: without it, `effort`
   and `claims_first` spend is invisible. Small change, big honesty win.
2. **Make compose/extract caps env-tunable (§3c).** One-line-per-constant change
   that unblocks dense-question quality.
3. **Implement real `coverage_gap` (§3d)** as an LLM plan-check. Revives the
   in-loop "reach for another source" behavior and improves honest abstention.
4. **Close the gap-fill → re-answer loop (§3e).** Turns "self-healing corpus" into
   "self-healing *answers*" — a genuinely differentiating feature.
5. **Wire RxNorm enrichment (§3f)** and expand drug queries by RxCUI. Directly
   improves recall for the most common query shape (a named drug).
6. **Add a reranker stage.** RRF fusion + BM25/dense is solid, but a cross-encoder
   reranker over the top-`fetch_pool` candidates (before atoms are formed) would
   sharpen precision at the point where the model's attention is most limited.
7. **Add an eval harness with a populated corpus (§3h).** The mechanics are
   proven; answer *quality* is not measurable at 2 trials. This is what would let
   you tune everything above with evidence instead of intuition.
8. **Near-duplicate block merging (§3g)** via embedding similarity at ingest, to
   improve retrieval diversity per query.

---

## 6. How to actually test a change in prod

The project's deployment target is Railway (`railway up` / auto-deploy from the
default branch; `railway.toml`). Because everything is flag-gated and `/config`
echoes resolved flags, the safe loop is:

1. Ship the change dark (flag OFF) → confirm OFF is byte-identical.
2. Flip the flag in the Railway dashboard (no redeploy needed for env flags).
3. Read `/config` to confirm the resolved flag value.
4. Drive the real prod endpoint (`POST /research`, or `/research/stream`) and
   inspect the actual `ResearchOut` — `grounded`, the `claims` with their quotes
   and `url`s, `source_stats`, `stopped_reason`, `coverage_gaps`.
5. If it regresses, flip the flag OFF — instant rollback, no revert.

That flag-driven, prod-observable loop is the payoff of all the discipline in
[05-design-philosophy](05-design-philosophy.md): you can change this system
boldly, because when you're wrong the system tells you honestly and you can undo
it in one switch.

## Tracked known issues

### Absence handling ("no established / no approved X") — fixed, residual RELIABILITY
For a question about a specific entity that does not exist (a fixed-dose combination
pill, an FDA-approved gene therapy for hypertension), the correct answer STATES the
absence and cites what IS known, rather than confabulating. Two fixes landed:
1. **Compose directive** (`react.py`, answer-focus clause): a narrow existence/approval
   instruction — when the findings show only components/adjacent research, state the
   evidence doesn't establish the specific thing and set `directly_addresses=false`.
2. **Eval contract** (`eval_clinical_gold.py`, `qa_scoring.py`): the original
   `expect: refuse` (= `grounded==False`) MIS-FIRED — it penalized the correct
   *grounded* "no approved X exists; here's what's known" answer (Rule 4: the eval was
   wrong, not the system). Reframed to `expect: "absence"` = the run must flag a
   coverage gap about the missing entity AND not confabulate it.
Verified: the system DOES state the absence + flags the gap (inspection-confirmed).
**Residual = reliability**: whether the coverage gap is emitted is model-variable
(one benchmark run flagged it, another didn't — the same non-determinism as the
reasoning-read skip). Next step (its own work): a targeted reliability nudge for the
absence signal on existence questions, analogous to the reasoning-retry.

### EuropePMC evidence tiers: re-ingest for full metadata
`europepmc.py` now stores the **strongest** `pubType` (was `pubType[0]`), and the
classifier reads an abstract self-label as a stopgap — but the ~213k **already-
ingested** EuropePMC blocks still carry the generic first `pubType`. The durable
fix is a re-ingest so the corrected metadata lands on existing blocks; the
gap-healing loop also corrects it gradually. Remove the abstract-scan stopgap in
`evidence_kind.classify` once the re-ingest completes.

---

← Back to the [README map](README.md)
