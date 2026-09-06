# Guided Intake — from a vague ask to a contract, with the artifacts a search needs

Status: spec v2 (2026-09-05), panel-reviewed (Codex, Gemini, code-grounded agent; verdicts in §10). Owner ask:
"a guided intake like noesis that helps understand requirements for job or candidate search, ensures a résumé
is present for job search or a job description for candidate search, can draft a JD if there is none and save
it, formulates the most accurate queries for either search or both, and advises how to navigate the results.
It can leverage existing postings to formulate the JD that is central across postings, then customize it to
the user."

Bound by: the facet contract / evaluator spec (`docs/specs/facet-contract-evaluator.md` — the ONE search
operation), the recruiter intake contract (`docs/specs/recruiter-workflows.md` P0: "asks for clarification
only when missing information materially changes the search"), CLAUDE.md (model owns meaning, code owns
gates; kernel domain-free; credits projected and gated; evidence typed; intent ≠ fact; mobile-first; tests
before code).

---

## 0. The principle

An intake is not a chat. It is the shortest path from what the user HAS to what a search NEEDS:

    HAS: words · a résumé · a JD · nothing
      → NEEDS: an ARTIFACT (profile or JD) + a CONTRACT (must / prefer / avoid / center / rank_by / text / scope)
      → the GAP, filled by the cheapest means, in this order: read it · look it up · ask ONE question · draft it

Consequences:

1. **The output is the contract.** Every intake ends in the same `Contract` the rail and the evaluator run,
   plus an artifact. The hand-off is one ordinary Talent or Jobs turn with the contract attached; the rail,
   saved maps, calibration and keep-fresh work unchanged. No new search path.
2. **Two kinds of question, two gates.** REQUIRED dimensions (per direction, fixed by the vertical: role /
   function, level, geo or work mode; for candidate search also the evidence requirement) are asked when the
   artifact and the words leave them unknown — regardless of the corpus. OPTIONAL dimensions are asked only
   when the answer would change the results: the counts over the current slice are spread (level 40 / 60,
   company type 3-way), the key is not already constrained, and the ask budget remains. Cap: 2 required + 2
   optional; "search now" ends it from any turn. Code decides WHETHER (gates); the model decides HOW (words).
3. **Facts and intent stay apart.** A résumé states what the person DID (→ text, skills, level prefer); the
   intake asks what they WANT (level target, work mode, geo, comp band → center / prefer / must). A posting's
   requirement is what the market ASKS, labeled so; the user's own answers are what they want. A drafted JD
   never contains a line that neither the corpus centre nor the user said.
4. **Ratify before running.** The ready card shows the plain "what I understood" and the compiled contract as
   chips, with the pool size; nothing runs until the user taps a search. (The predecessor intake's lesson: the
   translation from chat to contract must be visible to be trusted.)

---

## 1. Where things live

| Piece | Location | Domain words? |
|---|---|---|
| Ask gates over counts (`spread`, `worth_asking`), the intake state machine (stage, next gap), turn budget, "search now" | `packages/kernel/roster_kernel/facets/intake.py` (new; pure functions over counts + contract + a list of required keys the caller supplies) | No — mechanics over opaque keys |
| Persona; REQUIRED keys per direction; question words and option labels per key; what a résumé / a JD is; the JD template; the centre-of-postings prompts; advice phrasing | `packages/vertical_roster/roster_vertical/intake.py` (new) | Yes |
| `/intake/step`, `/intake/jd/draft`, `/me/briefs`; the intake session row; the centre builder over cached posting summaries; hand-off | `apps/api/intake.py` (new) + `apps/api/app.py` | neutral |
| UI: the Guided mode re-pointed (transcript, option chips, artifact stage, ready card) | `apps/web/index.html` | — |

**No server intake exists today** (code review): the FE Guided shell survives (`renderTriage` etc.), but
`/triage/step` has no route, `/config` hard-codes `triage_enabled: False`, and the vertical exports no triage
prompt — the endpoint is written from scratch; the inherited medical-lineage FE logic (safety / urgent,
register, retrieval terms, Q&A / Panel routes) is DELETED, not re-pointed.

**State ownership (panel, both CLIs):** the transcript stays FE-held and rides each request (cheap, stateless
model turn) UNTIL an artifact or a draft exists; from then on the server owns an `roster_intake` session row
(`id, user_id, direction, state jsonb, transcript jsonb, profile_ref, brief_id, created_at, updated_at`) and
the FE sends `intake_id` + the new message only. Attachments are parsed ONCE (the existing docling-first
parser + content-hash cache) and referenced, never re-sent. Signed-out users get the FE-held mode only; the
artifact stage asks them to sign in when they want a résumé or a JD kept.

---

## 2. Artifacts

| Artifact | Needed by | Sources, in order | Stored |
|---|---|---|---|
| **Profile** | job search | résumé on file (`roster_candidate_profile.parsed_profile`) → attachment this session (parsed, offered to save as the Apply résumé) → a self-description typed in the intake (`is_self_description` path → parsed into the profile shape, offered to save) | existing table |
| **Job description** | candidate search | pasted JD / URL (the `Find candidates` inputs; the URL path fetches the posting) → one of the user's company's own live postings in the index (search by company) → DRAFTED from the centre of peer postings + the user's answers (§4) | new `roster_brief` (kind `jd`) |

### 2.1 Completeness — an artifact is present AND complete (owner, 2026-09-05)

