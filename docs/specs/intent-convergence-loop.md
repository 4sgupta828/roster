# The Intent Convergence Loop — search as steering, not as a single answer

Status: spec v1 (2026-09-09). Panel: Codex (gpt-5-pro), Gemini 3 Pro, a code-grounded pass, and the
prior-art review in §9. Companion to `docs/specs/query-intent-decoding.md`, which decodes the FIRST
contract; this spec is about every turn after it.

Owner ask, in substance: *"Intent may be more subtle. We may even ask specific questions back to the
user to disambiguate. The intent crunching is to NEVER ONE SHOT. We understand something about the
user's intent, we return SOME results (not a whole lot), and present a few DIRECTIONS to choose from
that emerge from the query AND the results so far — pick one of 3 focus areas at a time if results
don't look right, relaunch returning 10 results, re-check alignment, and pick another focus area if
alignment is missing, or simply EXPAND if it is there. The focus areas are crunched from the user's
input and from the results: seniority, location, remote/hybrid, position clusters that emerge from the
collection of results."*

---

## 1. The finding that should change the plan: this is ~90% built, in the wrong place

The panel's job was to design a mechanism. The code-grounded pass found most of it already written,
domain-free, in the kernel — and already wired into the Guided consultant, where it stops one step
short of what the owner is asking for.

**`leverage()` — `packages/kernel/roster_kernel/facets/brief.py:271-284`.** Its own docstring:

> *"The keys whose values split the current pool the most (spread) — the measured forks a planner may
> pick from."*

It takes the evaluator's `counts`, computes `spread(dist) = (1 − max_share_among_known, known/total)`,
drops anything under `min_spread=0.3` or `known < 0.5`, and returns **the top 3 keys with their top-4
values and counts**. That is "pick one of 3 focus areas … seniority, location, remote/hybrid",
already implemented, already domain-free, already measured against the live pool.

**`grouping.py` — the emergent half.** `eligible()` (`grouping.py:42-85`) decides whether a dimension
may be offered *for the rows in front of the user* (`max_unstated 0.25`, `max_largest 0.60`,
`min_groups 2`, `min_repeats 3` for identity dimensions), scoring `balance × known`. `token_groups()`
(`:125-157`) is the free deterministic fallback that finds the distinctions INSIDE a set, discarding
any token held by more than 60% of rows because *"that is the query, not a distinction"*. That is
"position cluster types that emerge from the collection of results" — the part `leverage` cannot do,
because it only knows keys the schema already has.

**Guided v3 already runs the loop — and stops at READY.** `_preview` (`apps/api/consultant.py:487-512`)
evaluates the working contract every turn at `limit=10, depth={"counts": False}` and feeds the pool
size, `best_match`, `weak` and eight top role lines to the planner. `_probe_options` (`:571-586`)
costs every proposed option against the slice *before* offering it, so no option leads to zero.
`leverage(...)` is handed to the planner as "LEVERAGE (what splits the pool; counts)" and the planner
must choose its options from that list (`roster_vertical/consultant.py:187-188`).

So a small-batch, measured, option-costed, pool-aware question loop **exists and is live in prod**
(`ROSTER_GUIDED_V3=1`). What it does not do is the owner's actual ask: it terminates at READY, hands
off one contract to one search, and **nothing ever feeds the RETURNED RESULT ROWS back into a next
question.** The loop is pre-search. The ask is for it to continue after the results.

**The answer to "compose or build" is therefore: extend, do not invent.** Anything else rebuilds
`leverage`, `eligible`, `token_groups` and `_probe_options` under new names.

## 2. What genuinely does not exist

1. **No post-results question.** v3's `IntakeState` never holds rows; its only pool signal is `counts`
   over the must-slice. After READY the rail takes over and the conversation ends.
