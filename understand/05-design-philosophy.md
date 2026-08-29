# 05 · Design philosophy — the *why*

← [04-medical-vertical](04-medical-vertical.md) · [Back to README](README.md) · Next: [06-improvements](06-improvements.md)

The previous docs explained *what* Noesis does and *how*. This one explains *why*
it's built this way. These principles aren't abstract preferences — they're
scars. The code and the project's operating rules (`CLAUDE.md`) both reference
numbered rules ("Rule 18", "Rule 20", "the panel"), and you'll find those rule
numbers cited right in the source comments. Understanding the philosophy is what
lets you *extend* the system without breaking its guarantees.

---

## The one-sentence thesis

> **The LLM owns MEANING; code owns STRUCTURE and PROVENANCE — and meaning never
> gets a regex shortcut.**

Every design decision below is a corollary of that split. Let's take them one at
a time.

---

## 1. The LLM owns meaning — no regex heuristics for semantic decisions

Any judgment requiring *understanding* — "is this evidence relevant?", "what does
this study conclude?", "does this quote support this claim?", "is this answer
tangential?" — is delegated entirely to the language model. The tempting
alternative is a keyword list or regex that *approximates* the judgment. Noesis
bans that outright for semantic decisions (this is "Rule 18," cited across the
code).

Why? Because a keyword heuristic breaks on the next document's vocabulary. It's
why the comments in `gating.py:23-26` explicitly *refuse* to detect coverage gaps
by free-text scanning and leave the method a stub until an LLM-extracted,
ontology-validated plan exists. It's why `_rank_claims_by_relevance` is careful to
note that a dense embedding cosine is a *computable* signal, not a semantic
heuristic (`react.py:88-94`).

**But code still owns structure.** The split is absolute, not fuzzy. Regexes are
fine for *computable/structural* facts: parsing the NCT-id format
(`entities.py:17`), validating citation format (`_refs_valid`, `react.py:74-85`),
splitting a `"source:native_id"` string (`links.py`), clamping a job limit to
`[1,400]` (`gap_planner.py:71-72`). None of those interpret *meaning*. The line:
**code parses and validates; the model interprets.**

---

## 2. Provenance ≠ correctness (and the gate is provenance)

This is the subtlest and most important idea in the whole system. The provenance
gate ([03-answering §4](03-answering-questions.md)) proves a quote *physically
exists* at its cited locator. That is **provenance** — proof the system didn't
*fabricate* the span. It is emphatically **not correctness** — it does not prove
the model picked the *right* span. The docstring says so directly
(`provenance.py:3-5`).

A model can cite a real, verbatim quote *from the wrong trial*. The gate passes it
(the quote is real); only a **held-out eval with gold answers** can catch that
the *wrong* real quote was chosen. So:

- The gate is *necessary but not sufficient*. It's the fabrication floor.
- "verifier N/N pass" is **never** presented as evidence of correctness — it's
  evidence of non-fabrication only.
- Semantic correctness needs gold-value / gold-decision checks on held-out data
  (`eval_gold.py`), not provenance checks.

Internalizing this distinction is what keeps you from over-trusting a grounded
answer. Grounded means "traceable," not "right."

---

## 3. Grounding as a *hard gate*, not a soft preference

Notice the gate is not a score you can tune, a confidence threshold, or a
re-ranking nudge. It is a **binary admission test** applied to every single claim
(`react.py:243-253`), and it's the *same* test everywhere — ReAct claims, the
fallback grounder, claims-first candidates, precision-lookup cells all pass
through the identical `verify` (`react.py:248`, `434`, `458`; `precision.py:79`).

The consequence is structural: no auxiliary generator can *weaken* the gate. They
can only *propose* candidates the gate then filters. This is why the system can
afford aggressive recall moves (a second model, bulk extraction) without risking
fabrication — the gate is downstream of all of them and immovable.

---

## 4. Fail-safe / abstain over guessing

When a component can't do its job, it degrades to *nothing*, never to a guess.
The pattern is everywhere, and the code distinguishes two flavors:

- **Fail-safe** (return empty / keep prior state): a failed vision read →
  `""` (`research.py:86-87`); the fallback grounder with no key → `[]`
  (`fallback_grounder.py:11-13`); claims-first on any error → answer unchanged
  (`react.py:480-481`); evidence-select on embedding error → original order
  (`react.py:99-102`).
