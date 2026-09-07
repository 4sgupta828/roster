# PAUSED: the people facet re-extraction — why, how far it got, how to resume

**Status: PAUSED 2026-09-07 by the owner. It is unfinished and must be resumed.**
`ROSTER_BULK_PEOPLE_FACETS` is set to `0` on the **roster-worker** service. That is the only thing stopping it.

---

## 1. What this job is

Every person in the index needs facets (field, function, level, work type, skills, specialty, metro, evidence …) in the
CURRENT schema, because facets are what the ONE search operation filters and ranks on
(`docs/specs/facet-contract-evaluator.md`). People arrived with the people index's older vocabulary, so each person is
re-read by a model and written back under `provenance='profile'` with the schema version; the person's bridged `legacy`
rows are deleted as each one lands (`roster_vertical/facet_legacy.py`, `apps/worker/bulk_ingest.py`).

Until a person is re-extracted, they are searchable only through the bridged legacy vocabulary, which is coarser: a
Talent search filters and ranks them less accurately, and some schema keys are simply absent for them.

## 2. How far it got (measured at the pause, 2026-09-07)

| | people |
|---|---|
| re-extracted into the current schema (`provenance='profile'`, `schema_version='77c3be380247'`) | **281,335** |
| still on bridged legacy rows — the remaining work | **126,321** |
| total active people in the index | 414,197 |

**67.9 % complete.** Started 2026-09-05 (owner-approved, projected at the time as ≈ $12); it has run continuously since,
apart from the two provider outages below.

## 2b. What it changes, in one person's facets

The same shape of person — a GitHub-sourced engineer — before and after. Names and links are omitted here; only the
facets matter. **Left: still bridged.** The people index's old vocabulary is translated by one table
(`roster_vertical/facet_legacy.py`) into four schema keys. **Right: re-extracted.** The model reads the profile and
writes the schema's own keys, each with its provenance.

| | bridged (legacy) | re-extracted (`provenance='profile'`) |
|---|---|---|
| field | `software` | `data_ml` |
| function | `engineering` | `research` |
| level | `mid` | (stated when the title states one) |
| work_type | `ic` | `academic` |
| **role_family** | — | `graduate student` |
| **specialty** | — | `machine learning`, `big data` |
| **skill** | — | `machine learning`, `big data` |
| **metro / state** | — | `atlanta` / the state |
| **evidence** | — | `repos` (from linked artifacts) |
| company | — | the current employer, normalized |

The bridge can only carry what the old vocabulary already knew. Everything in bold is a key the search operates on that
simply **does not exist** for a person until they are re-extracted.

### How complete each key is, per group (measured 2026-09-07)

| facet key | people still bridged | people re-extracted |
|---|---|---|
| role_family | **0.0 %** | 83.1 % |
| work_type | 13.8 % | 64.5 % |
| metro | 12.2 % | 45.9 % |
| specialty | **0.0 %** | 30.1 % |
| state | **0.0 %** | 18.8 % |
| skill | 1.9 % | 17.6 % |
| level | 2.1 % | 13.4 % |
| evidence | 0.2 % | 11.9 % |
| field | 80.5 % | 68.1 % |
| function | 100 % | 77.6 % |

## 2c. Why it matters — what the search can and cannot do

The contract is the only search operation: `must` filters, `prefer` ranks, `center` centres, and all three read facets
(`docs/specs/facet-contract-evaluator.md`). A person who lacks a key is not "ranked lower" on it — they are **outside the
question**:

1. **A filter on a missing key can never match them.** `matches_must` requires the value. Today the whole index reaches
   only 432 people by `role_family = backend engineer`, 56 by `specialty = payments`, 687 by `skill = kubernetes` and
   15,986 by `evidence = repos` — and essentially every one of those is a re-extracted person. The 126,321 bridged
   people cannot appear in such a search however well they actually fit.
2. **They cannot earn preference points either.** Ranking adds points per matched preferred value, so a bridged person
   can only ever score on `field` and `function`. Two equally relevant people rank differently purely by whether the
   extraction has reached them — a systematic bias against a third of the index, invisible in the results.
3. **The rail's chip counts describe the extracted two-thirds.** Bridged people fall into the `unknown` bucket for
   level, metro, specialty and evidence, so those numbers understate the real pool and the navigation misleads.