2. **The follow-up is not a refinement — and the two surfaces get there differently.** Verified by
   reading both paths, because the distinction changes what the fix is.

   - **Jobs: dead code.** `refine_query` is read at exactly one place, `app.py:3314`, which sits
     *after* the `facet_evaluator_enabled()` branch at `:3151` has already returned. Its own comment
     describes a real feature — *"turns 2+ carry the previous query; the model applies the utterance
     to it — narrowing, expansion, removal, or replacement"* — that **cannot execute in prod**, where
     the evaluator is on. Unintended, and a straightforward thing to reconnect.
   - **People: deliberate.** `_fresh = not (body.refine_facets or prior_person or prior_context)`
     (`app.py:4253`) gates the evaluator at `:4254`, and the comment above it says so on purpose: the
     old engine *"keeps what it is better at: a person's NAME … and follow-up refinement turns that
     carry the running filter."* So a talent refinement leaving the evaluator is a decision, not a
     slip.

   Either way the consequence is the same and it is the precondition for this loop: **today a second
   turn does not inherit the first turn's contract.** But the fix is not one change. Jobs needs a dead
   path reconnected; people needs a deliberate trade re-opened — bring refinement into the evaluator
   and establish, with the trajectory evals in §5, whether the thing the old engine was "better at"
   survives the move.
3. **No undo, no back.** Neither intake has one; `asked`, `counts_asked` and the contract only
   accumulate. v3 has `restart` only. The owner's v2 complaint named this.
4. **No UI that offers a CHOICE of directions after results.** The rail is a filter list with counts;
   group headers only expand and collapse (`index.html:8836-8838`) — tapping a group does not narrow
   the search. The only fork-with-options UI in the product is the intake's question chips, in a
   different tab, before the search.
5. **No person grouping vocabulary.** `GROUP_DIMENSIONS` (`roster_vertical/job_grouping.py:12-20`) is
   the only dimension table in the repo, and it is applied to person rows as-is (`app.py:4366`); the
   legacy people path returns no `group_options` at all.

---

## 3. The loop

```
decode (spec 1)  →  evaluate  →  GATE: is steering warranted?
                                   │no → results, and nothing else. Done.
                                   │yes
                                   ▼
                         3 DIRECTIONS, ranked, each costed against the slice
                                   ▼
                    user picks one  ·  or ignores them and scrolls  ·  or types
                                   ▼
                      re-evaluate (same text, changed contract)
                                   ▼
                         ALIGNMENT, inferred before it is asked
                            aligned → EXPAND (more rows, same contract)
                            not     → 3 fresh directions, computed from the NEW rows
```

Every arrow is an existing call. The only new code is the gate, the direction ranker that merges two
existing sources, the alignment read, and the contract stack.

### 3.1 The three directions

Two sources, one ranked menu:

- **`leverage(counts, schema, top=N)`** — schema keys whose values split the *slice*: seniority,
  location, work mode, company type. Already returns exactly this, with counts per value.
- **`token_groups(row_tokens)`** — the emergent clusters `leverage` structurally cannot see, because
  they are not schema keys: *"these are mostly agency reposts"*, *"half of these are Series-A"*,
  *"there's a cluster of infra roles and a cluster of ML roles"*.

Rank both into one list of 3. `leverage` yields `spread ∈ [0,1]`; `eligible`/`token_groups` yield
`balance × known` — comparable in shape but not calibrated to each other. **Normalise to a common
"expected fraction of the slice this removes" and prefer the direction closest to a 50/50 split**,
which is the standard information-gain argument and matches what both scorers already approximate.
Ties break toward the key the user has not yet constrained.

**Why three:** it is a menu, not a form. Beyond about three, a choice becomes a filter list — which is
the rail, which already exists, and which is the thing this is meant not to be.

### 3.2 A direction may narrow, exclude, or re-centre

The owner's framing is "if results don't look right". The most informative move is often **not a
must** — it is *"not this"*. The Contract already carries `avoid`, and `token_groups` is exactly what
surfaces the thing to avoid (a cluster of agency reposts is visible in the rows and invisible in the
schema). So a direction is one of:

- `must` — "only staff+"
- `avoid` — "drop the agency reposts"
- `center` — "centre on senior, keep the neighbours" (the ordinal case; `center` already exists)

Neither panellist proposed `avoid`, and it is the cheapest way to act on a negative reaction.

### 3.3 The gate — when to shut up

"Never one-shot" is the one part of the ask this spec argues with, and the evidence is unusually
direct (§9): low-quality clarifying questions measurably **disturb** users; showing only high-quality
ones yields better outcomes with less effort; short and ambiguous queries benefit significantly while
long precise ones do not. Someone who types *"staff backend engineer at Stripe in NYC"* must be shown
results and nothing else.