- **Fail-closed** (drop the candidate): the provenance loader returns `None` for
  out-of-scope blocks → verify fails (`provenance.py:51-52`); the entailment judge
  being unavailable drops *all* candidates ("without a judge, don't ship extractor
  claims", `claims_first.py:117-125`).

The guiding value: the worst acceptable outcome is "I don't have evidence for
that," never "here's a plausible fabrication." For a medical tool this is
non-negotiable.

The one deliberate exception is **compose**, which is *not* allowed to silently
fail-safe to a blank — because it's the user-facing deliverable. It retries and,
if it truly can't complete, surfaces a *visible* note (`react.py:530-570`). Even
here, the principle holds: never a *silent* failure.

---

## 5. Feature flags, default OFF ("Rule 20")

Every user-visible or risky behavior ships behind an env flag that defaults OFF,
and the OFF path is byte-identical to the pre-flag behavior. You saw the whole
inventory in [the API layer](README.md): `ROSTER_STRUCTURED_ANSWERS`,
`ROSTER_CLINICAL_SYNTHESIS`, `ROSTER_VISION`, `ROSTER_GAP_HEALING`,
`ROSTER_STREAM`, `ROSTER_COUNTRY_SCOPE`, `ROSTER_CONVERSATION`,
`ROSTER_CLAIMS_FIRST`, `ROSTER_EVIDENCE_SELECT` (all in `app.py:29-93`, `225-230`).

The flag is three things at once: a **rollout switch** (turn a feature on for
prod without a redeploy), a **rollback path** (turn it off instantly if prod
surprises you), and an **A/B seam** (compare ON vs OFF). The clinical-synthesis
flag is a textbook example — it keeps *both* answer formats as separate constants
so OFF is a true no-op (`answer_format.py:60-68`, `app.py:207-216`). This is why
the codebase can carry so many in-progress capabilities safely: each is dark until
proven.

The `/config` endpoint echoes each flag's *resolved* value to the frontend
(`app.py:344-371`), so the UI renders to match the backend path actually taken —
the flag is server-authoritative, never read independently by FE and backend
(avoiding drift).

---

## 6. Held-out evals, and cassettes for reproducibility

An LLM feature is only "working" once a **held-out** eval says so — a test whose
question, answer, and document shape never appeared in any prompt, example, or
fixture the model saw at inference. The medical `eval_gold.py` is deliberately
tiny and adversarial: one factual case (`trial_condition`) and one *should-refuse*
case (`coverage_gap_unknown_condition`, expecting an honest gap on a condition not
in the corpus). The refuse-case is the important one — it tests that the system
*abstains* correctly, which a memorization-measuring eval would miss.

Reproducibility is enforced by the **cassette** system (`build.py:29-60`,
`providers/base.py`). Every provider runs in one of three modes
(`base.py:9-12`): `replay` (offline, deterministic, zero credits — the default),
`record`, or `live`. The same code path serves CI, local dev, and production;
credits are opt-in. There's even a guard that *raises* if a `live` call is
attempted under pytest without an explicit `ROSTER_ALLOW_LIVE=1` opt-in
(`base.py:37-45`) — tests can never accidentally spend money. This is what lets
the whole ingest→retrieve→answer path run in CI for free and deterministically.

---

## 7. Cost / credit discipline

LLM calls cost money, and an agentic loop can run away with credits. Noesis meters
this at multiple layers:

- The **per-run budget governor** (`budget.py`) caps calls per question and stops
  the loop when spent ([03-answering §9](03-answering-questions.md)).
- **Deferred batched embedding** turns "one API call per document" into
  "O(blocks/batch) calls" during ingest (`pipeline.py:108-120`).
- **Model tiering** in claims-first: cheap `gpt-4o-mini` for bulk extraction,
  stronger `gpt-4o` only for the entailment safety gate, one batched call each
  (`claims_first.py:20-26`).
- **Aux/web queried once per step**, not fanned out per reformulation, to bound
  latency and cost (`research.py:48-58`).
- **The spinning detector** forces an answer after two empty searches instead of
  burning the full step budget (`react.py:354`).

The philosophy: spend the strong model where correctness lives (compose,
entailment), spend cheap models on breadth (extraction), and never let a loop
spin unbounded.

---

## 8. Observability into the decision, not just the crash

Because LLM answers can be wrong in *subtle* ways, the system emits trace data
about what it decided, not just whether it crashed. `AnswerResult`
(`react.py:136-155`) carries `rejected_claims` (with reasons), `source_stats`
(retrieved vs. cited per source), `stopped_reason`, `retried_empty`,
`compose_failed`, and `coverage_gaps`. The streaming endpoint fires an event at
every phase (`step`/`search`/`found`/`verifying`/`verified`/`grounding`/
`extracting`/`selecting`/`composing`). So when an answer is wrong you can see
*which candidates were visible, which were selected, which were rejected, and
where ambiguity remained* — without rerunning the whole pipeline blindly.

---

## The core tradeoffs, made explicit

Every strong design chooses. Here's what Noesis chose and gave up:

| Chose | Over | Because |
|-------|------|---------|
| Verbatim substring gate | Semantic entailment as the primary gate | A substring check is *unfabricatable* and cheap. Entailment is added as a *second* gate in claims-first, not the primary one — the floor must be deterministic. |
| Recall via multiple recovery layers | A single clean pass | Real models abstain unpredictably; the gate makes aggressive recall safe, so the system stacks re-ask + second model + bulk extraction to claw back missed evidence. |
| Domain-free kernel + facets | Direct, domain-specific columns | Reuse: a new vertical is a package, not a fork. The cost is a layer of indirection (everything is a generic `facets` bag) and that the kernel can't optimize for domain structure it can't see. |
| Fail-safe/abstain | Best-effort guessing | In medicine a confident wrong answer is a hazard; "no evidence" is always acceptable. |
| Flags default-OFF, both paths kept | Hard-replacing old behavior | Safe rollout/rollback in prod without redeploys; the cost is dead config and two code paths to maintain. |
| Cassette replay as the default | Always calling live | Free, deterministic CI and evals; the cost is that cassettes must be recorded and can drift from live behavior. |

The unifying thread: **Noesis optimizes for trustworthiness under a model that
can lie.** It assumes the LLM is powerful but unreliable, and wraps it in
deterministic checks so that its *failures degrade to honesty* rather than to
confident fabrication.

---

Next: [what you'd change to improve it →](06-improvements.md)
