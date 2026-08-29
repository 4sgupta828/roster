# Roster Reflection Pass — implementation spec

Flag: `ROSTER_REFLECTION` = `off` (default) | `shadow` | `steer`. OFF byte-identical (Rule 20).
Panel-vetted (Codex + Gemini + code-grounded subagent, 2026-08-26). Builds on the existing single
`derive_contract` upfront call — NO second upfront pass.

## North star (user, verbatim)
Reflection must get to the SOUL / HEART of the question — the user's real underlying intent — and shape
the whole answer to it. "We may not always capture it, but we must get CLOSE." And: "We should
ABSOLUTELY use web tool, search tool, whatever can gather evidence ON DEMAND." A "go into details,
landscape" ask WANTS aggressive collection; the system must never shrug "evidence is thin" when it simply
did not look.

## The two defects this fixes (both observed in prod)
1. MUTED / DID-NOT-LOOK: coverage fan-out is CORPUS-ONLY (react.py:1286 `source.search`), and the only
   web-per-entity machinery (deep readers, entity_open) fires ONLY for `subject_kind==specific_entity`/
   `person`. A landscape/multi-entity/business question (`subject_kind==general`) gets ONE shallow web
   search and then reports "thin evidence" — for companies with abundant public coverage (Runway, Pika,
   Synthesia, Sand AI). => On-demand WEB coverage fan-out per named entity + dimension.
2. NAMESAKE / WRONG-ANCHOR: an ambiguous subject (Traversal.com) anchors on the literal string; the deep
   reader deepens the wrong entity. => Interactive, grounded, multi-select disambiguation — but only when
   genuinely necessary.

## Enriched contract (extend `_ContractOut` + `Contract` in contract.py; all default inert)
- `intent: str` — short inferred user job ("assess credibility of Traversal's founding team"). "" = none.
- `intent_confidence: "high"|"medium"|"low"|""` — steers ONLY when high/medium.
- `answer_brief: str` — "what a great answer must deliver." "" = none.
- `resolved_question: str` — faithful restatement. Used ADDITIVELY (an extra retrieval seed), NEVER
   substituted for the literal question (drift guard). "" = none.
- `ambiguity_risk: "high"|"medium"|"low"|""` — gates the disambiguation probe.
- `candidates: list[str]` — for a genuinely ambiguous subject, the distinct candidate readings (grounded
   later by a probe). "" list = none.
Existing fields (mode/entities/axes/stance/subject_kind) unchanged. Persist the new fields into
`result.question_contract` (react.py:1263) + diag. Prompt variant emitted ONLY under the flag (mirror
`subject_kind`'s flag-gated emission) so OFF derivation output is identical.

## Wiring (all guarded `if field:` like existing `_steer`/`_answer_dir`)
D0 INTENT STEER (confidence>=medium): append `answer_brief`/`intent` to planner steer (react.py:1127)
   and to the compose directive before the vertical directive (react.py:2394). Literal `Question:` at
   react.py:2345 UNCHANGED. Low/empty confidence => no append => byte-identical (+ optional ambiguity
   note). Fail-safe (Rule 18): intent is derivable from the QUESTION ALONE, never asserts entity facts;
   grounding stays with the span-gate.

D_WEB ON-DEMAND COVERAGE FAN-OUT (the muted fix): extend the coverage-leg fan-out (react.py:1282-1298)
   so, in addition to corpus legs via `source.search`, it dispatches BOUNDED WEB legs via `aux_source`
   (web_open) for the derived entities x dimensions — for landscape/multi-entity/general questions where
   corpus coverage is thin. Bounds: cap total web legs (e.g. <= 8), concurrency-limited, first step only,
   late-merged into `_c_stash` (planner window unaffected) and screened through the SAME
   screen_open_web_hits + span-gate + drop_dead_urls path the existing `web:deep`/`entity_open` legs use.
   Credit discipline: only fire web legs when (a) flag steer AND (b) the question is
   general/multi-entity/landscape (NOT a single-entity question already served by the deep reader) AND
   (c) bounded. Emit a diag trace of every web leg (Rule 13).

D3 COVERAGE NUDGE: add a mild `balanced` entry to ANSWER_PROFILES (answer_contract.py) — `max_steps:10`,
   `compose_claim_cap:42`, coverage-first `answer_directive` ("Prefer complete coverage over length; do
   NOT pad; explicitly name thin evidence"). Raise exploratory axes guidance 0-5 -> 3-6 when warranted.
   Turn coverage legs ON under the flag. Length stays bound to verified claims (anti-pad by construction).

D2 INTERACTIVE DISAMBIGUATION (only when NECESSARY; default AVOID): when `ambiguity_risk` is high AND
   material, run ONE cheap grounded probe on the raw subject; if the retrieved candidates are genuinely
   distinct entities and the model cannot confidently resolve which the user means, RETURN a
   clarification (NOT an answer) — a multiple-choice question with the grounded candidate readings, the
   user may pick ONE OR MORE (multi-select). Reuse the existing seam: `followup_clarify` /
   `needs_clarification` return path + the `.clarify` UI + guided-intake resubmit. On resubmit the
   selection anchors `entities` (one or many => compare-mode) BEFORE `_deep_*_entity` (react.py:1336) and
   `_choose_domain`. If the model CAN confidently resolve from evidence, do NOT ask — anchor silently.
   Never parametric. Log pre/post anchor.

## Flag / phasing
`shadow`: derive + persist + log intent/ambiguity/web-legs-that-WOULD-fire; steer/collect NOTHING (watch
confident-wrong intent before it acts, mirrors ROSTER_QUESTION_CONTRACT shadow). `steer`: all wiring live.
OFF: old prompt, old schema, old strings — byte-identical.

## Held-out eval (paired OFF vs steer)
Buckets: namesake (Traversal.com), multi-entity landscape (text-to-video startups — MUST now collect web
per company, not shrug), thin-evidence-stays-short (obscure startup — must NOT pad), ambiguous person
(must clarify not blend), normal-balanced (Blazel — coverage up, precision flat). Metrics: entity
precision, namesake contamination count, axis recall, web-legs fired, padding index (tokens/verified
claim), groundedness (span-gate pass rate unchanged).

## Build order
P1 schema+prompt+flag (shadow, persist, diag) — OFF byte-identical proven. P2 intent steer. P3 web
coverage fan-out (the muted fix). P4 balanced profile. P5 interactive disambiguation clarify (API+FE).
P6 eval + prod verify. Each phase flag-gated, OFF byte-identical, verified before the next.