Two consequences:

1. **Directions are ignorable chips, never a blocking question.** The loop is offered, not imposed.
   "Never one-shot" becomes "never *finish* in one shot" rather than "never *answer* in one shot".
2. **The gate is already computed.** We do not need a new heuristic — spec 1's decoder emits an
   `ambiguity` signal, and the evaluator already returns the rest:

| Signal | Where it already comes from | Reading |
|---|---|---|
| `ambiguity[]` non-empty | spec 1's decoder | the query itself was unclear → steer |
| `coverage.weak`, `best_match` low | `evaluate.py:207-224` | nothing matched well → steer |
| flat `match_pct` across the page | per-row, `calibrated_pct` | no row stands out → steer |
| `coverage.diagnosis` present | `evaluate.py:215-221` | a must is collapsing the slice → steer |
| thin `pool` | `coverage.pool` | too few rows to split → **do not** steer, widen instead |
| tight top-ranked cluster | `match_pct` spread | already aligned → **say nothing** |

### 3.4 Alignment, inferred before it is asked

Asking "does this look right?" every turn spends the scarcest thing we have. Roster already records
what alignment looks like: `/me/buckets` items (shortlisting, `app.py:7423-7431`), `/me/applications`
(`:7601`), an opened job summary (`:3724`), a saved map. **A user who shortlists 3 of 10 is aligned; a
user who shortlists none and immediately retypes is not.** Read those first; ask only when the
behavioural signal is absent *and* the slice signals in §3.3 say the results are weak.

### 3.5 The batch: return 10, but evaluate wide

The naive reading of "return 10" breaks the feature. `_group_options` runs on the **returned rows**
(`app.py:6408-6419`), and `eligible` on 10 rows is a different test: `max_unstated 0.25` means three
unknowns kill a dimension, `max_largest 0.60` means seven of ten sharing a level kills it, and identity
dimensions need three repeating companies inside ten rows. Most dimensions would be judged ineligible
and the menu would go empty exactly when it is needed.

But `limit` and `cap` are already independent (`evaluate.py:71`: `cap = max(limit × 4, 160)`), and
`counts` are slice-wide, not page-wide (`evaluate.py:203-210`). So: **evaluate wide, show ten.**
Directions come from `counts` via `leverage` (slice-wide, unaffected) and from `token_groups` over the
evaluated pool rather than the ten shown rows. "Expand" then means paging the contract we already ran —
not a new search — which is already client-side at 20/page (`index.html:11093`).

### 3.6 State, and the undo that was missing

The intake endpoints are already **stateless on the server — the whole state rides the request**
(`IntakeIn.state`, `app.py:1314`; FE `INTAKE.state`, `index.html:6005`). Keep that. The loop's state is
a **stack of contracts** on the client turn record, which the FE already stores per turn
(`index.html:3068-3074`) and currently never sends back.

That gives undo for free — the thing the owner's v2 verdict called out ("It doesn't allow starting
over"): back = pop the stack and re-run the previous contract through `/search/evaluate`, which the
rail's Apply already does (`facetReevaluate`, `index.html:8794-8828`). Saved maps snapshot a contract,
so they keep working unchanged.

---

## 4. Kernel vs vertical

| Piece | Where | Note |
|---|---|---|
| `leverage`, `spread`, `eligible`, `token_groups`, `enforce` | **kernel, already there** | reused verbatim |
| merging the two scorers into one ranked menu of 3 | **kernel** (new, small) | a normalisation rule, names no domain noun |
| the gate's thresholds (what counts as "weak", "flat", "thin") | **kernel** mechanism, **vertical** numbers | same split the evaluator weights already use |
| which keys may be offered as a direction, and their wording | **vertical** | `GROUP_DIMENSIONS` extended; a PERSON table added (§2.5) |
| naming an emergent cluster in words | **vertical prompt**, capped (§6) | the one place a model is justified |
| the alignment signals (a bucket item, an application) | **vertical** | "shortlist" and "applied" are domain nouns |
| the contract stack / undo | **client** | server stays stateless, as the intakes already are |

