# Implementation plan: recruiter workflows (branch `recruiter-workflows`)

Status: ACTIVE. Date: 2026-09-03. Spec under review: `recruiter-workflows.md` (companions:
`talent-intelligence-redesign.md`, `evidence-model-v2.md`). Work happens on the
`recruiter-workflows` branch and deploys to prod from that branch; `main` merges once stable.

## 1. Critical review of the spec

The thesis is right and it is the thing the current product is already best at: the evidence
layer (typed claims, span-checked artifacts, coverage honesty) is what nobody else ships. The
spec's largest risk is not the direction but under-crediting what exists and over-specifying
machinery for workflows no user has asked for yet. Point by point:

1. **P0 is ~70% built; the spec should say so and name the deltas.** Talent Map + evidence
   panel + saved maps + private links + CSV + coverage statements + review states all exist and
   are live. The real P0 gaps are: (a) the *interpreted brief* is only visible as facet chips —
   hard filters and ranking preferences are not distinguished; (b) clarification is absent for
   population searches (it exists only for single-person lookup); (c) the brief is not stored as
   a first-class contract with the map; (d) review state was just moved off the card (the panel
   no longer has it) and the vocabulary (`shortlisted / needs more evidence / reviewed`) does
   not match the spec (`shortlist / maybe / needs more evidence / not relevant`).
2. **Clarification must never block.** Agreed with the spec's "bad clarification" list, and
   stronger: every clarifying question ships *with* a first map built under stated assumptions.
   The current single-person flow already does this well; population search should copy it.
3. **"Not relevant" is fine; "reject" is not.** Keep the spec's vocabulary. Note the spec bans
   "score" language while the product shows `match_pct` on semantic-first cards. That number is
   a *search signal* and the panel says so; keep it, but it must never be the lead element.
   Rename its tooltip to "wording match to the brief", never "fit".
4. **Hiring-manager calibration needs an identity model the spec skips.** Feedback needs a
   `reviewer_id`; a private link is read-only and anonymous. Proposal: a *named reviewer* on the
   share link (the reviewer types a display name once; stored per browser + map, no account),
   feedback rows carry `reviewer_name` + a per-browser reviewer key. Owners see who said what.
   Account-backed reviewers come later.
5. **Feedback → revised map must be explainable, not magic.** The spec's tags map cleanly to
   *code-owned* edits of the search contract (`wrong_seniority` → drop/raise the seniority
   constraint; `wrong_company_target` → remove that company from targets; `needs_artifact_evidence`
   → add an evidence requirement; `more_like_this` / `less_like_this` → positive/negative
   example rows that bias the semantic query). No LLM decides what the feedback *means*; the LLM
   may only phrase the "what changed" summary from a code-computed diff.
6. **Dossier already exists in two weaker forms** (the Q&A "web-grounded dossier" hop and the
   inline evidence panel). The spec's dossier should be the evidence panel *extended*, cached per
   (map, entity), with a short cited narrative — not a third surface. Stale-marking on evidence
   change is right and cheap (hash of the evidence refs).
