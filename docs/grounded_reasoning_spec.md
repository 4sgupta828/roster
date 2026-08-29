# Roster — Grounded Reasoning & Idea Generation (full spec)

Panel-vetted (Codex + Gemini 3-pro + 2 code-grounded subagents, 2026-08-19). Status: BUILDING.

## 1. Thesis (what we are proving)

Frontier chatbots (GPT) reason and brainstorm well but **ungrounded** — you cannot audit why a
leap was made. Roster's defensible edge is **auditable grounded reasoning**: it reasons and
generates ideas ambitiously, but **every leap traces back to evidence and passes a "does this
actually follow?" check**, and every leap is **labeled** by how solid it is.

**The proof:** on reasoning/brainstorm questions, an independent judge scores Roster (with this
feature) vs raw GPT. Roster should win on **groundedness** and **low arbitrary-leap rate** while
still producing useful ideas — i.e. it is *as creative but not arbitrary*.

## 2. Principle — asymmetric strictness

- **Facts:** stay strict. Every fact quotes a real source (the existing verbatim span-gate). UNCHANGED.
- **Reasoning:** ambitious, but disciplined. A reasoning step may state something no source states —
  provided it (a) is built only from grounded facts (or already-accepted steps), (b) passes a
  validity check ("does the conclusion follow from those premises?"), and (c) carries a confidence
  label. Validity = *does it follow now*, NOT *will it come true later* (we do not track predictions).

Rule of thumb: a **fact** that fails its gate is **dropped**; a **reasoning step** that fails is
**demoted** one rung (inference → hypothesis → speculation → drop). Ambition lives in the demotion.

## 3. The reasoning unit

```
DerivedClaim = {
  conclusion: str,              # the derived statement (may be absent from every source)
  basis: tuple[int, ...],       # 1-based indices into the verified findings it is built on (≥1)
  kind: str,                    # comparative | causal | arithmetic | implication | opportunity
  warrant: str,                 # one-line: why the conclusion follows from the basis
  falsifier: str,               # what observation would make it wrong (required for hypothesis/speculation)
  label: str,                   # ASSIGNED BY THE GATE, never self-declared: inference|hypothesis|speculation
}
```

## 4. The gate (three checks, cheap → expensive)

Applied to each candidate the LLM proposes, in order; fail the structural checks → dropped before any
judge runs (so a bare "Hypothesis: X" with no real basis can never exist):

1. **Structural (code, free).**
   - every `basis` index resolves to a real verified finding (or an already-accepted DerivedClaim);
   - the basis set is non-empty (no free-floating claim);
   - **no new hard tokens** — the conclusion introduces no number/date/%/$ absent from its basis text,
     UNLESS `kind == "arithmetic"` and code can re-derive the figure from the basis operands.
   - (reuses the existing `extract_hard_tokens` from `react.py`.)

2. **Validity judge (LLM, one call for the whole batch; cross-family when available).**
   For each surviving candidate: *"Given ONLY these basis findings, does the conclusion follow?"* →
   `valid` (deductively/strongly follows) | `plausible` (reasonable but a real jump) | `arbitrary`
   (does not follow / unsupported leap). Judges **validity, not truth**. Conservative on silence →
   demote, don't upgrade.

3. **Label assignment (code, from the judge verdict + kind).**
   - `valid` + code-decidable kind (comparative/arithmetic/transitive) → **inference**
   - `valid` (causal/implication) OR `plausible` → **hypothesis** (must have a falsifier)
   - `arbitrary` but has a concrete falsifier AND idea-mode is on → **speculation** (quarantined in UI)
   - otherwise → **dropped**

Guarantee extended: not just "every fact traces to a span," but **"every reasoning step traces to a
basis of grounded facts, and the leap was checked."**

## 5. Idea-generation (brainstorm) mode

A flag/mode `generate_ideas`: the derive prompt additionally asks for `kind: "opportunity"` claims —
whitespace, new combinations, second-order implications — that are NOT in any source. These run the
SAME gate, so an idea is only emitted if it is anchored to grounded facts and survives the validity
check; it is labeled **hypothesis** or **speculation** and carries a falsifier. This is the
"grounded brainstorming tool": creative, but never arbitrary.

