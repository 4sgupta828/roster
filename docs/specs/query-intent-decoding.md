# Query Intent Decoding — one mechanism for short queries, across jobs, talent and research

Status: spec v1 (2026-09-09). Panel: Gemini 3 Pro, a code-grounded mapping pass, an adversarial pass,
and live prod measurement. Codex could not be run (see §9). Owner ask: "we are not doing a good job to
understand intent and our results are many times not what user wants … this is a general problem to solve."

Bound by CLAUDE.md: the MODEL owns meaning (Rule 18) but never invents constraints; the kernel is
domain-free; API credits are scarce; corpus-first; evidence is typed; a gate needs an eval case designed
to pass it while being wrong; never normalize a failing test.

---

## 1. What actually happens today (measured on prod, not assumed)

Six short queries against `POST /jobs` on 2026-09-09. `must` and `prefer` are the compiled contract.

| query | compiled must | compiled prefer | what came back |
|---|---|---|---|
| `SRE` | `country:us` | field=software, function=engineering, role_family | **correct** — real SRE roles |
| `MLE` | `country:us` | field=data_ml, function=engineering, role_family | **correct** |
| `PM` | `country:us` | field=product, function=product, role_family | product mgr — **silently picked one of two readings** |
| `stripe` | `company:stripe`, `country:us` | — | **correct** (but duplicate rows: `stripe` and `Stripe`) |
| `austin` | `country:us` | — | **wrong** — "Austin Tour Guide", "Warehouse Technician - Austin" |
| `staff` | `country:us` | — | **wrong** — "Staff Feed Staff", "Supervisor, Talent Coordinators" |
| `remote` | `country:us` | — | **wrong** — "Remote Opportunity - Take Back Control of Your Time" |
| `CUDA kernels` | `country:us` | — | half-right by luck — an NVIDIA GPU-kernel role at #2, a freelance "AI Task Auditor" at #1 |
| a greenhouse JD URL | `country:us` | — | never fetched; the URL string was embedded |

The pattern is not "acronyms are broken". Role acronyms decode **well**. What fails is narrower and
much more embarrassing:

**A query that IS a facet value, verbatim, is dropped.** `remote` is a literal member of
`WORK_MODES = ("remote","hybrid","onsite")` (facet_schema.py:20). `staff` is `staff_plus` in
`LEVELS` (facet_schema.py:18). `austin` is a metro the vertical already knows how to canonicalise
(`METRO_ALIAS`, `canon_metro`). In all three the decoder emitted nothing.

### 1.1 Why — three compounding mechanisms

1. **The compiler is prompted to read a DOCUMENT, not a query.** `_COMPILE_SYS`
   (apps/api/facets_engine.py:130-141) says *"ONLY hard requirements the brief states explicitly …
   omit what the brief does not say; never invent constraints"*, and the KEYS block it is given is
   rendered from the same `guidance` strings the ingest EXTRACTOR uses — "as the posting states it",
   "actually named in the text". Given the one word `remote`, a model told to extract only what is
   explicitly *stated as a requirement* correctly concludes that nothing is stated. The prompt is
   working as written; it is written for the wrong job.
2. **There is no lexical leg.** The pool for a facet search is built only from pgvector legs
   (`evaluate.py:83-103`); `apps/api/facet_store.py` contains no `tsv`, `to_tsquery`, `ts_rank` or
   `ILIKE` anywhere — `semantic()` at :220 is pure cosine. So when the contract is empty, the result
   is 100% embedding similarity of one or two words, inside `country=us`. A single common word has a
   diffuse embedding: that is precisely how you get "Austin Tour Guide" for `austin`. The `rs_block`
   tsv column exists and this path never touches it.
3. **Failure is silent.** `compile_contract` catches every exception and returns an empty contract
   carrying only `text` (facets_engine.py:157-158). A model timeout and a genuinely unconstrained
   query are indistinguishable downstream, and nothing on screen says which happened.

### 1.2 Two decoders, and the tabs bypass the router
`POST /jobs` never calls `classify_qa_route`; the Jobs and People tabs return before it
(app.py:4409-4423, 2851). The router's `indexed_job_search` route uses `parse_job_query`
(people_population.py:2185-2210) — a **different decoder**, and the only one that knows
`FAANG → [meta, apple, amazon, netflix, google]` and `SWE → [software, engineer]`. The tab a user
actually types into cannot reach it.

---

## 2. What no decoder can fix — two findings from the prod index

The adversarial pass queried the production database, and I re-ran its two most consequential
queries myself (`railway ssh --service roster-api`, read-only). Both hold.

### 2.1 `downgrade_uncovered_musts` has never fired in production

