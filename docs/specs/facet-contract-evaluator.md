# Facets, Contracts, and the Evaluator — the one mechanism behind search, navigation, calibration and refresh

Status: spec v1 (2026-09-05). Owner directive: "Principled, deep thinking … that allows us to do a lot more
with minimal work, but right work." Product ask: rank / filter Talent Maps and Job Maps by canonical dimensions
(seniority, startup / Fortune 500 / big tech, location, compensation, IC vs manager, depth of work …) with
counts, for quick navigation across huge results; extract the facets across people and jobs to support it.

Standing rules this spec is bound by (CLAUDE.md): the MODEL owns meaning (Rule 18) — regex only as a
fallback; the kernel is domain-free (mechanics only; vocabulary and judgment come from the vertical
manifest); API credits are scarce — every spending run is projected and gated; corpus-first (extract at
ingest, never at answer time); evidence is typed and carries provenance; mobile-friendly UI; never
normalize a failing test.

---

## 0. The principle

Every search-like surface in Roster is one operation:

    evaluate(contract) over a typed entity set  →  rows, counts, coverage

People search, job search, résumé match, saved maps, reviewer calibration, keep-fresh, and the Q&A people
and jobs routes are all instances of it. Today it is implemented about six times (six scorers, six facet
readers, five return paths in one endpoint). Faceted navigation is a *property* of the operation (the
contract rendered with counts), so the operation is built ONCE and every surface re-points to it.

Four pieces, each written once:

1. **One facet model, three entity kinds** (person, job, company) in the facet read model the kernel
   already has. A facet is `(key, value, provenance, confidence, as_of, schema_version)`. `unknown` is a
   value. The kernel stores, filters and counts keys it never names; the vertical owns the schema.
2. **One extractor, schema-driven**: `extract(kind, text, schema)` — the model reads the source with the
   vocabulary supplied and returns typed facets. It replaces the five prompts that exist today.
3. **One contract**: the interpreted brief becomes executable — `must / prefer / avoid / center / rank_by`
   over schema keys, plus the semantic text and the scope.
4. **One evaluator**: pool → must-filter → score with a weight table → counts over the must-filtered pool.

What this deletes: the per-surface scorers and their bonus code, the jsonb-only job facets as a filter
path, the duplicate ranking block, four of the five jobs return paths, and every future "add a filter
for X" task. Adding a dimension = one schema entry + one extraction pass.

---

## 1. Where things live (kernel / vertical / app)

| Piece | Location | Domain words allowed? |
|---|---|---|
| Facet TYPES, contract grammar, edit semantics, scoring, counting, store protocol | `packages/kernel/roster_kernel/facets/` (new) | **No.** Pure mechanics. Keys and values are opaque strings; types are structural (categorical / ordinal / numeric / set / hierarchical). |
| Facet SCHEMA for person / job / company (keys, vocabularies, ordinal orders, labels, weights, extraction guidance) | `packages/vertical_roster/roster_vertical/facet_schema.py` (new) — registered on the manifest as `extraction_schema` | Yes — this IS the vocabulary. |
| Store adapter (Postgres): facet rows, enumeration, semantic recall, counts, projection | `apps/api/claimgraph.py` (extends the existing store) | Neutral SQL over the read model. |
| Extractor (LLM call shaped by the schema), projector (extraction record → facet rows), contract compiler (brief → contract via the model) | `apps/api/facets_engine.py` (new) | Uses the vertical schema; no vocabulary of its own. |
| Endpoints, surfaces, maps | `apps/api/app.py`, `apps/api/maps.py` | — |
| Rail UI | `apps/web/index.html` | — |

The kernel guardrails (`tools/check_kernel_invariant.sh`, `tools/check_kernel_imports.py`) stay green: the
kernel package imports nothing from the vertical and contains no domain noun. Conformance phase P4 must
still pass with the schema attached to the manifest.

---

## 2. The facet model

### 2.1 Facet types (kernel)

```
FacetType = categorical | ordinal | numeric | set | hierarchical
```

| Type | Value form | must semantics | prefer / avoid | center | counts |
|---|---|---|---|---|---|
| categorical | one normalized token per facet row; an entity may hold several rows (e.g. two functions) | entity holds ANY of the listed values | weight per hit | n/a | per value |
| ordinal | one token from an ORDERED vocabulary | as categorical | as categorical | distance from the centre in vocabulary positions; `span` = how many positions still count as "near" | per value |
| numeric | a number with a unit, stored normalized (e.g. annual USD) plus the raw display | `min`/`max` range | n/a | n/a | per BAND (bands defined in the schema) |
| set | many tokens | entity holds ANY listed value (`all` mode optional) | weight per hit, capped | n/a | per token, top-N |
| hierarchical | a path `country/state/metro` | entity's path is contained in ANY listed path prefix | as categorical | n/a | per node at the requested depth |

