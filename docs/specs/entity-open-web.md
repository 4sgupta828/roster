# ROSTER_WEB_ENTITY_OPEN — entity-scoped, quality-screened open-web augmentation

## Contract (Rule 1)

**Behavior:** For a question the LLM judges to be diligence on a *specific named entity*
(company / product / project), and only when `ROSTER_WEB_ENTITY_OPEN` is ON, the research loop
fires **one additional open-web Exa probe** (whitelist dropped) on the **first planner query
only**, screens those open-web hits with an LLM page-quality judge, and merges the survivors
into the evidence pool alongside the existing whitelisted-Exa + DDG legs. Landscape/general
questions and the whitelist legs are untouched.

**Why:** the dominant breadth limiter (panel-verified) is that the strong provider (Exa) is
boxed to a 38-domain whitelist, so an arbitrary startup's own site / niche coverage is invisible
to it; DDG alone carries open-web reach and is weak. This opens the good provider to the open web
*only where breadth is actually missing*, without reintroducing the SEO-junk regression that got
global `web_open` turned off (the junk defense is an explicit LLM quality screen — the tier-grader
does NOT reject unknown domains, it ranks them 0, so junk still enters the pool without a screen).

**Invariants that MUST hold:**
- Provenance span-gate unchanged: every emitted sentence still needs a verbatim quote.
- Rule 18: the "specific entity?" judgment is LLM-owned (a Contract field), never a keyword matcher.
  The quality screen is also LLM-owned; on LLM error it **fails closed** (drop the open leg's hits).
- Rule 20: `ROSTER_WEB_ENTITY_OPEN` default OFF; OFF path **byte-identical** — no extra Exa call,
  no extra LLM call, contract derivation output identical (the subject_kind prompt variant is
  gated behind the flag at manifest build).
- Credit discipline: added cost on an eligible ON question is bounded to **+1 Exa probe (first
  query only) + 1 small quality-judge call**. Never per-step, never on general questions.

**Success criterion:** on a Blazel-class entity question, ON surfaces the entity's own primary
sources (reachable only via open web) and increases grounded evidence breadth vs OFF; on an
AI-landscape control question, ON is behaviorally identical to OFF (no open leg fires). OFF path
byte-identical (asserted by test).

## Tasks (execute in order; review between)

### T1 — Contract `subject_kind` (kernel mechanics + vertical prompt), flag-gated prompt

**Kernel — `packages/kernel/roster_kernel/research/contract.py`:**
- Add `subject_kind: str = ""` to `Contract` (dataclass) with a doc comment: opaque LLM judgment,
  `"specific_entity"` = diligence on one named company/product/project, `"general"` / `""` = not.
- Add `subject_kind: str = ""` to `_ContractOut`.
- In `derive_contract`, parse it: `sk = (getattr(p,"subject_kind","") or "").strip().lower()`;
  keep only if `sk in ("specific_entity","general")` else `""`; pass to the returned `Contract`.
  Fail-safe path already returns `None` → subject_kind is moot when derivation fails.

**Vertical — `packages/vertical_roster/roster_vertical/answer_contract.py`:**
- Add a SECOND contract prompt constant `TECH_CONTRACT_PROMPT_ENTITY` = the existing
  `TECH_CONTRACT_PROMPT` **plus** an added output-field spec instructing the LLM to emit
  `subject_kind`: `"specific_entity"` when the question is diligence on a single named
  company/product/project (e.g. "what is Blazel", "how does X's tech work / what's its moat"),
  else `"general"`. Do NOT edit the existing constant (keeps OFF byte-identical).

**Manifest — `packages/vertical_roster/roster_vertical/manifest.py`:**
- Add `web_entity_open_on()` reading `ROSTER_WEB_ENTITY_OPEN` (default false), same style as other
  `*_on()` flags in the vertical.
- Set `contract_prompt = (TECH_CONTRACT_PROMPT_ENTITY if web_entity_open_on() else TECH_CONTRACT_PROMPT)`.

**Tests:** contract.py unit — subject_kind parses/validates/fail-safe; OFF manifest uses the
original prompt (identity), ON uses the entity variant.

### T2 — Quality-screen module (kernel, LLM-owned, fail-closed)

**New file `packages/kernel/roster_kernel/research/web_quality.py`:**
- `async def screen_open_web_hits(hits: list[BlockHit], *, question: str, llm, prompt: str|None,
  budget) -> list[BlockHit]`:
  - If `not hits` or `not prompt` or `llm is None` → return `[]` (nothing to add / cannot judge → fail closed).
  - One batched `llm.complete` with `response_format` = a pydantic model listing, per candidate
    (indexed by url+title+body-excerpt), `keep: bool` (+ optional short reason). System prompt =
    the vertical-supplied `prompt` (keep official/self-reported, reputable third-party, technical
    docs, structured profiles; drop content-farm/SEO/irrelevant).
  - `budget.charge(calls=1, tokens=res.output_tokens)`.
  - Return only hits whose index was kept. On ANY exception → `_log.warning` + return `[]` (fail closed).
- Keep it domain-free (kernel litmus): the prompt is injected, no tech vocabulary here.

**Vertical prompt:** add `WEB_QUALITY_PROMPT` in `answer_contract.py` (or a new `web_quality.py`
in the vertical) describing keep/drop classes for tech open-web pages; expose via the manifest as
a new opaque slot `web_quality_prompt` on `VerticalManifest` (add the field to
`packages/kernel/roster_kernel/contract/manifest.py`, default None — domain-free).

**Tests:** fake-LLM keep/drop; empty hits → []; llm None / prompt None → []; exception → [].

### T3 — Open-web leg wiring in the ReAct loop

**`packages/kernel/roster_kernel/research/react.py`:**
- Add `run_react` params: `entity_open_web: bool = False`, `web_quality_prompt: str | None = None`.
- Compute once (near the contract resolution, ~line 1166): `_entity_open = bool(entity_open_web
  and _contract and getattr(_contract, "subject_kind", "") == "specific_entity")`. Log it in the
  existing answer-contract log line.
- In the search step, **only when `step_i == 0` and `_entity_open` and `aux_source is not None`**,
  add an extra leg: `("web:entity_open", aux_source.search(replace(base_req, web_open=True)))`.
  (base_req default `web_open=_web_open` stays False → the normal `web` leg stays whitelisted.)
- When the `web:entity_open` leg lands, before merging its hits into `hits`, pass them through
  `screen_open_web_hits(..., question=question, llm=llm, prompt=web_quality_prompt, budget=budget)`.
  Screened-out hits are dropped; survivors merge like any other leg. Emit the `retrieving` trace
  with the post-screen count. Diagnostics: record raw vs kept counts under `web_entity_open`.
- Fail-safe: a raised leg / screen exception degrades exactly like the existing web leg (visible in
  trace, answer proceeds on other legs).

**Tests:** with a fake aux_source + fake screen, assert the entity_open leg fires only on step 0 &
only when `_entity_open`; assert OFF (`entity_open_web=False`) adds no leg (byte-identical leg set).

### T4 — Service + app wiring

**`packages/kernel/roster_kernel/runtime/research.py`:** add `entity_open_web: bool = False` and
`web_quality_prompt: str | None = None` to `ResearchService`; pass both into the `run_react` call.
Source `web_quality_prompt` from the active manifest slot at service build.

**`apps/api/app.py`:** add `entity_open_web_enabled()` (reads `ROSTER_WEB_ENTITY_OPEN`, default
false) next to the other flag helpers; in `build_default_service` pass
`entity_open_web=entity_open_web_enabled()` and `web_quality_prompt=<manifest.web_quality_prompt>`.

**Tests:** flag OFF → `service.entity_open_web is False`; ON → True (mirror `test_answer_axes.py`).

### T5 — Verification

- Unit/integration: all new tests green; the kernel react + contract suites still pass; confirm the
  OFF leg-set is identical (no `web:entity_open` leg) via the T3 test.
- Prod (after deploy, flag ON): Blazel-class entity question → open leg fires, quality screen keeps
  primary sources, breadth up vs OFF; AI-landscape control → no open leg, identical behavior.
  Report exact input/observed (Rule 3).

## Non-goals / deferred
- Not raising global `max_results` (Codex: add a separate knob later only if diagnostics show the
  single open probe under-fetches).
- Not replacing DDG (runner-up approach; rejected — spends on every question + reopens junk on
  landscape).