Presence is not enough: a résumé can be terse or silent on the things a search turns on; a pasted JD can omit
the level, the location, the comp, or the one skill the hiring manager actually cares about. Each artifact
kind carries a COMPLETENESS CHECKLIST (vertical-owned vocabulary; the schema's keys plus a few artifact-only
items). The gap read is the model's (meaning), the gate is code's (which items are required):

| Artifact | Checklist items (required in bold) | Where an answer goes |
|---|---|---|
| Profile | **current title / role**, **level**, **years**, **field / function**, **top skills (≥ 3)**, **location + work-mode wants**, **comp expectation (band)**, target level, target company types, must-avoid (industries, on-site), notable work (repos / papers / talks), authorization | facts → the profile record (`parsed_profile`), wants → the contract (comp → `must comp ≥ band` or `prefer`; level target → `center`; work mode / geo → `must`) |
| JD | **title**, **level**, **location + work mode**, **must-have skills (≥ 3)**, **responsibilities (≥ 2)**, **comp range or "not disclosed"**, team / reporting line, nice-to-haves, evidence wanted (repos / papers), disqualifiers, company context (stage / type) | the JD text and its `structured` facets → the candidate-search contract |

Flow: after ARTIFACT, one model read returns `{present: {item: value}, missing: [items], weak: [items]}` over
the artifact text (a résumé's "5 years at Acme, backend" fills years / field; a one-line JD leaves most items
missing). Missing REQUIRED items are asked first, one at a time, options from counts where the item is a
schema key (comp bands, levels, metros) and free text otherwise; ask budget for artifact gaps: ≤ 4, then the
contract questions (§3) with their own ≤ 2 + 2. "Search now" always ends it. Weak items are not asked — they
show on the ready card as "you could add …".

Salary: for a job seeker the comp expectation is a REQUIRED want (band chips from the jobs `comp` counts,
"prefer not to say" allowed → no constraint); for a hiring manager the comp range is REQUIRED for the JD (a
range, or "not disclosed" — the peers' market signal is shown beside the question, labeled as signal).

### 2.2 Improve and save — a better résumé or JD, kept on the account

At READY the intake offers, never imposes: "Want a fuller version of your résumé / JD with what you told me?
I'll save it to your account." One model call rewrites the artifact in the vertical's template using ONLY the
original text plus the user's answers — nothing invented; each added line is marked "from this conversation"
in a diff view the user reads before saving. Saved as `roster_brief` rows:

`roster_brief`: `id, user_id, kind (jd | resume), title, role_key, text, structured jsonb (contract +
extracted facets + checklist state), sources jsonb (peer posting ids + per-line support, or "conversation"),
version int, active bool, created_at, updated_at`.

- **Résumé:** the improved text becomes the Apply profile's résumé on file when the user says so (the
  original is kept as version 1; `roster_candidate_profile` points at the active version). Job search and 🚀
  Apply read the active one.