4. **Smart relaxing and the empty-pool diagnosis reason about the wrong pool.** When a search comes back thin, the
   diagnosis blames the filter that "emptied" it, when the real cause can be that the matching people have no facets yet.

### The search, before and after — a hiring manager's ask

> "Backend engineers who have built payments systems, staff level, in Seattle."

| | bridged person | re-extracted person |
|---|---|---|
| `prefer role_family: backend engineer` | no value → no points, cannot be a must | matches |
| `prefer specialty: payments` | no value → invisible to the decisive filter | matches |
| `center level: staff_plus` | usually `unknown` → no centring | centred |
| `must metro: seattle` | 88 % have no metro → excluded by a geo must | matches |
| `prefer evidence: repos` | no value | matches, and the card shows the proof |

The bridged person is reachable only by free-text similarity and the two coarse keys. That is precisely the "55 % match
for a pharma role" failure the facet spec was written to end.

## 3. Why it was paused

The job is the dominant consumer of model credit in the whole product — roughly 281 thousand model calls so far, which is
two orders of magnitude more than all interactive traffic (searches, Guided intake, evals) combined. It runs 24/7 and
resumes the instant credit appears, so a top-up intended for product use is consumed by this loop within hours.

On 2026-09-07 the DeepSeek balance reached **-0.11 USD** (exhausted) and the OpenAI account had already hit
`429 insufficient_quota`, which took **all text search** down until the degraded-search guarantees landed
(`docs/specs/guided-consultant-v3.md` §11.3). The owner chose to pause the background job so that the next top-up goes to
the product, not to this loop.

**Nothing is lost by pausing.** The job is idempotent and resumable: it re-reads only people who still carry legacy rows,
so it picks up exactly where it stopped.

## 4. How to resume

```bash
railway variables --set "ROSTER_BULK_PEOPLE_FACETS=2000" -s roster-worker   # batch size; the worker restarts itself
railway service status -s roster-worker                                     # wait for SUCCESS
```

`0` (or unset) disables the loop entirely (`apps/worker/bulk_ingest.py:118`). There is also a hard kill switch that stops
every bulk loop without a redeploy: the `('control','stop')` row in `rs_ingest_checkpoint`.

**Before resuming, check the credit.** The loop routes through `apps/api/model_json.py` (DeepSeek first, OpenAI as the
fallback), so an empty DeepSeek balance silently spends OpenAI credit instead — and OpenAI credit is what query
embeddings need. Query embeddings are OpenAI-only and must match the vectors already stored in the index, so DeepSeek
cannot substitute for them.

```bash
# DeepSeek balance, from inside the API container
railway ssh --service roster-api "python -c \"
import os,json,urllib.request
r=urllib.request.Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']})
print(json.load(urllib.request.urlopen(r,timeout=20)))\""
```

## 5. How to check progress at any time

```sql
-- re-extracted vs remaining (run in the API container against ROSTER_CORPUS_DSN)
SELECT
  (SELECT count(DISTINCT entity_id) FROM roster_entity_facet
     WHERE entity_kind='person' AND provenance='profile' AND schema_version <> '') AS extracted,
  (SELECT count(DISTINCT entity_id) FROM roster_entity_facet
     WHERE entity_kind='person' AND provenance='legacy') AS legacy_remaining,
  (SELECT count(*) FROM rs_entity WHERE kind='person' AND status='active') AS total;
```

The public endpoint `GET /admin/people-coverage` shows index counts by source (not extraction progress).

## 6. What is degraded while it stays paused

- **126,321 people (30 % of the index) are unreachable by role, specialty, skill, state and evidence filters** — see
  §2b / §2c. They are not ranked low; they are absent from those questions.
- Their ranking can only score on `field` and `function`, so they sit below equally relevant re-extracted people.
- Facet COUNTS on the rail put them in `unknown` for level / metro / specialty / evidence, so chip numbers understate.
- Nothing is broken and no search fails: free-text similarity still reaches them, and every plain search still answers.

## 7. The decision to revisit

Finishing the remaining 126 thousand costs roughly 45 % of what has been spent on this job so far. Two cheaper options
worth weighing before a blanket resume:

1. **Re-extract on demand**: extract a person the first time they appear in a result set, so spend follows use.
2. **Re-extract by value**: finish the segments that searches actually reach (software / data engineering / leadership in
   the top metros) and leave the long tail bridged.

Either would need a small change to `apps/worker/bulk_ingest.py`; the blanket loop is what exists today.
