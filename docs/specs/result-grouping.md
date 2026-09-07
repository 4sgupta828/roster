# Grouping search results — what we offer, and what we refuse

Status: DESIGN (2026-09-07), panel-reviewed (Codex, Gemini, code-grounded audit). Phase 1 ready to build; the DISCIPLINE
dimension needs the owner's go because it is the one the panel split on.

Owner: "identify what more relevant groups we can add — pertinent to how job seekers would benefit … industry type …
maybe go deep into job function itself — Building Infra, Applications, Research, Model training, Model inferencing,
Model design, specific tech like CRM, Databases … for Sales, Marketing, Recruiting and many other job types appropriate
groups could be thought of. Panel this one to do it right, as there are many ways to cut it, and we need to be super
disciplined about what we offer."

---

## 0. What grouping is for

Filtering **eliminates** ("show me only remote"). Grouping **organises ambiguity** ("what kinds of role are in here?").
Grouping earns its place when the seeker has not yet chosen a value: AI infra vs applications, fintech vs healthtech,
one big employer vs twenty small ones. It is noise when one group dominates, when the query already fixed that dimension,
or when most rows have no value for it.

## 1. The measurement that disciplines the menu (prod, 246,348 open postings)

| facet | corpus coverage | distinct | verdict |
|---|---|---|---|
| employment_type | 98.4 % | 4 | one group holds 92 % — fails the eligibility rule below, so it is rarely shown |
| work_type (IC / manager / exec) | 96.4 % | 5 | good |
| function | 94.6 % | 14 | good |
| field (domain) | 94.2 % | 16 | good |
| company (from the row) | ~100 % | — | good, already shipped |
| company_industry | ~58 % | 42 | good when the result set knows it |
| state | 55.2 % | 482 | only as part of Location |
| level | 42.6 % | 6 | corpus-weak, often strong inside a result set |
| metro | 42.0 % | 3,754 | only as part of Location |
| work_mode | 38.1 % | 3 | corpus-weak, strong when the seeker asked about remote |
| specialty (set) | 59.3 % | 23,554 | never raw — the top 100 values cover 30 % of rows |
| skill (set) | 58.3 % | 15,301 | never raw |
| role_family | ~100 % | 43,524 | never raw; the head is good evidence for DISCIPLINE |
| comp | 3.7 % | 5 | refuse |
| posted | 100 % | 1 value | refuse |

The corpus is not an engineering corpus: truck driving is the largest single specialty and clinical_pharma the fourth
largest field. Any taxonomy that assumes software will look broken on the corpus we actually have.

## 2. The rule (this is the discipline)

**Eligibility is judged on the CURRENT result set, never on the corpus.** A search for remote ML roles returns rows whose
work mode is nearly always known; the corpus average would have hidden exactly the dimension that search needed. For each
candidate dimension, over the rows the search returned:

- known ≥ 65 % of rows,
- between 2 and 8 non-empty groups,
- the largest group ≤ 60 % of known rows,
- "Not stated" ≤ 25 %, and always sorted last,
- groups of 1 are allowed but never the majority of groups.

A dimension that fails is **hidden from the menu**, not shown empty. This is what makes `employment_type` disappear on a
normal search (one 92 % group) and what lets `work_mode` appear on a remote-flavoured one. Both panelists converged on
"never show a junk-drawer group"; the disagreement was corpus-threshold (Gemini) vs per-result eligibility (Codex), and
per-result wins because it is the only one that adapts to the search in front of the user.

**Single membership.** A posting belongs to exactly one group per dimension. Multi-value facets (skill, specialty) are
evidence for choosing that one group, never a licence to list a posting three times. Both panelists were emphatic.