## 5. Rollout

| # | Ships | Flag | Proves it |
|---|---|---|---|
| 0 | **Fix the refinement path (§2.2)** — make the evaluator branch read `refine_query`/`refine_facets` instead of recompiling from scratch, and stop a people refinement from falling out of the evaluator | — | a second turn keeps the first turn's contract; today it does not |
| 1 | Return `directions[]` on the search response — `leverage` ∪ `token_groups`, ranked, top 3, each costed by slice as `_probe_options` already does | `ROSTER_DIRECTIONS` | the menu is non-empty and no direction leads to zero rows |
| 2 | Render them as ignorable chips under the gate (§3.3); a click stages a contract edit and re-runs — the rail's Apply path, reused | `ROSTER_DIRECTIONS` | a precise query shows no chips; `remote` / `austin` do |
| 3 | Contract stack + back (§3.6) | — | the v2 complaint is answered |
| 4 | Evaluate-wide/show-ten + expand-as-paging (§3.5) | `ROSTER_SMALL_BATCH` | first screen is faster; `group_options` does not go empty |
| 5 | Inferred alignment (§3.4) → directions re-ranked, or suppressed when the user is clearly converging | `ROSTER_ALIGNMENT` | a shortlisting user stops being interrupted |
| 6 | Person `GROUP_DIMENSIONS` (§2.5) | — | talent search gets directions of its own vocabulary |

**The evals, before any of it.** The existing gold set has 27 cases and no multi-turn case at all. This
needs a *trajectory* eval: a start query, a scripted pick, and an assertion about turn 2 — because the
failure this design risks is not a bad answer, it is a bad *sequence*.

Trap cases, in the repo's tradition (designed to pass the gate while being wrong):
- A precise query (`staff backend engineer at Stripe in NYC`) must show **no** directions.
- A direction must never be offered that removes every row — `_probe_options` already costs options;
  reuse it rather than reinventing.
- A direction whose two branches are 97/3 must not be offered: it looks like a choice and is not one.
- Picking a direction then going back must land on the EXACT prior contract, not a recompile.
- The emergent-cluster namer must never invent a cluster that is not in the rows — it names what
  `token_groups` found; it does not decide what is there.

## 6. Cost

The loop multiplies turns, so per-turn cost is the whole design constraint.

Today, one typed search is **1 compile call + 2–7 embedding calls** (one per semantic leg: 1 semantic
+ ≤3 angles + ≤2 prefer, `evaluate.py:83-96`), and `noise_floor` embeds again. **Neither the compile
nor the embedding is cached on the search path** — the compile cache exists only on `/search/compile`
(`app.py:6779-6789`), and `embed_query` has a circuit breaker but no cache. N turns cost N times that,
with no amortisation.

Two levers already exist and make the loop nearly free:

1. **Cache the query embedding on the text.** A direction changes `must`/`avoid`, never `text` — so
   every turn after the first re-embeds a string it has already embedded. One cache keyed on
   `(kind, text)` collapses 2–7 calls per turn to zero.
2. **`depth={"counts": False}`** (`evaluate.py:60-65`) on intermediate turns, as `_preview` already does.

Half of this is already true: a direction click goes through `/search/evaluate`, which is documented
as *"deterministic for a given index state (no model call)"* (`app.py:6857`) — the **compile is already
skipped** on a contract re-run. What remains is the embedding, because `evaluate` still calls
`store.semantic(contract.text)` per leg. So the text-keyed embedding cache is the ONLY thing between
today and a direction click that costs **nothing at all** — a Postgres re-filter and a re-rank. That is
what makes "never one-shot" affordable, and it is the same caching item spec 1 ranked second.

(And when the direction makes the query pure-facet, spec 1's §0a text-blanking removes the embedding
outright: no text, no semantic leg, the must-slice IS the pool.)

**No model call in the loop's critical path**, with one exception: naming an emergent `token_groups`
cluster in human words. Cap it — one call, only when the cluster is offered, cached on the cluster's
token signature.

## 7. Eigen

