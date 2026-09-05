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