**No moving furniture.** The default stays Ungrouped and the user's choice is remembered. We do not silently re-group
between searches (Gemini's objection, accepted). The adaptive part is *which options are offered*, ordered by how
usefully each splits this result set — guidance without the UI changing under the user's hands.

## 3. The menu

| dimension | source | why a seeker cares |
|---|---|---|
| **Company** *(shipped)* | row | compare employers; spot one company hiring ten of these |
| **Industry** | `company_industry` | "healthcare vs fintech vs defence" — the owner's first example |
| **Discipline** *(new, §4)* | derived | "infra vs applications vs ML vs integrations" — the owner's second |
| **Seniority** *(shipped)* | `level` | is this market senior-heavy or junior-heavy |
| **Location** *(shipped)* | `metro` → `state` | where these roles actually are |
| **Work mode** | `work_mode` | remote / hybrid / onsite, when the set knows it |
| **IC or manager** *(shipped)* | `work_type` | track, not title |

Refused outright: raw skill, raw specialty, raw role_family (thousands of groups), comp (3.7 % coverage), posted (one
value), and employment_type as a standing option.

## 4. DISCIPLINE — the dimension the panel split on

The owner's ask is precisely the split that `function` cannot give: 69,376 postings are `function = engineering`, and a
seeker choosing between platform work and applied ML learns nothing from that. Gemini's objection is empirical and
serious: a curated map over 23.5k specialty tokens whose top 100 cover 30 % of rows produces an "Other" swamp. Codex's
answer is the right one, and it is what we adopt:

- **Classify from several signals in priority order** — `role_family` head (≈ 100 % coverage) → `specialty` → `skill` →
  `field`. Free-text specialty is evidence, never the sole key.
- **Per-function vocabularies, one menu item.** The user sees "Discipline"; the vocabulary is chosen by the dominant
  function/field of the result set. Engineering: infrastructure & platform · applications & product · data & analytics ·
  ML & AI · security · mobile · frontend · embedded & hardware · quality & release · integrations & solutions. Sales:
  new business · account management · sales engineering · partnerships · enablement & ops. Recruiting: technical ·
  executive search · sourcing · coordination · people ops. Clinical: bedside & direct care · clinical operations ·
  specialty care · pharmacy · research. Transport: long-haul · local delivery · fleet & dispatch · warehouse.
- **A hard gate**: show Discipline only when ≥ 70 % of the returned rows land in a named bucket and "Other" ≤ 25 %.
  Below that it is hidden — a failure the seeker never sees.
- **Zero model spend**: the map is curated lookup over tokens already extracted, exactly like the employer → industry
  map. No re-extraction, and no new facet on 247k postings.

Honest risk, stated: this is the piece most likely to disappoint. It is gated so that disappointment is invisible, and
it is measurable — the assignment rate on real result sets is the number that decides whether it ships.

## 5. What the code already gives us (code-grounded audit, 2026-09-07)

- Grouping already runs over **every returned row**, not the page (`groupJobs(jobs, …)`, `index.html`), ranks by
  stated → best match → size → position, and folds missing values into one "Not stated" bucket that sorts last. §2's
  eligibility and ordering are additions to this, not a rewrite.
- **Industry is already in the payload.** The via-key `company_industry` is merged into every job's `facets` during
  hydration; the card simply never renders it and `JOB_GROUPS` is a hardcoded list of four. Adding Industry (and Work
  mode) to the menu is a small front-end change with no server work.
- **Discipline cannot ride the via mechanism.** The via join is hardcoded to the company entity, so a job-local derived
  key must be materialised as real facet rows on each `job:<id>`, by a backfill pass plus a hook in `scripts/
  ingest_jobs.py` for newly ingested postings. That is heavier than the employer → industry precedent and is the main
  cost of Phase 2.
- Once declared navigable, a new job key gets chips, counts, must/prefer/avoid cycling, labels and filtering for free.
  It needs: the `FacetKey`, its stamp added to `COMPATIBLE_EXTRACTION_VERSIONS` (or the whole job corpus is re-read),
  weights, value labels, the writer, the ingest hook, and an entry in the hardcoded `JOB_GROUPS`.
- There is **no token normalisation anywhere today** — open-vocabulary values are stored exactly as the model emitted
  them, and a compiled must on `specialty` / `skill` / `role_family` is deliberately demoted because the vocabulary is
  uncontrolled. Discipline would be the first curated vocabulary over those tokens.

## 6. Delivery

1. **Phase 1 (no new taxonomy):** per-result eligibility + usefulness ordering, Industry and Work mode added to the menu,
   remembered choice, "Not stated" last. Small, and it improves the three dimensions already shipped.
2. **Phase 2 (Discipline):** the curated map for engineering and sales first, measured on real result sets, shipped only
   where it clears the gate; then clinical and transport, which the corpus demands.
3. **Phase 3:** per-group actions (save the group, "more like this group") — only once grouping is proven.

## 7. Panel verdicts

Codex — BUILD WITH CHANGES: eligibility-gate every grouping per result set; add Industry but never default to it on weak
coverage; build Discipline from multiple signals with per-function taxonomies and a 70 % assignment gate; force single
membership; refuse employment_type; suppress rather than show junk drawers. ADOPTED in full.

Gemini — BUILD WITH CHANGES: refuse anything under 90 % corpus coverage; kill Discipline (long tail, maintenance,
non-engineering corpus); elevate `function` and `field`; kill auto-grouping; sticky user choice. ADOPTED in part — the
sticky choice and no-auto-switching are right and are in §2; the corpus-coverage rule is rejected in favour of per-result
eligibility, which subsumes it; Discipline is kept but gated, because `function` demonstrably cannot answer the owner's
question.