The mechanism generalises; the vocabulary does not. `grouping.py`'s header is explicit — *"Nothing here
knows what a job, a company or a skill is"* — and `leverage` takes any `counts` over any schema. For
Eigen the "rows" are claims and findings, so the directions become *which sub-question to pursue*,
*which entity to focus on*, *which time window* — computed the same way from whatever the research
contract's counts are.

The honest caveat: research questions are not short and ambiguous the way `remote` is, so §3.3's gate
will fire far less often. The loop is worth more to search than to research, and should ship there
first.

---

## 8. What we will NOT build

- **No post-results chat.** The rail plus three chips is the interface. A conversational panel under
  the results is a second search surface, and the last spec's whole finding was that a second surface
  is how the product got two decoders.
- **No blocking "does this look right?" modal.** §3.3 and §9.
- **No LLM in the direction selector.** `leverage` and `token_groups` are deterministic, free and
  already written. A model is justified only to put a *name* on an emergent cluster, capped and cached.
- **No new state store.** The intakes are stateless on the server; the loop stays that way.
- **No fixed question order.** That is precisely what made v2 "a form" in the owner's words. Order is
  whatever splits *this* pool.

## 9. Prior art

- **[Asking Clarifying Questions: To benefit or to disturb users in Web search?](https://irlab.science.uva.nl/wp-content/papercite-data/pdf/zou2022asking.pdf)**
  (Zou et al., IPM 2023). Low-quality clarifying questions **disturb** users; always showing them is
  risky; showing only high-quality ones "receives better gains with less effort". **This is the direct
  argument for §3.3's gate, and against a literal reading of "never one-shot".**
- **[Clarifying the Path to User Satisfaction](https://arxiv.org/abs/2402.01934)** (2024). Specific
  questions beat generic ones; **short and ambiguous queries benefit significantly** from
  clarification while longer, less ambiguous ones do not. **This is why the gate keys off spec 1's
  ambiguity signal, and why a direction must name a real split in the rows rather than ask a generic
  "narrow by seniority?".**
- **[Towards Facet-Driven Generation of Clarifying Questions](https://dl.acm.org/doi/10.1145/3471158.3472257)**
  (SIGIR ICTIR 2021) and the ClariQ line of work, where a *facet* is the underlying information need
  and clarification is generated from facets. **Roster's version is stronger than the literature's
  default: our facets are measured against the live pool by `leverage`, so a direction is never
  offered unless the index can actually deliver both branches.**
- **[Analysing Mixed Initiatives and Search Strategies during Conversational Search](https://strathprints.strath.ac.uk/77947/1/Aliannejadi_etal_CIKM_2021_Analysing_mixed_initiatives_and_search_strategies_during_conversational_search.pdf)**
  (CIKM 2021). Clarification is the largest single category of mixed-initiative interaction. Supports
  the loop as a shape; does not license asking every turn.

## 10. Panel record

- **Codex (gpt-5-pro)** — RUNNING at the time of writing; its section will be appended. Worth recording
  separately: on `gpt-5-pro` a repo-grounded design review takes **40+ minutes**. That is fine for a
  panel and unusable for anything interactive.

- **Gemini 3 Pro** — independently reached "compose, do not build", named Hick's Law for the choice of
  three, and got the two things that matter most right: directions must be **ignorable chips, never a
  blocking modal**, and the "shut up" gate is *"the most critical piece of code you will write for this
  feature"*. Its proposed gate signals (thin pool; `match_pct` clustered at the top) are adopted in
  §3.3. Where this spec goes further: it did not propose `avoid` directions, it did not catch that
  `eligible` on a 10-row batch collapses the menu (§3.5), and it assumed `_group_options` alone is the
  selector, missing `leverage` — which is the function actually written for this job.
- **Code-grounded pass** — found `leverage()`, established that Guided v3 already runs a per-turn
  mini-search and costs its options, and produced §2's list of what is genuinely missing. Its most
  consequential single finding is §2.2: with the evaluator on, `refine_query` and `refine_facets` are
  dead on the paths that matter, so there is no turn-to-turn continuity today to build a loop on.
- **Live measurement** — the queries in `docs/specs/query-intent-decoding.md` §1 are the ones this loop
  is for: `remote`, `staff`, `austin`, `PM`. The precise query in the same table is the one it must
  leave alone.