7. **Outreach prep exists** (the Reach-out composer drafts a message and hands off to the
   person's channels; nothing is sent). The delta is evidence-grounded *angles* with per-line
   evidence refs, and an honest "not enough specific evidence to personalize" state.
8. **Watchlist/refresh is real but the spec underweights cost.** Refresh means re-running the
   search and re-scanning artifacts; the web legs are quota-bound (Brave's monthly limit was
   exhausted today). Refresh must show a projected cost/quota before running and diff snapshots
   (the map-revision table makes the diff trivial). Ship it after calibration, not before.
9. **Jobs is not "consumer job search as the default product".** The spec lists consumer job
   search under "avoid as default". Jobs, Find jobs, Apply Assistant stay as *secondary* tabs
   (they already are). Nothing is removed; Talent Map remains first.
10. **Kernel discipline.** Every table the spec proposes is generic (brief, revision, review,
    dossier). They live in the app layer (`apps/api`), recruiting labels in the vertical/UI.
    Nothing new enters `roster_kernel`.

## 2. What exists today (mapping the spec to code)

| Spec item | Exists? | Where |
|---|---|---|
| Talent Map from a brief | yes | `answer_people_population`, `peopleRowsMarkup` |
| Why surfaced / evidence types / footprint / freshness on rows | yes | card + inline evidence panel |
| Interpreted brief shown | partial (facet chips; relaxed note) | `coverage_basis.query_facets`, `relaxed_from` |
| Hard vs soft distinction | partial (semantic-first = soft; identity = gate; guard) | `people_population.py` |
| Clarification for population briefs | no (only single-person lookup) | `lookup_person` |
| Saved maps, private link, CSV, notes | yes | `apps/api/maps.py`, `/maps/*` |
| Review states | yes (old vocabulary; control currently only via map) | `rs_map_review`, `REVIEW_STATES` |
| Search brief stored with map | partial (`brief` text + `filters`) | `rs_map.brief/filters` |
| Map revisions | no | — |
| Feedback tags | no | — |
| Dossier | partial (Q&A hop; evidence panel) | `qa_router` dossier route, `.ev-inline` |
| Outreach prep | partial (composer, channels) | `#reachmodal`, `channelsFor` |
| Refresh / watchlist | no (pulse infra is eigen-inherited, not wired to maps) | — |
| Selected-row CSV | no (whole map) | `build_csv` |

## 3. Phased plan (each phase ships to prod from the branch, gold eval green before the next)

### Phase 1 — Recruiter intake contract (P0 delta) — THIS BRANCH, FIRST
- `brief_contract` computed in code and returned in `coverage_basis`:
  `{hard: {company, worked_at, country/metro/state, evidence_kinds}, soft: {role, function,
  skill, seniority, industry}, guard_added, relaxed, topic_terms, assumptions}`. Hard = what
  gated the cohort in this run (identity facets, geo, must-have evidence); soft = ranking
  preferences (semantic-first facets, relaxed keys, thin-cohort soft keys).
- UI: an "Interpreted brief" strip at the top of every map: `Must: … · Prefer: … · Scope: …`
  with hard items styled as filters and soft as preferences; "assumptions" (e.g. "seniority not
  stated — all levels") listed inline. Replaces the bare facet chips.
- Clarification-lite: code-owned triggers (seniority absent for a "senior/staff/lead" brief that
  compiled without seniority; ambiguous geography; a hiring company named without exclusion in
  Find candidates) produce ONE question shown *with* the map, never before it; the answer is a
  refinement turn (already supported by `refine_facets`).
- Store the contract with the map: `rs_map.brief_contract jsonb` (new column, additive).
- Eval: gold cases for hard/soft labeling and the assumption line.

### Phase 2 — Review + hiring-manager calibration (P0 review vocabulary + P1)
- Review vocabulary → `unreviewed | shortlist | maybe | needs more evidence | not relevant`
  (old values migrated in read: `shortlisted→shortlist`, `reviewed→maybe`).
- Review control back on the card (compact select in the card footer when a map is active or
  the viewer is the owner/named reviewer), plus feedback tags (spec list) and a note.
- `rs_map_review` additive columns: `tags text[]`, `reviewer_key text`, `reviewer_name text`.
  Reviewer identity via share link: named reviewer, per-browser key.
- Map revisions: `rs_map_revision(map_id, revision_id, reason, brief_snapshot,
  filters_snapshot, coverage_snapshot, row_snapshot, created_at)`; the initial save writes
  revision 0.
- "Revise map from feedback": code maps tags → contract edits; the revised search runs; the
  diff (added/removed/moved rows, contract delta) is computed in code; an LLM phrases a
  two-line "what changed" from the diff only. Saved as a new revision with reason
  `hiring_manager_feedback`.
- Hiring-manager share view: brief + interpreted contract + top rows + evidence distribution +
  feedback controls + notes; admin/ingest controls hidden (already hidden for non-owners).

### Phase 3 — Person dossier
- `rs_dossier(map_id, entity_id, brief_snapshot, sections jsonb, evidence_refs jsonb, gaps,
  evidence_hash, created_at, updated_at)`.
- Sections are code-built from the evidence packet + artifacts (what they say / what employers
  say / what artifacts show / unverified); one LLM call writes a ≤120-word narrative constrained
  to cite `[ref]` ids that exist; uncited sentences are dropped in code. Stale when
  `evidence_hash` changes. Opens from the panel ("Full dossier") and from a shared map.

### Phase 4 — Outreach prep
- `/maps/{id}/outreach` → angles: each angle = one evidence ref (talk, repo, paper, post, role,
  headline) + a one-line hook; two short drafts, each line tagged with its ref; an explicit
  "not enough specific evidence — write a generic note" state. Self-stated refs are labeled.
  Sending stays outside Roster (existing channel hand-off).

### Phase 5 — Refresh and watchlist
- `POST /maps/{id}/refresh` (owner): projected cost first (LLM compile + N artifact scans +
  web legs + current search quota), then rerun → new revision with reason `evidence_refresh`;
  delta = new people, dropped people, changed evidence types, newly corroborated, stale claims.
- Watch = a stored brief with a cadence; the worker runs refreshes within a daily budget.

### Phase 6 — Selected handoff
- CSV export accepts `?rows=id,id,…`; a "Selected rows" checkbox mode on the map; ATS import
  templates (Greenhouse/Ashby/Lever CSV shapes) as static mappings.

## 4. Data model (all additive; no drops)

```
rs_map               + brief_contract jsonb
rs_map_review        + tags text[] DEFAULT '{}', reviewer_key text, reviewer_name text
rs_map_revision      (map_id, revision_id, reason, brief_snapshot, filters_snapshot,
                      coverage_snapshot, row_snapshot, created_at)  PK(map_id, revision_id)
rs_dossier           (map_id, entity_id, brief_snapshot, sections, evidence_refs, gaps,
                      evidence_hash, created_at, updated_at)  PK(map_id, entity_id)
```

## 5. Guardrails that do not change
- Self-stated is never rendered as verified; corroboration = two independent families.
- Lack of artifacts never lowers a row by itself (coverage state, not signal).
- Feedback edits the *contract*, never the evidence.
- No LLM chooses a score, a review state, or what feedback means.
- Every spending path (refresh, dossier narrative, outreach drafts) prints its cost first.
- Kernel untouched; `tools/check_kernel_*` stay as they are.

## 6. Acceptance (gold eval additions per phase)
- P1: `brief_contract.hard` contains the named company; `soft` contains the compiler's role;
  an absent seniority on a "senior" brief yields exactly one clarification with a map present.
- P2: a review with tags round-trips; revision 0 exists after save; "revise from feedback"
  produces revision 1 whose contract differs only by the tag-implied edits.
- P3: every dossier sentence carries a ref that resolves; stale flips when evidence changes.
- P4: every draft line has a ref; the "not enough evidence" state triggers on bare profiles.
- P5: refresh reports cost before running and a non-empty delta object after.

## 7. External review (Gemini 3 Pro, 2026-09-03) and what changed

Adopted before merge:
1. **Data model** — `rs_map_review` primary key is now `(map_id, entity_id, reviewer_key)` via an
   idempotent migration in the store's ensure hook; the `entity##reviewer_key` namespace is gone.
2. **Guest writes secured** — a guest registers a name on the private link
   (`POST /maps/{id}/reviewer`) and receives a server-signed HMAC token bound to (map, key, name);
   review writes require it — the read-only share token never authorizes a write. ≤10 reviewers per
   map, in-process rate limits on registration and writes.
3. **Vocabulary migration** — a one-time SQL update (`shortlisted→shortlist`, `reviewed→maybe`);
   the read-path map stays as a fallback only.
4. **Input sanitization** — reviewer name and note are stripped of control characters and angle
   brackets at the API boundary (output stays escaped).

Deferred with reasons:
5. **Card DOM nesting** — the review control is inline-only and click-isolated; restructuring the
   three card templates from spans to divs is a UI pass for P2b, not a merge blocker.
6. Gemini's product note that hiring managers may prefer free text over tags: the note field ships
   alongside the tags; tag adoption is a success metric to watch, not a design change yet.