`unknown` is a first-class value for every type: an entity with no row for a key is counted under
`unknown`, never guessed. A `must` on a key EXCLUDES unknowns (a filter is a promise); a `prefer` / `avoid`
never touches unknowns (neutral).

### 2.2 Facet row

```
entity_kind      person | job | company            (new column on roster_entity_facet, default 'person')
entity_id        rs_entity.entity_id  |  'job:<rs_job.id>'  |  'company:<slug>'
facet_key        schema key
facet_value_norm normalized token / band / path
display_value    human form (the raw pay string, the title as written)
provenance       posting | ats_field | profile | registry | lookup | resume | brief      (new column)
confidence       0..1 (model-reported, or 1.0 for structured / lookup)
schema_version   the schema hash the row was produced under                              (new column)
valid_as_of      date
```

The read model is PROJECTED from an extraction record: `rs_job.facets` (jobs) and `rs_entity.facets`
(people, companies) hold the model's typed output as one JSON envelope
`{"schema_version", "extracted_at", "facets": {key: [{value, display, confidence, provenance}]}}`.
Projection is idempotent (delete-then-insert per entity + key set) and is the only writer of facet rows
from extraction. This matches the table's own charter ("a READ MODEL … a projector fills this").

### 2.3 Schema (vertical) — canonical dimensions

Shared keys carry the SAME vocabulary for person and job, so a Talent Map and a Job Map navigate identically
and a résumé or a posting can compile to a contract against either kind.

| Key | Type | Kinds | Vocabulary / order | Notes |
|---|---|---|---|---|
| `field` | categorical | person, job | software · data_ml · hardware · product · design · clinical_pharma · finance · sales · marketing · legal · people_hr · operations · mechanical_civil_electrical · research · education · other | the DOMAIN of the work |
| `function` | categorical | person, job | engineering · research · product · design · sales · marketing · recruiting · finance · operations · legal · clinical · executive · support · other | the KIND of work ("depth of work") |
| `specialty` | set | person, job | free, normalized lowercase phrases ("distributed systems", "oncology", "growth marketing") | model-chosen; displayed top-N by frequency |
| `level` | ordinal | person, job | intern < junior < mid < senior < staff_plus < leadership | the existing level vocabulary; `unknown` when unstated |
| `work_type` | categorical | person, job | ic · manager · executive · founder · academic | manager = manages people; executive = director and above |
| `work_mode` | categorical | job | remote · hybrid · onsite | structured ATS field beats posting text |
| `employment_type` | categorical | job | full_time · part_time · contract · internship | |
| `geo` | hierarchical | person, job, company | `country/state/metro` paths from the existing geo vocabulary | replaces the special-cased scope; `remote` is a `work_mode`, not a place |
| `company_type` | categorical | job (via company), person (current employer via company) | fortune500 · public · big_tech · startup · other | LOOKUP data, dated and curated (`data/company_sets.json`); `startup` from accelerator / stage facets; never a model guess |
| `company_stage` | ordinal | company (joined) | pre_seed < seed < series_a < series_b < series_c_plus < public | from the existing stage facets |
| `comp` | numeric | job | annualized USD, bands: <100k · 100–150k · 150–200k · 200–300k · 300k+ ; `undisclosed` | raw string kept in `display_value`; normalization in CODE with stated assumptions (hourly × 2080, monthly × 12); contract / part-time NOT annualized (band = undisclosed, raw shown) |
| `posted` | numeric | job | days since posted; bands: week · month · older | from `posted_at` |
| `years` | numeric | person | bands: 0–2 · 3–5 · 6–10 · 11–15 · 16+ | from the résumé / profile when stated |
| `skill` | set | person, job | existing skill vocabulary, lowercased | |
| `evidence` | categorical | person | the existing evidence-strength kinds | unchanged |
| `company` | categorical | person, job | normalized slug | unchanged; the join key to company facets |

Facet-through-relation: a job's `company_type` / `company_stage` are the facets of the company entity the job's
`company` slug points to. The kernel evaluator supports one hop (`via: company`) declared in the schema; the
adapter resolves it in SQL (one join) — this is how "startup" reaches both people (current employer) and jobs.
Convention (adapter): the via-key `company_type` reads the company entity's `type`, `company_stage` its `stage`
(the `via` prefix is stripped); company entities are keyed `company:<slug>` with the same slug the entity's
`company` facet holds.

Geo, v1: the existing open-vocabulary keys `country` / `state` / `metro` (set type) carry location, so no
migration is needed; the hierarchical `geo` path is reserved for v2 once metros are mapped to states.