`_facet_coverage` (app.py:6362-6381) computes, per key, `1.0 - unknown/total` where
`total = sum(d.values())` over that key's own counts. But **the index stores absence as a missing
row, not as an `unknown` value** — measured over people, every key returns exactly 0 unknowns:

```
key               unknown   entities   coverage
country                 0     366452      1.000
function                0     346956      1.000
field                   0     293238      1.000
role_family             0     233755      1.000
metro                   0     161660      1.000
```

So coverage is **1.000 for every key, always**, `min_known = 0.5` is never met, and the guard that is
supposed to stop a compiled `must` from becoming an impossible promise is inert. The same
`coverage < 0.5` condition gates `_index_aware`'s collapsing-must demotion, so that arm is dead too.

This is a live bug, not a proposal risk. `level` exists for 40,325 of 428,357 people (**9.4%**), so a
`must: level=…` on the People tab silently excludes ~90% of the index *by absence* — and the mechanism
built to catch precisely that cannot see it, because it measures coverage over only the entities that
already have the key. **The denominator is wrong.**

### 2.2 The people index is substantially not the population Roster claims to serve

Top values, people, distinct entities (verified directly):

```
field:        software 110,404 | clinical_pharma 74,355 | research 45,814 | other 26,044
role_family:  published author 83,806 | software engineer 49,870 | founder 10,966
```

The **most common "role" among Roster's people is `published author`** — an OpenAlex ingest artifact,
not a job — and the **second-largest "field" is `clinical_pharma`** at 74,355 people (25% of the
293,238 who have a field at all). That is inherited biomedical residue from the noesis/eigen fork,
which CLAUDE.md's standing no-noesis directive says must not be shaping roster answers. `field` is in
`RELAX_NEVER` (intake.py:192), so a `must: field=…` derived from that distribution is unrecoverable
within a search.

And `company_industry` — the key this design most wanted to infer — has **0 rows in the entire index**.

**The implication for this spec is the point of the section.** Better intent decoding maps a query
onto the index we have. It cannot make that index the right one. Ranked honestly, fixing 2.1 and
auditing 2.2 outrank everything in §4, because a perfectly decoded `must` against a 9%-covered or
25%-contaminated key produces a confidently wrong answer instead of a vaguely wrong one.

---

## 3. The principle

> Decoding intent is **cheapest-first, and every layer may abstain.**
> A layer that cannot be sure hands the term to the next one. Only the layers that cannot be wrong
> are allowed to produce a `must`.

Today there is one layer (a model call prompted for the wrong job) and no abstention: it either
guesses or emits nothing, and both look the same. The fix is not a better prompt — it is putting
the deterministic knowledge we ALREADY HAVE in front of the model, and letting the model do the part
only a model can do.

## 4. The three layers

### Layer 0 — LEXICON. Deterministic, zero model calls, cannot hallucinate.
Match query n-grams (1–3 tokens) against, in order:
- **the schema's CLOSED value sets** — `work_mode`, `level`, `field`, `function`, `work_type`,
  `employment_type`, `company_type`, `company_industry`. If the user typed a legal value of a closed
  vocabulary, that is not an inference, it is an identity. `remote` → `work_mode:remote`.
- **vertical alias tables** — `METRO_ALIAS`/`US_METROS` (already exist, currently unreachable from the
  compile), state names/codes, level synonyms (`staff`→`staff_plus`, `principal`, `head of`→
  `leadership`), and the acronym table stranded in `parse_job_query`.
- **resolved corpus entities** — company name → slug, from the company table we already join for
  `company_type`/`company_stage`/`company_industry` (facet_store.py:45-47,117-119). This is also
  where the observed `stripe` / `Stripe` duplicate gets fixed: one slug, one row.

Output: candidate `(key, value, span, source="lexicon")`. **Only this layer may produce a `must`,**
and only for a closed set or a resolved entity — the two cases where being wrong is impossible.

### Layer 1 — CORPUS STATISTICS. **Deferred. Its preconditions do not hold today.**
The intended mechanism: an offline aggregate over the corpus giving, for a term, the distribution of
the extracted facet values of the documents it appears in — so `CUDA` learns it implies
`skill:cuda` without anyone writing that down, and a legal vertical gets `M&A → specialty:mergers`
by running the same job over its own corpus.

The adversarial pass killed the version that ships now, on evidence (§2):
- its flagship target key, `company_industry`, has **0 rows** in the index;
- the person half has **no document→extraction pairing** to aggregate — nearly all person facet rows
  carry empty or `legacy` provenance, i.e. migrations, not text extractions;
- its two best-covered person keys are 25–36% ingest artifact (§2.2), so the distribution it would
  learn from is contaminated at the source.