## 6. Where it lives (additive, low-risk)

- New kernel module `packages/kernel/roster_kernel/research/reason.py` — domain-free. Pure function
  over already-VERIFIED findings (runs AFTER the fact gate; adds no fact).
- Hooked in `runtime/research.py` after `run_react` returns verified claims, behind a flag; appends a
  `derivations` list to the answer result and a "Reasoning & Ideas" answer section.
- API echoes `derivations` on the research response so the UI can render the audit view (premise
  chain + label + falsifier per leap).
- Manifest: an optional `derive_prompt` / `idea_prompt` slot the vertical fills with domain wording;
  kernel default is domain-neutral (keeps the kernel purity invariant green).

## 7. Flags (all default OFF, Rule 20)

- `ROSTER_DERIVE` — master switch for the reasoning gate + `derivations` output.
- `ROSTER_DERIVE_IDEAS` — also generate `opportunity`/brainstorm claims (rides ROSTER_DERIVE).
- OFF path is byte-identical to today.

## 8. The yardstick (fast, static — no waiting on the future)

`packages/kernel/roster_kernel/eval/reason_scoring.py` + a fixture set of reasoning cases where we KNOW
which leaps are valid vs arbitrary. Each case: `{findings, valid_conclusions, arbitrary_conclusions}`.
Score (a vector):
- **soundness (precision):** of the leaps Roster emits as `inference`, how many are actually valid?
  MUST be ≥ 0.95 (arbitrary leaps must never ship as confident inference).
- **coverage (recall):** of the known-valid leaps, how many did Roster derive? (> 0; higher = better.)
- **arbitrary-suppression:** every known-arbitrary conclusion must be dropped or labeled speculation,
  NEVER inference.
Adversarial by construction (Rule 7): e.g. "A>B, C>B ⇒ A>C" (arbitrary) sits next to "A>B, B>C ⇒ A>C"
(valid). Held out from every prompt.

## 9. The proof vs GPT

`evals/derive_vs_gpt.py`: a set of deep-tech reasoning/brainstorm questions. For each, produce:
- **Roster** answer with `ROSTER_DERIVE(_IDEAS)` on (grounded + gated + labeled derivations);
- **raw GPT** answer (OpenAI API) to the same question, no grounding harness.
An **independent judge** (a third model, blind to which is which, position-debiased) scores each on:
1. **Groundedness** — can each substantive claim be traced to evidence? (Roster should win big.)
2. **Arbitrary-leap rate** — fraction of claims that are unsupported jumps. (Roster should be lower.)
3. **Idea usefulness** — are the generated ideas non-obvious and relevant? (should be ≈ parity.)
4. **Calibrated honesty** — does it distinguish fact from inference from speculation? (Roster wins.)
Success = Roster ≥ parity on idea usefulness AND clearly better on grounding + arbitrary-rate +
calibration. That is the whole pitch: *as creative, but auditable and not arbitrary.*

## 10. Specialists (prerequisite, currently EMPTY)

Roster's Ask-Panel engine exists but the tech vertical set NO `panel_specialists` (empty tuple). The 5
extraction lenses were meant to seed them (never wired). Phase 3 wires the roster:
Funding & traction · Technology & IP · Competitive landscape · Market sentiment · Team & execution
(+ a Foresight/Brainstorm role). Then the derive gate runs on the specialists' **disagreements** —
the highest-value place for grounded derivation (reconciling conflicting expert views).

## 11. Build order

1. **Yardstick first** (§8) — the measuring stick before the feature. reason_scoring.py + fixtures.
2. **The gate** (§3–4) — reason.py + unit tests proving arbitrary leaps are dropped (fake-LLM, deterministic).
3. **Wire it** (§6) behind `ROSTER_DERIVE`; run the yardstick live; tune until soundness ≥ 0.95.
4. **Prove vs GPT** (§9) — the head-to-head.
5. **Brainstorm mode** (§5) `ROSTER_DERIVE_IDEAS`.
6. **Specialists** (§10) + reason-over-disagreements.

## 12. Non-goals
- No prediction/outcome tracking over time.
- No loosening the fact gate.
- Not competing on raw cleverness — competing on auditable grounded reasoning.