### 2.4 Provenance and labelling

Every facet shown in the UI carries its provenance label: *from posting*, *from the job board*, *from the
profile*, *from the résumé*, *lookup*. Inferred facets are navigation aids, never cited facts: the fit table,
claims and citations stay on the grounded path. Counts always show `unknown` as its own chip so gaps are visible.

### 2.5 Schema versioning

`schema_version = sha1(canonical JSON of the schema)[:12]`. Extraction stamps the envelope and the rows.
Re-extraction predicate: `envelope.schema_version != current` (or no envelope). Adding a key re-extracts only
entities lacking it; renaming a vocabulary re-extracts everything of that kind — deliberately, and gated.

---

## 3. Extraction (schema-driven, model-owned)

`extract(kind, texts, schema, llm) → list[envelope]`, batched (jobs 25 per call; people 20 per call).

The prompt is RENDERED FROM THE SCHEMA (kernel `schema.to_prompt_block()` lists keys, types, vocabularies,
and one line of guidance per key that the vertical wrote). The model returns strict JSON aligned to the
batch; code validates every value against the schema (unknown vocabulary → `unknown` for categorical/ordinal,
kept only for `set` after normalization). Fail-safe: a failed batch writes nothing and is retried next pass.

Inputs per kind:
- **job**: title, company, department, location, the posting body HEAD (first 400 chars) AND TAIL (last 600
  chars — pay-transparency ranges live at the end), plus structured ATS fields when present (Ashby
  `compensation`, `workplaceType`, `employmentType`; Lever `workplaceType`, `salaryRange`). Structured beats
  text: when the ATS gives `workplaceType` the model's `work_mode` is ignored and provenance = `ats_field`.
- **person**: name, bio, company, location, blog, the profile's own text (GitHub), or the scholarly /
  registry summary the ingester already builds; years from dates when present.
- **company**: registry / filing / YC text already in the corpus; plus lookups (`company_sets.json`).
- **résumé** (a person the user IS): the same person schema over the résumé text — replaces the ad-hoc
  `field/fields/level` fields added to the parser on 2026-09-05.
- **brief compile** (a search the user typed): `compile(kind, text, schema, llm) → contract` — the model maps
  the brief onto schema keys as must / prefer / center; code validates. Replaces `_JobPlan` field/level and
  the people facet-parse prompt's ad-hoc vocabularies (the people prompt's synonym guidance moves into the
  schema's per-key guidance).

Spend model (DeepSeek, measured 2026-09-05: 80 calls ≈ $0.05 for 2,000 postings): jobs ≈ $0.025 / 1k;
people ≈ $0.03 / 1k. Full index: jobs ≈ $6 (212k), people ≈ $12 (400k), companies ≈ $1. Each is a gated run
(`ROSTER_BULK_JOB_FACETS`, `ROSTER_BULK_PEOPLE_FACETS`, per-pass caps), tranche-ordered: entities that appear
in saved maps or recent results first, then newest, then the rest.

---

## 4. The contract (kernel grammar)

```json
{
  "kind": "job" | "person",
  "text": "the semantic query (brief, résumé soul, posting soul)",
  "must":   {"level": ["senior","staff_plus"], "geo": ["us/ca/bay_area"], "comp": {"min": 200000}},
  "prefer": {"company_type": ["startup"], "skill": ["kubernetes"]},
  "avoid":  {"function": ["sales"]},
  "center": {"key": "level", "value": "senior", "span": 1},
  "rank_by": "match" | "comp" | "newest" | "level" | "evidence",
  "scope":  {"country": "us"},
  "exclude_ids": [],
  "limit": 60
}
```

Semantics, fixed once (kernel):
- Within a key: OR. Across keys: AND. `must` filters; `prefer` / `avoid` rank; `center` ranks by ordinal
  distance; `rank_by` is the final sort (ties by score).
