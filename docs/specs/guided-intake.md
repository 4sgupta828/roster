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
- NOT yet built: server-held intake sessions / `roster_intake` (state is FE-held; attachments are parsed per turn
  they are sent), advice on the result's rail (§5), the "search the other side" offer, the persona eval harness
  (§7), peer-matching by the manager's company type (§4 step 1 `prefer`), a JD-draft entry from the Account card.
  People evaluate with musts ≈ 10 s (the person semantic leg) — next perf item.
