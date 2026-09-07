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

- The 126 thousand people still on legacy rows rank and filter less precisely in Talent searches; some schema keys
  (specialty, evidence kinds, work type) are missing or coarse for them.
- Facet COUNTS on the rail mix precise and bridged rows for those people, so chip numbers for them are approximate.
- Nothing is broken and no search fails; the corpus is simply less sharp for a third of the people.

## 7. The decision to revisit

Finishing the remaining 126 thousand costs roughly 45 % of what has been spent on this job so far. Two cheaper options
worth weighing before a blanket resume:

1. **Re-extract on demand**: extract a person the first time they appear in a result set, so spend follows use.
2. **Re-extract by value**: finish the segments that searches actually reach (software / data engineering / leadership in
   the top metros) and leave the long tail bridged.

Either would need a small change to `apps/worker/bulk_ingest.py`; the blanket loop is what exists today.