Two design rules survive and are recorded here for whenever it is built:
- **Score by LIFT, not probability.** `P(value|term)` is dominated by the majority class; use
  `P(value|term) / P(value)` with a minimum support count.
- **It may only ever emit `prefer`.** Term co-occurrence is boilerplate-contaminated by construction
  — "we use Stripe for payments", "remote (US)". A wrong `prefer` costs a few rank points (capped at
  30 by `max_prefer`); a wrong `must` empties the result set, and §2.1 shows the guard that was
  supposed to catch that is inert. **Statistics propose; they never filter.**

Preconditions before this layer is reconsidered: fix §2.1 so coverage is measurable at all; give
person facets real extraction provenance; audit §2.2.

### Layer 2 — THE MODEL. One call, re-prompted, given the first two layers' findings.
Unchanged in cost (one call, already paid today). Two changes in kind:
- **Prompted for queries, not documents.** A second system prompt for `kind="query"` that says what a
  short query IS: a person naming a role, a place, a seniority, an employer or a technology, usually
  without grammar. The extractor's "as the posting states it" guidance must not be what a query
  compiler reads.
- **Given candidates to CONFIRM or REJECT**, not a blank page. Layers 0 and 1 pass their hits in the
  user message; the model's job becomes adjudication (is `staff` here the seniority or the noun?),
  which is exactly the judgment a model is good at and a lexicon is not.

## 5. The output: `QueryIntent`, not just a Contract

```
QueryIntent {
  route:     keyword | natural_language | profile_text | entity_url | question
  contract:  Contract            # must/prefer/avoid/center/text, as today
  spans:     [{text, key, value, source: lexicon|stats|model, confidence}]
  residual_text: str             # what stayed prose, for the semantic leg
  ambiguity: [{span, key, candidates[], reason}]   # explicit, and empty is a real answer
  notes:     [str]               # why the search is what it is — shown, not swallowed
}
```
The three things today's `Contract` cannot express, and every one of them is a bug we measured:
1. **that the decoder was unsure** (`PM` picked product over project in silence),
2. **that the decoder failed** (an exception and an unconstrained query are the same empty contract),
3. **which words became facets and which stayed prose** — needed to feed the sparse and dense legs
   differently (§5), and needed for the UI to show its work.

## 6. The lexical leg (the fix that is not about intent at all)

`austin` and `remote` would both be far better served by a keyword leg than by any decoder, and the
column already exists. Prior art is explicit that a hybrid index wants *different* treatment per
channel — "dense expands, sparse anchors" (arXiv 2608.15851) — and that rewriting the string fed to a
dense retriever can actively hurt when the query is already lexically aligned with the corpus
(arXiv 2603.13301: −9.0% nDCG@10 on FiQA, and selective-rewrite gating does not beat never-rewriting).

So: add a tsv leg to the evaluator's pool alongside the semantic legs (kernel mechanic, no domain
vocabulary), feed it the **residual text and the matched spans verbatim**, and feed the dense leg the
expanded/normalised form. Never replace the user's words in the sparse channel.

This is separable from the decoder and can ship first.

## 7. Kernel vs vertical

This is where proposals like this usually die, so it is stated as a table.

| Piece | Where | Why |
|---|---|---|
| `QueryIntent` dataclass, spans, ambiguity, confidence | **kernel** | a shape, names no domain noun |
| n-gram matcher over "the schema's closed value sets" | **kernel** | reads `FacetSchema`, which the vertical supplies |
| "which keys may be auto-inferred, and may they be a must" | **vertical manifest** | judgment, not mechanism |
| alias tables (metros, level synonyms, acronyms) | **vertical** | vocabulary |
| the offline lift aggregate: SQL, schema, lift maths, support floor | **kernel** | statistics over a typed entity set |
| the aggregate's CONTENT (cuda→semiconductors) | **vertical's corpus** | learned, never written down |
| the query-mode compiler prompt | **vertical** (as the extractor prompt already is) | domain wording |
| the tsv leg in `evaluate` | **kernel** | retrieval mechanics |

Litmus: a legal vertical supplies its own schema, aliases and corpus, runs the same offline job, and
gets `M&A → specialty:mergers` for free with the kernel untouched. That is the test this design is
built to pass.

## 8. Staged rollout, cost, and the evals that must exist first

Today a typed jobs search costs **one** `compile_contract` model call plus one query embedding.
Layers 0 and 1 add **zero** model calls and a single indexed lookup. Layer 2 is the call we already
pay. So the decoder is cost-neutral at query time; the only new spend is the offline aggregate.

