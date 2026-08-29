# ROSTER_DEEP_SYNTHESIS — deep, novel, grounded synthesis as the default answer

## Contract (Rule 1)
When `ROSTER_DEEP_SYNTHESIS` is ON, a non-lookup question's answer is a **synthesis-first grounded
analysis** (core thesis → tensions → second-order implications → mechanism), built around
**grounded derivations woven into the spine** (not a post-hoc appendix), with a **corrective
post-compose grounding-audit** so the richer prose introduces no unsupported figure. Lookups stay
crisp. OFF → byte-identical (today's compose path). Flip to default in prod only after a held-out
eval shows depth up + no grounding regression + lookups not bloated.

**Invariants:** span-gate unchanged; novelty = grounded derivation (label + basis + falsifier),
never new facts; the prose-audit turns "grounded-by-prompt" into "grounded-by-code" for the widened
prose surface. Rule 18 (LLM owns synthesis; structural gates only). Rule 20 (flag default OFF,
byte-identical OFF). Credit-disciplined (+2 derive calls + longer output on non-lookup; lookup ~unchanged).

## Verified seams (current main)
- derive() `reason.py:214` — 2 `.complete()` calls (propose+judge), returns surviving DerivedClaim
  (label/conclusion/basis/falsifier); **charges NO budget** → the relocated caller must charge.
- Currently derive runs POST-compose: `runtime/research.py:539` → `_render_derivations` (`:27`) appended
  to `composed_answer`. Compose never sees it (appendix).
- `_unsupported_prose_tokens(prose, verified)` `react.py:2090` = hard tokens in prose absent from every
  finding; used DIAGNOSTIC-ONLY at `:2054`. Make it corrective in deep mode.
- Caps: `_COMPOSE_CLAIM_CAP=30` (`react.py:74`), `_COMPOSE_MAX_TOKENS=16000` (`:33`, keep). Compose call `:1865`.
- Question kind: `runtime/research.py` `_Scaffold.kind` ∈ management|lookup|understanding (~:242-291).
- Compose-directive assembly `react.py:~1760-1779` (answer_format + addenda). Compose_user build `~1786`.
- Existing wiring template: `tech_synthesis`/`entity_open_web` flag → app.py → ResearchService → run_react.

## Tasks

### T1 — Deep format + persona + flag wiring (vertical + app)
- `answer_format.py` (or `reasoned.py`): add `TECH_DEEP_SYNTHESIS_FORMAT` — synthesis-first structure:
  Core thesis (non-obvious read across findings) · Tensions/contradictions (surface, don't smooth) ·
  Second-order implications ("which means…") · Mechanism (why it works this way) · question-scaled
  length · anti-padding ("depth from insight+structure, not word count; no boilerplate/restatement") ·
  grounding contract (facts cite [n] verbatim; inference wrapped [[R]] over cited findings/derivations;
  never a new number/date/name; separate disclosed vs inferred).
- `persona.py`: flag-gated deep-analyst clause ("reason over evidence, surface the non-obvious
  connection / implication / tension only synthesis can build; a correct recital is a FAILURE") —
  mirror the existing `_ADVICE_RULE` swap; build from the general-audience rule, not the memo persona.
- `manifest.py`: `deep_synthesis_on()` (ROSTER_DEEP_SYNTHESIS); expose the deep format + a flag the
  service/kernel can read.
- `app.py`: `deep_synthesis_enabled()`; pass `deep_synthesis=` into ResearchService; route like
  `reasoned_default_enabled` for unset engine (preserve explicit engine="standard").
- Tests: flag→service; deep format string present.

### T2 — Pre-compose derive-weave (kernel)
- `runtime/research.py`: when deep + kind != lookup, run `derive()` BEFORE compose and pass its
  survivors into run_react as `pre_derived` (new optional param) INSTEAD of the post-compose append;
  keep the post-compose `_render_derivations` path for OFF/non-deep (byte-identical OFF).
- `react.py`: accept `pre_derived: list | None`; when present, inject a distinct block into
  `compose_user` — "GROUNDED DERIVATIONS (already validated — weave the best ones; keep each label +
  falsifier; cite [Dn]) : [D1] <label>: <conclusion> (from [a],[b]; falsifier: …)". Instruct compose
  to make them the analytical backbone.
- **Charge budget** for the 2 derive calls when relocated (charge-after, like the frame-repair call).
- Tests: derive appears in compose_user before generation (deep); OFF → derive still post-compose appendix.

### T3 — Corrective prose-audit + per-kind caps + format selection (kernel)
- `react.py` run_react params: `deep_synthesis: bool = False`, `kind: str = ""` (+ `pre_derived` from T2).
- Compose-directive: when `deep_synthesis and kind != "lookup"`, use TECH_DEEP_SYNTHESIS_FORMAT as the
  base (replace, not append) — thread the deep format in via answer_format from the service/manifest,
  selected by flag+kind. lookup → today's format (crisp).
- Per-kind `compose_claim_cap` (only RAISE): lookup 20 · understanding ~48 · management/landscape ~60.
  Keep `_COMPOSE_MAX_TOKENS=16000`.
- **Corrective grounding-audit (deep only):** after compose, `u = _unsupported_prose_tokens(answer,
  verified)`; if `u` and not budget.exhausted → recompose ONCE with "remove the figures {u} not in the
  findings"; if still non-empty → fall back to the non-deep compose (or strip the offending sentence
  set) and log. Also validate `confidence.rationale` hard-tokens (currently ungated).
- OFF byte-identical: `deep_synthesis=False` → format unchanged, cap unchanged, audit stays diagnostic.
- Tests: OFF golden byte-identical (5 Qs); lookup stays crisp (no deep format, small cap); audit
  recompose fires on an injected unsupported figure; confidence gated.

### T4 — Held-out eval + verification
- Frozen set (~40-60) stratified by kind + ~10 adversarial (ambiguous siblings, conflicting sources,
  tempting-but-unsupported deep read). Lookups = anti-bloat control.
- Depth/novelty LLM-judge (DIFFERENT family, Rule 17): 0-3 on cross-evidence connection / implication /
  tension / so-what. Deep ON must beat OFF on management+understanding.
- Grounding-regression gates (must NOT regress): span-gate pass-rate; prose-audit pass-rate (~100%);
  interpretation/derive drop-rate. Anti-bloat: lookup length ON within ±10% of OFF.
- Provenance (Rule 11): record model/prompt/flag/SHA/question-ids/OFF+ON answers.
- Only flip ROSTER_DEEP_SYNTHESIS ON in prod after the eval clears.

## Biggest risk + de-risk
The deep directive widens the UNGATED free-prose surface (span-gate covers claims list, not prose).
Ship the **corrective prose-audit (T3) in the SAME change as the format** — never the format alone.
Dark-launch, run the eval OFF vs ON, confirm grounding holds, then flip.