- **Multiple JDs:** a hiring manager keeps one row per kind of hire. `role_key` = `<field>/<function>/<level>/
  <slug(title)>` (from the JD's own facets), unique with `user_id`; a new JD with the same key becomes the next
  version of that row, a different key is a new hire. The intake ASKS which JD when the account holds more
  than one ("Is this the senior payments backend hire, the ML platform lead, or a new role?"), lists them with
  their pool sizes, and the chosen one is the artifact. Saved Talent Maps carry `brief_id`; the JD card in
  Account lists each JD with its maps and a "find candidates" button.

v1: overwrite = new version; no branching.

---

## 3. The state machine (code-owned)

```
DIRECTION  job (I am looking) | candidate (I am hiring)          — "both" = one direction now, the other offered after
ARTIFACT   required artifact present and parsed?  missing → collect (attach / describe / paste / draft);
           several JDs on the account → ask which (or "a new role")
GAPS       completeness read (§2.1) → missing REQUIRED checklist items asked one at a time, ≤ 4
COMPILE    artifact + answers + words → Contract (compile_contract, cached) ; counts over the must-slice via store.counts
           (_facet_nav) — NEVER evaluate(): that runs the embedding + semantic leg; counts need no rows
REQUIRED   still-unknown required keys → one question each (options = the top counts), ≤ 2
OPTIONAL   spread dimensions → one question each, ≤ 2, only if the budget remains
READY      ratification card: understood · contract chips · pool size · artifact · advice · one button per search
           · the offer to save an improved résumé / JD (§2.2)
```

`/intake/step` → `{stage, message, question?: {key, options[], free_text: true}, understood, contract, counts,
pool, artifact: {kind, status}, intake_id?, ready: bool}`. One model call per turn (DeepSeek JSON, ≈ $0.001):
given persona, stage, the last message, the contract so far → `{direction?, answers{key: value}, wants{},
free_text, question_words}`; code applies `answers` via the kernel `edit`, re-runs counts, picks the next gap.
Fail-safe: a model error at any stage yields READY with what is known (never a fake ready on an HTTP error —
the FE shows the error and a direct search button). Failure states enumerated and tested: parser failure,
no résumé, bad JD URL, sparse pool (< 5), counts unavailable, model JSON invalid.

Ask gates (kernel, pure): `spread(counts[key]) = 1 − max_share(known values)`; `worth_asking` iff spread ≥ 0.35,
known share ≥ 0.5, key unconstrained, budget > 0. Required keys bypass spread entirely.

Cost of counts (code review): each answer edits `must`, so the 10-minute counts cache misses on every
question; `store.counts` re-materializes the slice CTE 3+ times. Budget ONE uncached counts call per
question (≈ 1–4 s on people); render the question immediately and let the option counts arrive async.
The Contract also carries `limit` and `angles`; the intake sets `limit` (60) and never `angles`.

---

## 4. Drafting a JD from the centre of peer postings (v1.1 — after the intake core ships)

Owner ask kept; method changed on the panel's advice (no bulk body reads, peer-matched, cheap):

1. Compile the rough role → job contract; evaluate (kind=job) with `company_type` / `company_stage` PREFER
   set to the user's own (when known) → the nearest 40 open postings; dedupe by company; keep the top 10 by
   similarity = the PEERS. (The evaluator's rows carry id / title / url, not the body — the summaries are
   fetched by url via `rs_job_summary`, and a body, when a summary must be built, by `SELECT body FROM rs_job
   WHERE id = ANY(...)`, sliced like `job_extraction_item`.)
2. Requirement lines come from the per-posting SUMMARY the product already keeps (`rs_job_summary`:
   `key_requirements`, `nice_to_have`, `responsibilities`, `seniority`, `work_mode`, `compensation` — built by
   `build_job_summary`, verified against the posting text, cached 7 days). Missing summaries are built for
   the peers (≈ $0.002 each; they serve the job cards afterwards).
3. One small model call GROUPS equivalent requirement lines across the peers (meaning, not string matching);
   a group that appears in ≥ 30 % of peers is the CENTRE, the rest is "some peers also ask". Comp band and
   work mode come from the peers' facet counts, labeled market signal.
4. One model call drafts the JD in the vertical's template from the centre + the user's context (team,
   stack, location, comp, must-haves they named). Every requirement line carries its support ("asked by 6 of
   10 peer postings" or "you said"). No uncited line.
5. The draft is read-only text in the thread; changes are asked for in words ("make it staff level, add
   Kafka") and re-drafted. SAVE is an explicit tap (no silent save, no silent search from a draft); the
   candidate-search contract is compiled from the SAVED text, and the ready card shows the pool before the
   search runs (an edit that empties the pool is visible before it costs anything).

Spend: ≈ $0.01 per draft plus summaries for peers not yet summarized. Latency: ≈ 3–5 s with cached summaries.
Sparse pool (< 5 peers): say so and draft from the user's answers alone, every line "you said".

---

## 5. Hand-off and navigation advice

READY card: direction; artifact line (résumé on file / JD saved); "what I understood" (plain); the contract as
chips (the rail's glyphs: ● must ▲ prefer); pool size; and ADVICE. Advice is code-derived from the counts and
phrased by the model in one line each:

- the 2–3 rail dimensions that split this pool most ("level and company type split this pool — use those
  chips first"); the unknown share to expect ("years unknown for 70 %, don't filter on it").
- job search: the résumé-fit path (🚀 Apply) and keep-fresh ("save the Job Map, set weekly").
- candidate search: the evidence gate ("must have repos / papers") and the review states.

The same advice line renders ON THE RESULT'S RAIL after the search ("this pool splits most on …"), computed
from the actual counts — the panel's point that pre-result advice is generic. One button per search runs the
ordinary turn with the contract attached; "search the other side" is offered after a result (a JD → find
candidates; a profile → find roles), never as a parallel first-class flow.

---

## 6. UI

Re-enable the hidden "◍ Guided" mode (`apps/web/index.html`: the `triage` mode button, `#triagebody`,
`renderTriage` / `submitTriage` / the ready card) and re-point it — not a full chat revival: question turns
render OPTION CHIPS (tap = answer; free text allowed); the artifact stage shows attach / "describe yourself" /
"paste a JD" / "draft a JD"; the draft renders as read-only text with support counts per line and a Save tap;
the ready card's route buttons are Talent Map / Jobs. Voice stays off. Mobile: chips wrap, the composer stays
docked, buttons full-width, verified at 390 px.

Entry points: the Guided tab; the first-run nudge gains "Guided search"; an empty Talent Map / Jobs box shows
"Not sure how to phrase it? Guided".

---

## 7. Tests (written first)

- kernel: `spread`, `worth_asking` (thresholds; constrained keys never asked; unknown-heavy keys never asked;
  required keys asked regardless of spread), `next_gap` order (artifact gaps → required → optional), budgets,
  `search_now` forces READY; the checklist gate is generic (a list of required item names the caller supplies).
- completeness: a terse résumé ("SWE, 5 yrs") reports years present, skills / location / comp missing; a
  one-line JD reports title present and level / location / must-haves / comp missing; an answer fills the item
  and the contract; "prefer not to say" on comp leaves no constraint; weak items never become questions.
- improve & save: the rewritten artifact contains no fact absent from the original text + the answers (a
  trap case: a résumé with no employer must not gain one); versions increment; `role_key` derives from the
  JD's facets; two JDs with different keys coexist; the same key becomes a new version; the intake asks which
  JD when the account holds more than one; a Talent Map saved from an intake carries `brief_id`.
- vertical: required keys per direction exist in the schema; question words + option labels for every
  navigable key; the JD template sections; prompts name only schema keys.
- app: `/intake/step` transitions with a fake model (direction → artifact → required q → optional q → ready);
  résumé on file → READY in one turn; a self-description becomes a profile; a model error → READY with what is
  known; HTTP error path; `roster_intake` round-trip; hand-off contract validates; the transcript rides the
  result turn as an audit field (capped, stripped from shared maps).
- JD centre (v1.1): peers deduped by company; a centre line cites ≥ 30 % of peers; no uncited line; a sparse
  pool drafts from answers only; save → compile → pool shown; a draft is never saved without the tap.
- eval harness: simulated persona users (job seeker with / without résumé; recruiter with / without JD; a
  vague founder) — over-ask rate, confabulation (facets the user never said), boundary (advice about the
  person), artifact recall, and "would this question have changed the results".
- FE: `node --check` + a Playwright pass at 390 px through a full intake.

---

## 8. Non-goals (v1)

No résumé tailoring per posting here (that stays in 🚀 Apply); no JD posting to boards; no outreach; no multi-user intake; no voice; no inline JD editor (words → re-draft);
no automatic re-centring of a saved JD (on demand later); no "both" as a parallel flow.

---

## 9. Delivery order

1. **v1 intake core** (no spend beyond ≈ $0.001 / turn): kernel gates + tests → vertical intake vocabulary →
   `/intake/step` with fake-model tests → FE Guided re-point (chips, artifact stage, ready card) → session
   row → audit field → prod smoke (throwaway accounts) at desktop and 390 px.
2. **v1.1 JD draft** from peer summaries → save → candidate search from the saved text; improve-and-save for
   both artifacts; multiple JDs keyed by `role_key` with the "which JD" question.
3. **v1.2 advice on the rail** (counts-derived line on every result) and the "search the other side" offer.
4. Eval harness with persona users; then the first Talent turn moves to compile → evaluate (after the people
   re-extraction lands — separate track).

---

## 10. Panel verdict (2026-09-05)

**Codex** — (1) FE-only state breaks once artifacts exist → server-owned session after an artifact (ADOPTED).
(2) Spread is not decision value: a mostly-senior slice would never ask staff vs senior; geo mostly unknown
would never be asked → fixed REQUIRED keys per direction, spread only ranks optional asks (ADOPTED). (3) The
centre can launder generic market noise (payments lead → "AWS, Python") → peer-matched pool, dedupe by
company, share-based recurrence, the user's must-haves always on top (ADOPTED); cut JD drafting from the
first release (ADOPTED as v1.1, kept because the owner asked for it explicitly). Also: artifact QUALITY gate,
explicit approval before save / search from a draft, failure states, "both" as an after-offer, advice after
real counts, a visible search strategy (the contract chips + pool) — all ADOPTED. Keep the intake compile a
separate front door until the people re-extraction lands (ADOPTED).

**Gemini** — (1) A 25-body extraction call is a 15–30 s latency trap → use the postings' pre-extracted
facets and cached summaries, N=10, ≥ 30 % (ADOPTED — `rs_job_summary` already holds requirement lines).
(2) Spread ignores intent: sparse-but-dealbreaker dimensions → required keys (ADOPTED). (3) Re-sending
attachments per turn explodes payloads → parse once, reference by id (ADOPTED). Missing: edit → contract
sync (ADOPTED: pool shown from the saved text before search), preferences vs facts for job seekers (ADOPTED
§0.3), a no-match fallback for the centre (ADOPTED). Cut the inline editor (ADOPTED: words → re-draft); cut the
brief table (NOT adopted: the owner asked to save the JD; kept minimal). Carry over ratification (ADOPTED).

**Code-grounded agent** — corrections ADOPTED: there is no server triage to re-point (`app.py` has no
`/triage/step`; `/config` hard-codes `triage_enabled: False`); "counts only" must be `store.counts` /
`_facet_nav`, never `evaluate()` (which embeds); the semantic leg returns no posting body; the Contract also
has `limit` / `angles`. Cheapest path confirmed: DIRECTION / ARTIFACT reuse `is_self_description` + `get_parse`
+ the JD fetch / `build_jd_brief`; CONTRACT reuses the `/search/compile` handler; GAPS reuse `_facet_nav`;
hand-off: the Talent path already accepts a contract, Jobs compiles on its own. Risks logged: one uncached
counts call per question (budgeted above); `closed_at` / `facets` DDL lives only in `scripts/ingest_jobs.py`
while the store queries them (fix: move that DDL into the store's `ensure_schema` — a small prerequisite);
`setMode("triage")` is coupled to the `#triage` hash, voice, `PENDING_INTAKE_TRANSCRIPT` and the qa / panel
routes — the hand-off must go through `setSearchMode`'s per-tab thread swap so the result lands in the right
tab's thread.

**Synthesized call.** Build v1 as the intake core only (direction → artifact → required questions → ≤ 2
optional → ratify → hand-off), server-owned once an artifact exists, on the existing compile / counts /
contract path; JD drafting follows as v1.1 on cached peer summaries; navigation advice is a counts-derived
line on the rail. Prerequisite chore: move the `rs_job` facet columns' DDL into the store.

---

## 11. Status (2026-09-05, end of session) — START HERE next session

> 2026-09-06: v1 + v1.1 + §12 step 1 live; intake battery 18/18 (see §12.13 for the latest fixes). Next: §12 step 2.

- **v1 intake core LIVE** on prod behind `ROSTER_GUIDED_INTAKE=1` (needs `ROSTER_FACET_EVALUATOR=1`): the ◍ Guided
  TAB (first, beside Talent Map / Jobs Map / Q&A — never a separate mode bar), `POST /intake/step` (stateless;
  `state` rides the request; artifact text capped at 6k), kernel gates `roster_kernel/facets/intake.py` (9 tests),
  vertical vocabulary `roster_vertical/intake.py` (6 tests), service `apps/api/intake.py` (10 tests incl. the
  endpoint and the contract hand-off). Verified on prod at 390 px and 1280 px: job seeker (self-description →
  level / field / location / comp chips → READY → Jobs Map with 20 cards + rail) and hiring manager (pasted JD →
  level / comp / evidence → READY → Talent Map with cards + rail).
- Hand-off: `ResearchIn.contract` — `/jobs` runs a ratified contract as is; the People surface with a contract
  runs the evaluator (`_people_contract_route`) instead of the old engine. The intake never constrains `company`;
  for a job seeker only field / geo / work mode / employment / comp may be musts (résumé facts rank).
- Direction read is its own prompt with unambiguous tokens (`looking` / `hiring`) — "candidate" meant both.
- Ops findings this session: DeepSeek returned HTTP 402 (balance exhausted) → `api/model_json.py` is the ONE
  strict-JSON call (DeepSeek → OpenAI gpt-4o-mini on a provider error, 10-min cooldown after 402 / 401); the
  people re-extraction had been failing 100/100 batches silently and resumed after the fix. Postgres: the must
  clauses correlate on `('job:' || j.id::text)` — the expression index `ix_rs_job_facet_eid` (now in the store
  DDL) took counts from 30 s to 0.1 s. Small must-slices take an exact-distance semantic path (probe ≤ 2000).
- **v1.1 LIVE (later the same day):** JD DRAFT from the centre of peer postings (`apps/api/jd_draft.py`: peers = the
  role's similarity neighbourhood — only `field` filters — deduped by company, 10 kept; requirement lines from
  `rs_job_summary` (built on demand for peers without one); one model call groups equivalent lines; centre = groups
  in ≥ 30 % of peers; one model call drafts; every line cites a group or "you"; uncited lines are dropped; market
  signal from the peers' facets; changes in words → `redraft`; "Use this JD" / "Save to my account & use").
  SAVED BRIEFS: `roster_brief` (kind jd | resume, `role_key` = field/function/level/title-slug from the JD's own
  facets; the same key = next version; `GET/POST/DELETE /me/briefs`; Account → "Saved briefs" card with "Find
  candidates" that opens Guided on that JD; the intake asks "Which hire is this for?" when the account holds JDs).
  IMPROVE-AND-SAVE: `POST /intake/improve` (original + answers only; added lines verified to appear in the text
  and highlighted; Save → a brief; a résumé also becomes the résumé on file via `set_resume_text`). MULTI-SELECT
  chips (owner): chips toggle, "Answer with N ✓" sends a list; the service accepts lists for items and keys.
  Direction read returns null for a bare title list (asks). A pasted JD's > 2 skill musts rank instead of AND-filter.
  The first-run nudge respects a résumé on file and never fires on an expired session. Verified on prod
  (signed-out browser flows at 1280 / 390; briefs + improve + résumé-on-file via a throwaway account, deleted).
- **Owner report (session #b08fa55c, 2026-09-05): "Guided throws everything into the jobs search; results all over
  the place."** Root cause: the contract's semantic `text` was the whole artifact (the résumé). Fix: the search text
  is the conversation's INTENT — the user's opening words + the answers (role / title, field, skills, specialty,
  location), ≤ 400 chars, never the artifact body (`IntakeService.intent_text`, recomputed at READY); the artifact
  only feeds the checklist. Level from the conversation CENTRES the ranking (`center: level`, span 1) instead of
  filtering, per the standing "level is a preference" rule. Multi-select UX: a chip tap stages, "Answer ✓" sends.
- **Eval set (owner ask, 2026-09-05): `evals/intake/`** — `scenarios.jsonl` (15 persona-scripted conversations:
  seekers with / without a résumé, terse and rich, ambiguous title list, "Founder in Residence", search-now early,
  decline everything; hiring managers with a full / thin / linked JD, saved JDs, a rich opening → draft, a thin
  opening + "Keep going", multi-select musts; impossible musts; improve-and-save) and `run_intake_eval.py`, which
  drives `/intake/step` on a live API and scores CONTENT: direction, artifact path, over-asking / repeats, items
  the artifact stated are never asked, intent text (≤ 400 chars, opening words in, artifact body out), level
  centres, no company constraint, skill-must cap, draft title / citations / peer relevance / redraft, empty-result
  diagnosis, hand-off rows (strict for hiring flows), latency caps, improve text. Seeded scenarios use
  `@roster.test` throwaways (their user rows are swept in the DB afterwards). ≈ $0.15 / run; `runs/` keeps the
  record. Findings it produced the same day: the CTO draft took 316 s (sequential peer summaries → concurrent,
  53 s) and handed off 0 candidates (function=engineering must; evidence as a required must) → function ranks,
  evidence is OPTIONAL for hiring; "Keep going" drafted recruiter postings → a role text that compiles to no
  field / role reads no peers and re-asks.
- **Ranking rule (owner report: "none of the results are relevant"):** with text, RELEVANCE is the primary axis —
  5-point bands of the calibrated match; prefers and the centre reorder WITHIN a band; an avoid lowers the band.
  Weak results (best match < 45 %) are flagged and diagnosed like empty ones, with the best match WITHOUT each
  must, so the user sees which filter keeps the closer people out (a Bay Area + manager CRM search: dropping
  the metro raises the best match from 43 % to 65 %).
- NOT yet built: server-held intake sessions / `roster_intake` (state is FE-held; attachments are parsed per turn
  they are sent), advice on the result's rail (§5), the "search the other side" offer, the persona eval harness
  (§7), peer-matching by the manager's company type (§4 step 1 `prefer`), a JD-draft entry from the Account card.
  People evaluate with musts ≈ 10 s (the person semantic leg) — next perf item.

---

## 12. Contract SEARCH — choosing the combination of constraints by measuring, not guessing (v3, 2026-09-06)

Owner: "Guided does a brute-force job at mapping intent to a search. The hard part is WHAT COMBINATION of flags
gives the most relevant results — not guessing and throwing it at search. This is the smartest part of the
product — panel it, think in detail." Two panel rounds (Codex, Gemini) and a code-grounded feasibility read
shaped this version; their verdicts are in 12.12.

### 12.1 The problem, precisely

A brief maps onto the schema in several defensible ways (a READING): "marketing engineering manager, CRM,
Salesforce" is {field: software, specialty: crm, work_type: manager} or {field: marketing}; "remote US or
Seattle" is a place-or-mode disjunction the contract grammar cannot AND (within a key OR, across keys AND).
For a given reading, each named dimension can FILTER or RANK (a POLICY). Which (reading, policy) finds the
relevant people depends on what the index holds and how it was extracted — only the index can tell. Today one
model call picks both, blind to the index, by fixed rules.

### 12.2 Three inputs, kept apart

- **U — the user's own constraints**: chips set to must, answers to questions, rail edits. Derived from the
  intake's `answers` / `asked` (no metadata on the kernel contract). Inviolable in every RANKED candidate;
  DIAGNOSED when it hurts (12.6): the card can say "your Seattle filter leaves 3 people; without it 412".
- **R — readings of the brief**: model readings (up to three distinct mappings where the brief is ambiguous,
  returned by the same compile call) and DATA readings from co-occurrence (12.4). Data readings PROPOSE; they
  never overwrite a model reading.
- **P — policies**: `strict` (every named dimension a must; level stays a centre per the product rule),
  `default` (today's rules), `loose` (only U must; everything else ranks), `relaxed` (strict minus the
  collapsing musts — 12.4.2).

Candidates C(r, p) ∪ U are generated AFTER the post-compile edits (company dropped, skill cap, function → prefer
for hiring), capped at 9 before probing. One extra EMERGENCY candidate `u_relaxed` (U's most collapsing must
demoted) is probed only when every U-honouring candidate is below the floor; it is shown as an alternative with
its reason, never auto-picked.

### 12.3 The pipeline

    brief + answers + U
      → readings R (compile call returns ≤ 3; + co-occurrence readings)
      → candidates C(r, p) ∪ U (≤ 9)
      → STRUCTURAL PROBES (code, bounded slice-size counts, tens of ms each, concurrent) → viability, pool
      → survivors (≤ 5) evaluated top-20 → ONE blind judge over the normalized UNION (one batched profile fetch)
      → SELECT (12.6) → READY card: pick + numbers, alternatives one tap away, U diagnostics
      → after hand-off: rail edits within 10 min are labelled misses (learning log)

### 12.4 Structural probes (code, deterministic, cheap)

1. **Pool per candidate** — a LIMIT-bounded slice size (`slice_size(kind, must, cap=2001)`, the existing
   small-slice probe made public): tens of ms with the per-key index; "≥ 2001" is enough to know a pool is
   large. Full `counts` run only for the winner (the ready card needs them anyway).
2. **Marginal drop → `relaxed`** — `diagnose_musts` on the strict candidate. A compiled (not U) must is
   "collapsing" and demoted to prefer only when ALL hold: removing it multiplies the pool by ≥ 5×, the pool with
   it is below 1,000 (absolute scarcity — a drop from 100k to 20k is not a collapse), and the key is one of the
   RELAXABLE kinds: a place named with a mode, a low-coverage key for the kind (known share < 50 % in the index,
   e.g. people `years`, `work_type` until the re-extraction lands), or a `set` key (skill / specialty). A rare
   but decisive must on a well-covered categorical key (`field`, `level` centre, `evidence`) is never demoted by
   this rule — it may be the target ("compiler engineers" is supposed to be small).
3. **Co-occurrence readings** — for each specialty / skill value the brief names (≤ 3): one counts call with
   must={that value} → the `field` (and `function`, `work_type`) distribution among carriers. A field spawns a
   data reading {field: f, specialty|skill: v} when its share ≥ 25 % AND its LIFT over the index background is
   ≥ 1.5 AND support ≥ 20 entities (a value that lives 45 / 45 in two fields yields two readings; a field that is
   merely the index's majority does not qualify). Data readings carry their numbers onto the card ("crm lives
   62 % in software, 21 % in marketing").
4. **Disjunction rule** — a place named alongside a mode ("remote or Seattle") becomes a metro PREFER for people
   (no work-mode key exists for people); for jobs it becomes TWO readings (metro must; work_mode=remote +
   country must) that compete like any others. U overrides (a metro chip the user set to must stays a must).

Cost: ≤ 9 slice sizes + ≤ 3 co-occurrence counts + one marginal-drop set, concurrent, cached: < 1 s typical.

### 12.5 The judge (model, meaning, once)

- Input: the BRIEF (the user's words + answers; never a contract) and the UNION of the survivors' top-20 rows,
  de-duplicated, shuffled, blind to source, one normalized shape per row for every row: `{id, role line, company,
  field · function · level · work_type · metro, skills/specialties ≤ 5, snippet ≤ 140 chars, evidence kind}`;
  missing values shown as "—". People rows get their role line / snippet from ONE batched profile fetch for the
  whole union (they carry only facets otherwise). Evidence kind is shown so a "yes" can be weighed (12.6).
- Output per row: `{fit: yes | partial | no, why ≤ 8 words}`. ≈ 30–45 rows ≈ 3–4k tokens ≈ $0.003, ≈ 1.5 s.
- Candidate scores (code): `prec10` = Σ w(row) over its top 10 / 10 where yes = 1, partial = 0.4 (inflation
  guard), and a yes whose row carries only self-stated / no evidence counts 0.8; `prec3` likewise on its top 3;
  `missed` = judged-yes rows in the union absent from its top 20.

### 12.6 Merge, don't pick (owner, 2026-09-06: "different recipes are each partially good — pick the best results
across recipes and rank them together with an impartial judge")

Selection of ONE recipe is replaced by FUSION of the survivors:
1. **Union** the survivors' top-K (K = 60) — every row remembers which recipes surfaced it and at what rank.
2. **Fuse** by reciprocal rank fusion (RRF, k = 60) across recipes — code, no model, parameter-light; a row
   several recipes agree on rises; a row only a loose recipe found sits lower. Recipe weights are equal in v1
   (a recipe's judged head precision may weight it later).
3. **Judge the head**: the top 40 of the fused list graded blind against the BRIEF (12.5's protocol and row
   shape). Order = fits, then partial fits, then the ungraded tail in fused order; rows judged "no" fall below
   the tail with a note. The card shows the verdict as a labelled model read ("reads as a fit — model"), never as
   a fact, and shows WHY the row surfaced (which readings; the user's own chips).
4. **The rail over the merged pool**: counts run over the union of the recipes' must-slices; the user's chips
   are the SHARED layer every recipe honours (a chip edit re-runs the fusion); readings appear as toggles
   ("software + CRM", "marketing", "strict") the user can switch off — replacing "switch to an alternative".
5. **Recall guard and WEAK** become simpler: nothing is thrown away, so six perfect people and two thousand good
   ones coexist in order; WEAK = fewer than 3 judged fits in the head → say so and ask one clarifying question.

Why this beats picking: no recipe has to be right on its own; the judge does the one thing only it can (fit to
the brief), on the rows where it matters (the head); the tail is honest about being ungraded.

### 12.7 Ratification, U diagnostics, learning

READY card: the readings that fed the merge, one line each with their numbers ("software + CRM — 312 people ·
marketing — 9 · strict — 3"), each a toggle; the merged head's fit count ("31 of 40 graded fit"); a U line when U
hurts: "your must 'evidence: repos' leaves 17 — without it 412 (best match 65 %)". Learning log
(`roster_intake_choice`, additive): brief hash, candidates with numbers, the pick, judge verdict counts, and any
rail edit within 10 min of the hand-off (a labelled miss). v1 reads the log only in the eval report.

### 12.8 Where it lives

| Piece | Where | Domain words? |
|---|---|---|
| Ladder over opaque keys (strict / default / loose / relaxed / u_relaxed), collapsing-must rule with the thresholds as parameters, lift / support / share for co-occurrence readings, the objective with the recall guard, selection | `packages/kernel/roster_kernel/facets/contract_search.py` (new, pure, tested with the in-memory store) | No |
| Readings compile prompt (≤ 3 mappings), judge prompt + row normalization, the disjunction rule, which keys are named dimensions / relaxable kinds, partial and evidence weights | `packages/vertical_roster/roster_vertical/intake.py` | Yes |
| `slice_size` probe (public), co-occurrence counts, survivors' evaluates, batched profile fetch, judge call, `ready.alternatives` + U diagnostics, learning log | `apps/api/contract_search.py` (new) + `apps/api/facet_store.py` + `apps/api/intake.py` | neutral |
| Ready card alternatives + switch, U line, miss logging on rail edits | `apps/web/index.html` | — |

Two caches exist (the store's counts cache and `_facet_nav`'s) — the probes use the store's directly.

### 12.9 Budget, latency, gates

Probes < 1 s; survivors' evaluates (≤ 5, concurrent) ≈ 1–3 s; one judge ≈ 1.5 s; profile fetch ≈ 0.1 s. Target
p90 ≤ 5 s added at READY, probes started while the last question is being answered. Spend ≈ $0.003–0.005 per
intake. Behind `ROSTER_INTAKE_CONTRACT_SEARCH=1`, on by default only after 12.10's gate.

### 12.10 Evaluation — paired, blind, with a golden union

- PAIRED per scenario: BASELINE (single compile) vs SELECTED; union of both top-20s judged blind against the
  brief; report prec10 / prec20 per arm, the lift, `missed obvious relevant`, and the judge's yes/partial/no rates.
- GOLDEN UNION: a frozen brief with 20 frozen rows and human labels (the owner labels once), run every eval;
  judge agreement with the labels tracked; a > 5-point shift = judge drift, fail the run. Adversarial rows in
  the golden set: wrong company, wrong level, adjacent specialty, stale role, keyword-only false positive; row
  order shuffled per run to catch position bias.
- SCENARIOS: grow the set from 15 to ≥ 30 with the failure classes seen: field-vs-specialty ambiguity,
  place-or-mode disjunction, a title that is also an employer name ("Salesforce"), a JD whose must-haves are
  all skills, sparse profiles (no level / no geo), a decisive rare must ("compiler engineers").
- GATE to turn the flag on: selected ≥ baseline on ≥ 80 % of scenarios, no scenario worse by > 0.10 prec10,
  `missed obvious relevant` ≈ 0 for the selected arm, no WEAK-with-false-confidence case, p90 ≤ 5 s, and every
  winner flip reviewed by hand once.

### 12.11 Known limits (v1 of contract search)

The judge grades only rows some candidate surfaced (recall beyond the candidate set is the probes' job, not the
judge's); rich profiles still read better than sparse ones despite normalization (the evidence weight and the
golden set's sparse rows watch this); one model family compiles and judges (agreement bias — the golden union's
human labels are the independent check); the semantic text itself is not varied in this version (a separate,
controlled experiment — mixing it into the ladder hides which variable moved the result).

### 12.12 Panel verdicts (v1 → v3)

Codex r1: index-driven candidates, precision-first, blind union judge, normalized rows, paired eval — ADOPTED.
Gemini r1: the v1 objective would bury users in noise; latency wall; probe at compile time; structured judge rows
— ADOPTED (code probes, not a model tool loop). Codex r2: precision theater over a ranker-shaped pool → the
`missed` penalty and the probes-own-recall stance; facet absence as negative evidence → relaxable = low-coverage
keys, never decisive well-covered ones; U can be wrong → U diagnostics on the card; pool ≥ 5 too weak → recall
guard; WEAK must not auto-pick strictest → pick stands, alternatives shown, one clarifying question when the
head is poor; evidence-weighted yes; lift-over-background and minimum support for co-occurrence; sentinel golden
set with adversarial rows; ≥ 30 scenarios and hand review of winner flips — all ADOPTED. Gemini r2: emergency
U-relaxed candidate → ADOPTED as an alternative, never auto-picked; recall trap → the ≥ 20× / 0.10 guard;
marginal drop needs absolute scarcity (< 1,000) → ADOPTED; bimodal co-occurrence → ≥ 25 % share spawns a reading
→ ADOPTED; daily golden union for drift → ADOPTED; ship step 1 alone → ADOPTED. Code-grounded: bounded
`slice_size` probe (tens of ms); U from `answers` / `asked`; people judge needs one batched profile fetch;
jobs disjunction = two readings; ladder after post-compile edits; two caches — all folded in.

### 12.13 Delivery order

Status 2026-09-06: **step 1 LIVE** — `apps/api/contract_search.py` (`index_aware`), kernel
`facets/contract_search.py` (collapse under the viability floor of 20, co-occurrence share / lift / support, RRF
fusion ready for step 2), `FacetSQLStore.slice_size` (bounded probe), the compile's side readings
(`place_or_mode`, `work_mode`), wired into the Guided intake (at compile and again at READY with the user's own
keys), the plain Talent brief and the plain Jobs brief; the ready card shows "Adjusted against the index" and
"Your own filters". A remote role is never asked for a metro and "remote US" means the country. Eval scenarios:
`hiring_place_or_mode`, `hiring_field_vs_specialty`, `hiring_decisive_rare_must` (all passing). Step 2 (fusion +
blind judge + alternatives) not started.

Later 2026-09-06 (battery **18/18**, run `evals/intake/runs/intake-20260906-103324.json`): (a) JD-draft peers share
the role's DOMAIN and its TIER — `field` and the stated `level` filter (fallback to field only under 5 peers); a
preferred work type that contradicts the tier is dropped (`roster_vertical.intake.peer_work_types`); a draft with
no "you" line is asked once more with the omission named (a CTO draft had read IC infra postings). (b) What the
artifact STATES reaches the contract as a schema TOKEN: the completeness read returns `tokens` per stated
closed-vocabulary item and that token replaces the compile's read for the key (`field` excepted — the index-aware
step owns it); non-answer words ("missing", "not stated") are never an answer nor search text. (c) A remote role
that is also place-or-mode leaves the metro to the index-aware step, and the place-or-mode reading is noted even
when the compile already left the place under prefer. Throwaway accounts: `evals/intake/sweep_throwaways.py`
(in-container).

1. **Index-aware compile (deterministic, no model, no flag)** — the disjunction rule, collapsing-must demotion
   with the 12.4.2 conditions, co-occurrence readings (the best-lift data reading replaces a model field only when
   the model's field has < 10 % support among the specialty's carriers; otherwise both become alternatives on the
   card), U diagnostics on the ready card. Eval scenarios for each class. Ships first, alone.
2. **Contract search behind the flag** — ladder + probes + survivors + blind union judge + selection + ready-card
   alternatives + learning log; paired eval + golden union; flag on when the gate passes.
3. **Learning** — read the log; tune default policy and the readings prompt; a small per-field prior for which
   dimensions filter, learned from labelled misses.