- `unknown` never satisfies a must; never moves under prefer / avoid / center.
- **Edit cycle** (the rail's tap): `off → must → prefer → avoid → off` for one value; `edit(contract, key,
  value, mode)` is a pure function; the map feedback tags `prefer:key=value` / `avoid:key=value` are the same
  edits (calibration becomes contract editing — `calibration.feedback_to_contract` re-targets this grammar).
- Serialization is canonical (sorted keys) so a contract can be hashed, diffed, and stored on a map.
- **A must is a promise the index can keep.** A compiled must on an open-vocabulary key (a free phrase such
  as `role_family` or `specialty`) is stored as a prefer — an exact promise cannot be made on free text; the
  employer, named skills and places are exact and may be promised. A must on a key whose index-wide coverage
  (share of entities with a known value, cached hourly) is below 50 % is downgraded to a prefer, because it
  would filter by absence rather than by fact. The compile prompt asks for musts only on explicit
  requirements (a named company, a place, remote / hybrid, a pay floor, "only senior"); what the brief merely
  describes is a preference. The user's own chip taps are never downgraded — they are explicit.

---

## 5. The evaluator (kernel mechanics; vertical weights)

```
evaluate(contract, store, weights) → {rows, counts, coverage, contract}
```

1. **Pool**: `store.semantic(kind, embed(text), must, cap)` ∪ `store.enumerate(kind, must, cap)` ∪ optional
   keyword legs (`contract.angles`, supplied by the compile step). Musts are applied IN SQL for every leg, so
   no row enters the pool without satisfying them (the leaky-pool defect of 2026-09-05 cannot recur).
2. **Score** (per row): `sim` (semantic similarity, calibrated above the query's noise floor as today) +
   Σ prefer hits × w_prefer[key] − Σ avoid hits × w_avoid[key] − ordinal_distance × w_center + evidence
   term (person) — weights come from the vertical (`FacetWeights`), default 0.10 prefer, 0.10 avoid,
   0.06 / position centre. Reasons are generated from the terms that fired, worded from the schema labels
   ("prefers startup", "far from senior", "unknown level").
3. **Counts**: `store.counts(kind, keys, must)` — per key, per value (bands for numeric, depth for
   hierarchical, top-N for sets), over the must-filtered pool — PLUS `unknown` per key. This is what the rail
   shows; tapping a chip re-evaluates with the edited contract.
4. **Coverage**: pool sizes per leg, dropped-by-scope, unknown rates per key — the honest coverage basis.
5. **Rank_by**: `match` (score), `comp` (numeric desc, unknown last), `newest`, `level` (ordinal desc),
   `evidence`.

The evaluator is deterministic for a given index state: same contract → same rows and counts (no model call
inside `evaluate`). The model is used only to COMPILE a brief into a contract and to EXTRACT facets at ingest.

---

## 6. Surfaces (what re-points to the evaluator)

| Surface | Today | After |
|---|---|---|
| Jobs plain search | agentic_job_search (own scorer) | compile → evaluate(kind=job) |
| Jobs profile-steered / résumé match | match_resume_jobs (own scorer) | résumé → contract (text = search_text; center = level; prefer = skills) → evaluate |
| Talent Map search | answer_people_population (own facet filter + rerank) | compile → evaluate(kind=person) (grounding / evidence gate unchanged, applied to rows) |
| Saved map | rows snapshot + revisions | + `contract` jsonb column; navigation = evaluate(map.contract ⊕ view edits); snapshot stays the record; refresh = evaluate(map.contract) |
| Reviewer calibration | tags → contract edits → own re-run | tags → `edit(contract)` → evaluate |
| Keep-fresh | re-run helpers | evaluate(map.contract) + diff |
| Q&A people / jobs routes | own | compile → evaluate |
| Apply fit table | grounded two-stage grader | unchanged (the deep read stays separate from navigation) |

The five jobs return paths collapse to one post-processing helper: `evaluate → brief strip → session save`.

Endpoints:
- `POST /search/evaluate` `{contract}` → `{rows, counts, coverage, contract}` (signed-in optional; scope by
  country as today).
- `POST /search/compile` `{kind, text}` → `{contract}` (model call, cached by text hash).
- `POST /maps` gains `contract`; `GET /maps/{id}` returns it; `POST /maps/{id}/navigate` `{edits}` →
  evaluate without saving; `POST /maps/{id}/revise` saves the edited contract as a revision.

---

## 7. UI — the rail

The interpreted-brief strip becomes the rail. One row per dimension the schema marks `navigable` for the
kind; each shows chips with counts from `counts`; chip state cycles off → must → prefer → avoid (colour +
glyph: ● must, ▲ prefer, ▼ avoid); `unknown` is a chip. Active edits are pills above the list with ✕. Rank-by
is a select. Collapsed dimensions show the top 5 chips and "more". Phones: dimension rows scroll
horizontally; tap targets ≥ 40 px; the rail folds behind one "Filter · N active" button under 560 px. The
"must have" chips and "Center around level" disappear as separate controls (they are `must:evidence` and
`center:level` in the rail). Every facet chip has a tooltip with its provenance.

### 7.2 WHERE — the scope question, answered before any other (2026-09-08)

**The default is ALL US, any city.** It used to be the reader's own detected metro, which silently hid
most of the index: a Bay Area default answered "backend engineer" with Bay Area roles and nothing on
screen said the rest of the country had been excluded. The detected metro is still the FIRST option in
the header selector, one click away; it is simply not the default. Only a choice the reader makes
explicitly is remembered across sessions.

**The jobs search never applied the scope at all.** The people path promoted `scope.country` into
`must.country`; the jobs path passed the scope to the compiler and dropped it, so a US search returned
German and Indian postings mixed in. Both paths now promote the stated scope — country, and a metro or
state when the reader chose one — *unless the question itself named a place*, which always wins.

**The rail owns the levels.** Geography has levels — the whole world, a country, a state, a city — and
twenty chips in a row is not the same affordance as being able to say "only Texas". A **Where** row
sits at the top of the rail with three selects (Any country / Any state / Any city) built from the
counts already in the response, so it offers only places these results actually contain, and a
**🌍 Worldwide** button that clears all three. Choosing a country clears a state and city that no
longer sit inside it.

Two deliberate differences from the chips below it:
- **It applies at once.** A scope is not a filter you stage: chips stage behind Apply, Where does not.
- **It is a select, not a chip.** "Any city" is one answer out of hundreds, and a level is a choice
  rather than a toggle.

The three geography keys are removed from the chip rows, so there is one control per question.

**Place names are vocabulary, so they live in the vertical.** `KEYED_VALUE_LABELS` is keyed BY FACET
because the codes collide — `in` is India and Indiana, `de` is Germany and Delaware — and one shared
table would rename one of them wrongly. `uk` and `gb` both read "United Kingdom", because side by side
on a rail they look like a bug rather than two spellings. Metros have no table and are title-cased:
"san francisco" reads as data, "San Francisco" reads as somewhere to work.

### 7.1 The rail costs the counts, never a re-search (2026-09-07)

**The chips are a property of the contract's must-slice, not of the ranked page.** Opening a saved map used to
re-run the whole search (`POST /maps/{id}/navigate`) purely to fill the rail, then throw the rows away — the
snapshot was already on screen. Measured on the production index:

| | full evaluate (what the open path used to do) | counts only |
|---|---|---|
| talent map, `must {country us, field software}` | **29.0 s** (legs 28.4 s) | **3.2 s** cold, **0.1 s** warm |
| job map, résumé contract | 2.7 s | 0.11 s |
| chips produced | — | **identical** (asserted in the probe and in the kernel test) |

Three parts, each in the layer that owns it:

1. **Kernel** — `evaluate(..., depth={"rows": False})` returns `{rows: [], counts, coverage, contract}` and never
   asks the store for a row. Symmetric with the existing `depth={"counts": False}`. Coverage carries
   `counts_only: true` and `slice` (the must-slice read off the counts); it does **not** carry `pool`, because no
   page was ranked and a rail must not claim a number it did not compute.
2. **App** — `POST /maps/{id}/navigate {"rows": false}` takes that path; `group_options` is computed from the
   map's stored rows (pure Python). `save` is rejected there: a rail-only navigation has no rows to record.
   `GET /maps/{id}` now returns `labels` alongside the map — the vocabulary is schema-only, so a surface with
   counts needs no second call to draw.
3. **Surface** — saving a map stores its `counts` in `coverage` (forking already did), so **reopening a map
   paints the rail from the snapshot with no network at all** (measured 0.42 s to first paint at 390 px, 152
   chips, 17 dimensions). The rails-only navigate then refreshes those counts against the live index.

An **Apply** still runs the full evaluate — it genuinely needs new rows. A map saved before this change has no
stored counts and paints after the counts call instead of instantly.

---

## 8. Migration (minimal, in order)

1. Kernel `facets/` (types, contract, edit, evaluate, counts, store protocol) + vertical schema + app adapter
   (columns `entity_kind`, `provenance`, `schema_version` on `roster_entity_facet`; job rows projected from
   `rs_job.facets`), `POST /search/evaluate` and `/compile`, the Jobs plain search behind
   `ROSTER_FACET_EVALUATOR=1`. **No spend.**
2. Schema-driven job extraction (head + tail + structured fields; comp; work_mode; function; work_type;
   specialty); Ashby boards re-fetched with compensation on (ETag-gated). **≈ $6, gated.**
3. Rail on Jobs and Job Maps; maps gain `contract`; keep-fresh and job-map calibration on the evaluator;
   delete `match_resume_jobs` / `agentic_job_search` scorers. **No spend.**
4. People extraction in tranches (surfaced people first); Talent Map, people calibration, and Q&A people route
   on the evaluator; delete the old rerank; `people_facets.py` vocabulary folds into the schema. **≈ $12, gated.**
5. Company lookups + provenance labels + eval traps. **No spend.**

Nothing is deleted before its replacement runs behind the flag on prod for one day with the golden evals green.

### Status (2026-09-05, later session) — START HERE next session
- Step 1 LIVE: kernel `facets/` (13 tests), vertical schema (6 tests), app store + engine + endpoints, every open
  posting projected into the read model (212k), parity SQL ↔ reference OK on prod (`scripts/facets_parity.py`),
  `ROSTER_FACET_EVALUATOR=1` on prod: the Jobs surface (plain and signed-in résumé path) compiles → evaluates.
- Step 2 RUNNING: the worker's facets loop (`ROSTER_BULK_JOB_FACETS=2000`) extracts with the schema-driven
  engine (head + tail, pay gated by a verbatim figure), writes the envelope and projects in the same pass; legacy
  flat records re-extract. ≈ $6 total. Ashby boards NOT yet re-fetched with structured compensation / work
  mode (step 2b — the ingest requests `includeCompensation=false`; flip it in `_BOARD_URL` and carry
  `workplaceType`/`compensation` into `structured`).
- Step 3 DONE: the rail is STAGED (chips edit a draft; "N changes pending" → Apply runs once; Reset); saved Job
  Maps carry `contract`; `POST /maps/{id}/navigate` (save = revision); keep-fresh and reviewer calibration run
  on the evaluator when a map has a contract (`tags_to_contract`); per-card facet line with provenance.
  Old scorers (`match_resume_jobs`, `agentic_job_search`) still present behind the flag's else-branches —
  delete after a day of golden evals green with the flag on.
- Step 4 BRIDGED (2026-09-05, no spend): the people index's ~2.3M pre-schema rows (`seniority` / `role` /
  `function` in `people_facets.py`'s vocabulary) are projected into schema rows by ONE lookup table between the
  two closed vocabularies — `roster_vertical/facet_legacy.py` (provenance `legacy`; `evidence` derived from
  `rs_person_artifact`, provenance `artifact`) via the set-based `POST /admin/facets/project-people`
  (idempotent; re-run after people ingest). The SAME table translates the old engine's compiled filter into the
  rail's contract (`legacy_brief_to_contract`: the engine's HARD facets → musts — within a legacy key only the
  schema keys EVERY chosen value maps to; its SOFT facets → prefer), so the rail's counts describe the slice the
  engine filtered. Vocabulary collision handled in the store: the legacy `function` key (backend, medicine,
  chemistry …) shares its NAME with the schema's `function`; `closed_vocab_pairs` makes closed keys count and
  attach only schema-legal values, so an older vocabulary's rows are invisible to the read model.
  Verified on prod 2026-09-05: People tab rail (desktop + 390 px), stage → Apply → hydrated cards, saved Talent
  Map create → navigate → save-as-revision → tag → revise preview (throwaway account, deleted). Person evaluate
  ≈ 4 s (embedding + two legs + counts); counts for the rail are cached 10 min per must-set.
  Talent surface: the first turn's rows still come from `answer_people_population` (person lookup, refinement
  turns, company guard, topic partition, evidence ranking stay there); `facet_nav` on the response carries the
  rail (counts-only, cached 10 min per must-set); Apply → `POST /search/evaluate` kind=person → rows hydrated
  to people cards (`_hydrate_people`: `people_by_ids` → `rows_to_people` + artifacts, evaluator facets / match /
  reasons riding along). Saved Talent Maps carry `contract`; navigate / save-as-revision / keep-fresh run on the
  evaluator; talent calibration on a contract map uses `row_tags_to_contract` (card tags → prefer / avoid /
  exclude through the row's own facets; never a must).
  Step 4 RUNNING (owner's go 2026-09-05): `ROSTER_BULK_PEOPLE_FACETS=2000` on roster-worker — the people-facets
  thread extracts 2000 people per pass (saved-map people first, then newest), writes `rs_entity.facet_env`,
  projects the rows (provenance `profile`) and DELETES that person's bridged `legacy` rows in full (a key the
  model leaves unknown stays unknown). ≈ $12 for 400k people, DeepSeek, ~2 days. Progress: count of
  `rs_entity.facet_env->>'schema_version' = '77c3be380247'`. The extractor's hint text omits the old
  `seniority` token (its 'mid' was a default the model echoed). After it lands: the Talent surface's first turn
  moves to compile → evaluate and the people compile prompt folds into the schema.
- Step 5 PARTIAL: company `type` facets from lookup data are LIVE (`scripts/company_facets.py`, curated
  `data/company_sets.json` dated 2026-09); eval traps in `apps/api/test_facet_traps.py`; provenance labels on
  cards. Not done: hierarchical geo; golden-eval run with the flag on; deleting old scorers.
- Evaluator rules learned in prod (2026-09-05, owner report "removed founder, still founder jobs; rank by level
  shows IC"): WITH text the pool is the semantic neighbourhood ONLY — no enumerate leg (it poured sim-0 rows in and
  `rank_by` an ordinal sorted them first); filtered semantic legs use pgvector's iterative scan (`relaxed_order`,
  `max_scan_tuples` 40k) so a must no longer truncates recall to the first 200 candidates; small must-slices take
  an exact-distance path; `ix_rs_job_facet_eid` makes the job must correlation indexable. Schema guidance: a
  "founding engineer" is an early hire (ic, level as stated), not leadership — 503 founding-titled postings re-read.
- Ranking law v2 (owner report "field has no influence", 2026-09-05): rank = calibrated match points + BOUNDED
  preference points (weight × 50 per hit, ≤ 3 hits per key) − avoid points − centre steps; and every preferred key
  gets its OWN retrieval leg (the nearest rows holding a preferred value join the pool), because a preference that
  never reaches the pool cannot rank. `field` is the strongest signal both ways (prefer 0.25 = 12.5 points per
  hit; avoid 0.30). Verified: prefer field=marketing pulls marketing-field managers into positions 4 and 6 of a
  CRM search; must field=marketing yields exactly the 9 matching managers. Vocabulary note: "marketing
  engineering" is field=software + specialty / skills (CRM, Salesforce), not field=marketing (that selects
  marketers) — the rail's Skills / Specialty chips are the right lever there.
- Step 4 CUTOVER (2026-09-05, owner: "still not working" on a brief typed straight into Talent Map): a FRESH brief on
  the People surface now compiles → evaluate → hydrated cards + rail (`_people_population_route` → `_people_contract_route`)
  whenever the compile names a role / field / level / skill / place; the old engine keeps person-name lookups
  (a compile with only a company, or nothing) and follow-up refinement turns (`refine_facets` / `prior_person`).
  The People tab's evidence chips → `must evidence`; its level selector → `center`; the selector country → `must`.
- Latency (2026-09-05): counts materialize the slice once (temp table), SAMPLE above 40k entities (1-in-N hash,
  scaled, `coverage.counts_sampled` → "counts ≈" on the rail) and cache 10 min per must-set inside the store;
  retrieval legs run concurrently, prefer legs only for the two heaviest non-set keys. People evaluate: 1–4 s
  uncached, a Talent brief end to end ≈ 10 s; the first touch after a deploy is cold (DB buffers) — the API warms
  the common slices at startup.
- Known gaps: `role_family` pills show the key label only; the pool for a brief with no musts is the semantic
  top-K (cap ≈ limit × 6), so counts are index-wide while rows are the nearest; navigation on a map with no
  contract (saved before this work) says so and asks for a fresh search.

---

## 9. Tests (TDD — written before the code they pin)

Kernel (`packages/kernel/roster_kernel/facets/test_facets.py`), pure, no DB:
- schema: types validate values; ordinal order and distance; numeric bands; hierarchical containment; unknown
  handling; `to_prompt_block` renders every key; `version()` is stable and changes when a vocabulary changes.
- contract: canonical serialization; `edit` cycle off→must→prefer→avoid→off; OR-within / AND-across;
  `center` span; validation rejects unknown keys and off-vocabulary values (with a clear error).
- evaluate (in-memory store): musts filter and exclude unknown; prefer / avoid / center move rows the
  documented amounts with the documented reasons; rank_by orders; counts per key/value/band/path include
  `unknown`; counts reflect the must-filtered pool; determinism (same input → same output); the leaky-pool
  invariant (no row lacking a must value can appear).
- store protocol conformance test any adapter must pass (an in-memory reference adapter ships with the kernel).

Vertical (`packages/vertical_roster/roster_vertical/test_facet_schema.py`):
- every key has a type, label, kinds, guidance; shared keys share vocabularies; ordinal orders complete; weights
  present for every rankable key; the manifest exposes the schema; conformance P4 passes.

App (`apps/api/test_facets_engine.py`, `apps/api/test_search_evaluate.py`):
- projector: envelope → rows idempotent (re-projection replaces, never duplicates); job rows keyed `job:<id>`;
  provenance and schema_version stamped; structured ATS field beats model text.
- extractor: prompt rendered from the schema; off-vocabulary values become `unknown`; pay normalization
  (hourly × 2080 → band; contract → undisclosed; "competitive" → undisclosed) with the raw string kept.
- compile: brief → contract validated against the schema (monkeypatched LLM).
- endpoint: `/search/evaluate` returns rows + counts + coverage; musts are honoured in SQL (adapter test against
  the reference adapter); the Jobs surface behind the flag returns the same contract shape as the brief strip.
- eval traps (held-out): wrong-field posting ranks below same-field; unknown level never satisfies a level
  must; invented pay cannot appear (only structured or verbatim-span text is bandable); a startup label needs a
  lookup or a stage facet.

Golden eval (`scripts/eval_gold.py`) must stay 27/27; the Jobs golden cases run through the flag both ways
until the old path is deleted.

---

## 10. Non-goals (v1)

No cross-tenant facets; no learned weights (the weight table is hand-set and small); no client-side
filtering of snapshots (navigation always re-evaluates); no model call inside `evaluate`; no compensation
inference from titles or companies; no company size until a source exists.

## Industry — what the employer does (2026-09-07)

Owner: "why fintech jobs are not captured in this?" — nothing was broken; the schema had no way to say what an employer
DOES, so a brief's "fintech" survived only as free text while every other part of it became a facet.

`company_industry` is a VIA key on jobs and people (kinds P + J), reading the COMPANY entity's own `industry` — the same
mechanism as `company_type` / `company_stage`. 41 values (fintech, financial_services, insurance, crypto, healthtech,
biotech_pharma, medical_devices, developer_tools, data_infrastructure, ai, security, gaming, aerospace_defense,
semiconductors …). Weights: prefer 0.22, avoid 0.25 — an industry says more about fit than a size class does.

Source is LOOKUP DATA, never a model guess: `roster_vertical/data/company_industry.json` (curated, dated, grouped by
industry so it is auditable; a company the map does not name gets no facet). `scripts/company_industry.py --live`
writes the company rows; it matches a slug through its legal suffix (`coupang_inc` → `coupang`), is idempotent and
costs nothing. First run 2026-09-07: **1,342 companies, covering 136,762 of 247k open postings.**

Two traps this uncovered, both now guarded by tests:
- **A via key needs a real key to read.** `project` silently drops a key the schema does not declare, so the first live
  run reported 1,342 matches and wrote 0 rows. `test_company_industry` asserts every via key has a target key on the
  target kind, sharing its vocabulary; the pass counts rows written and fails loudly if a match writes none.
- **Adding a via key must not re-extract the corpus.** `FacetSchema.version()` hashes every key, so the stamp changed
  although nothing about extraction did. `COMPATIBLE_EXTRACTION_VERSIONS` lists stamps whose extractions stay valid and
  both corpus passes accept them — without it this change alone would have re-read 281k people and every job at full
  model cost.

Also: the compile is told that a named industry is a filter (it is looked up per employer, not read from a posting),
and `to_prompt_block(include_via=True)` lets a SEARCH compile set via keys while the EXTRACTION prompt still never
mentions them.

Next for coverage: the curated map covers the large employers; the long tail is best filled by reading the industry
from the posting body (5,217 open postings say "fintech" outright), which needs model credit and is not done.

### Industry coverage, employer links and grouping (2026-09-07, later)

- **A second, free industry source.** The curated map covers the big employers; healthtech is mostly small companies, so
  the owner saw no healthtech chip. The people index already carries a per-person `industry` label that describes the
  EMPLOYER (payments → Nubank, Klarna, Razorpay, Block, Stripe; healthcare → Endpoint Health, Brave Care), so a company
  whose employees carry one label, and that the curated map does not name, takes it (majority label, support ≥ 2,
  provenance `derived:employee_industry_labels`). Coverage 1,342 → **3,422 companies / 143k postings**, healthtech 493.
  The sibling `sector` key is deliberately unused: it describes the person's work, so its devtools bucket is Google,
  Microsoft and Apple.
- **A named industry FILTERS.** "backend at healthtech companies" was landing in `prefer`, so the search ranked healthtech
  faintly and returned everything else. The compile now puts an asked-for employer industry in `must`; `company_industry`
  is absent from `RELAX_ORDER`, so smart relaxing never quietly drops it.
- **One aggregator cannot fill a page.** jobgether reposts other companies' roles (4.5k open postings, the largest single
  "employer") and returned one title ten times. `thin_repeats` keeps at most two rows per (company, title).
- **Employer links** (`roster_vertical/employer_link.py`, `employer_site.py`, `scripts/company_sites.py`): the card links
  the company name to their OWN site and offers a terse "All jobs ↗" to their board. The site is harvested once per
  company from the board page (Ashby embeds `organization.publicWebsite`; Lever and Greenhouse put it behind the header
  logo — read at least 1.8 MB, Lever inlines ~400 KB of CSS first). An ATS VENDOR's own site is never taken for the
  employer's: Greenhouse's footer link had made `greenhouse.com` the site for 420 employers before that guard existed.
- **Grouping** (web): the whole result set groups by company, location, seniority or IC/manager — ranked by the best role
  in each group, then size; collapsed by default; "Not stated" last. Ungrouped remains the default.

Open: the site harvest is a script, not a worker loop, so new employers stay unlinked until it is re-run.