| # | Ships | Flag | Proves it worked |
|---|---|---|---|
| 0 | **The eval set that does not exist.** ~60 short-query cases: bare value, bare metro, bare company, bare seniority, ambiguous acronym, deep technical term, JD URL. Today `evals/gold_people_jobs.json` has 27 cases and **not one** of these shapes; `compile_contract` is monkeypatched in every test that touches it. | — | it fails, loudly, on today's code |
| 1 | **Fix the coverage denominator (§2.1)** so `downgrade_uncovered_musts` can fire at all | — | a `must: level` on people is demoted instead of silently excluding 90% |
| 2 | tsv leg in the evaluator | `ROSTER_LEXICAL_LEG` | `austin` / `remote` recall on the new set |
| 3 | Layer 0 lexicon + `QueryIntent` + notes shown | `ROSTER_INTENT_LEXICON` | bare closed-vocab values become musts; no regression on the 27 existing cases |
| 4 | Ambiguity signal + "did you mean" affordance | `ROSTER_INTENT_CLARIFY` | `PM` stops silently choosing |
| 5 | JD-URL route → fetch → treat as profile | `ROSTER_JD_URL` | a greenhouse URL returns that job's neighbours, not a URL embedding |
| — | Layer 1 statistics | — | **blocked** on §2 preconditions |

Ordering note: stages 1 and 2 are not intent work at all, and both outrank it. A decoder that emits a
correct `must` against a key the index barely holds makes the product confidently wrong rather than
vaguely wrong, which is the worse failure.

**Trap cases (the repo's rule: every gate needs a case designed to pass it while being wrong).**
- `"we use Stripe for payments"` in a posting body must NOT make that posting a `company:stripe` hit —
  the boilerplate attack on Layer 1.
- `"remote"` inside `"remote possibility of travel"` must not become `work_mode:remote`.
- `"staff"` in `"staff the front desk"` must not become `level:staff_plus` — this is the case the
  model layer exists to adjudicate, and the eval must contain it.
- A bare metro that is also a person's name (`jordan`, `austin`) must not silently become a metro must
  on the PEOPLE tab.

## 9. Eigen — what generalises and what does not

Honest answer: **the knowledge generalises, the routing does not.**

Eigen's queries are research questions, and it already has a *better* decoder than the search side —
`derive_contract` (roster_kernel/research/contract.py:84-119) runs self-consistency voting with a
vertical-supplied prompt, and `query_expansion.expand_query` (apps/api/query_expansion.py:43) is a
real terse-query expander that is wired **only** to research and never to facet search. Meanwhile the
facet side has one unvoted, fail-silent call. The two never import each other.

So the general problem is real, but the reuse runs in the direction opposite to the one assumed:
- **Take from research → search:** self-consistency voting on a cheap model, and `expand_query`.
- **Take from search → research:** Layers 0 and 1. A research question naming "RTL verification"
  should get `prefer: industry=semiconductors` from the same lift table, grounded in the corpus
  rather than in the model's priors.
- **Do not share:** the route classifier. A keyword-vs-question decision is meaningless for a surface
  that only ever receives questions.

The kernel already contains three unrelated things called "contract" (the plugin SPI
`contract/manifest.py`, the facet `facets/contract.py`, the research `research/contract.py`). Any
unification starts by naming them apart; this spec does not attempt that.

## 10. Panel record

- **Gemini 3 Pro** — proposed the corpus-mined dictionary and the ambiguity UI, and argued against
  query-time LLM rewriting on cost and on the "Not All Queries Need Rewriting" evidence. Its
  `P > 0.95 → must` rule is **rejected** here for the boilerplate reason in §4; statistics propose,
  they never filter.
- **Code-grounded pass** — produced the map in §1.1/§1.2: the extractor-shaped prompt, the silent
  empty-contract fallback, the missing lexical leg, the stranded acronym table, the two decoders, the
  zero eval coverage.
- **Adversarial pass** — went to the production database and voided the corpus-statistics layer on
  evidence: `company_industry` has zero rows, person facets carry no extraction provenance, and the
  two best-covered person keys are 25-36% ingest artifact. It also surfaced §2.1. Its two surviving
  recommendations — cache `embed_query`/`compile_contract` for the real spend win, and mine to
  `prefer` only if ever — are adopted.
- **Live prod measurement** — the table in §1, which corrected the panel's shared assumption that
  "acronyms are broken". They are not; bare facet values are.
- **Codex — did not run.** Its CLI could not reach any model: the ChatGPT plan 404s on the
  `gpt-5.5` its config pins, every other model id is refused as "not supported when using Codex with a
  ChatGPT account", and the `OPENAI_API_KEY` in the environment is rejected as incorrect. Upgrading
  the CLI 0.142.5 → 0.153.4 did not change it. This needs an interactive `codex login`.
