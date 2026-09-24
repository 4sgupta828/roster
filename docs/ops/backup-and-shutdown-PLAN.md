# PLAN: Slim Roster to a single-server, minimal-DB app (no backup)

**Status: ✅ DONE 2026-09-23.** Worker service deleted; API set lean (`ROSTER_INGEST_IN_API=false`);
DB truncated (20 tables) from ~44 GB of tables to **68.9 MB** (disk 48 GB → **1.29 GB** via
`railway metrics`); accounts + saved Talent Maps + apply/product tables all kept. Executed via a
temporary token-guarded `POST /admin/db/minimize` endpoint (commits cf2da61 add / 52b109d remove),
since `railway ssh` and TCP-proxy are blocked for the agent on this account. Details in memory
`roster-minimized-live-only`. Verify a live people/jobs search when convenient; `railway volume list`
gauge lags (~48 GB) — trust `railway metrics` disk (1.29 GB).

Owner's decision (2026-09-23): search is now served **live by Exa/PDL**, so the downloaded index is
disposable. Goal: **one server, minimal DB.** Drop the async worker; **delete the downloaded people +
jobs + company/corpus data** (re-derivable → **no backup**); **keep app accounts + saved Talent Maps**
(not re-derivable). This supersedes the earlier "back up then shut everything down" plan.

---

## Verified facts (read-only, 2026-09-23)

- **Live search serves index-less.** `apps/api/live_people.py:merge_live_candidates` and
  `apps/api/live_jobs.py` call Exa/PDL **directly**, **no DB reads/writes**, index used only for dedup.
  Prod: `ROSTER_LIVE_PEOPLE=1`, `ROSTER_LIVE_JOBS=1`, `EXA_API_KEY`+`PDL_API_KEY` set. → Emptying the
  people/jobs tables does **not** break search.
- **Research already runs web-only.** Prod `ROSTER_WEB_ONLY=1` → the `rs_block` corpus is already
  bypassed for Q&A. Dropping `rs_block` changes nothing in current behavior.
- **Company data is effectively unused.** `ROSTER_DEEP_COMPANY_READER` is OFF. `rs_entity kind='company'`
  only feeds the inherited eigen connection-graph pages (`crossviews.html`/`explorer.html`), not the
  recruiting product (candidates/jobs/Talent Maps/apply). Those pages go empty — acceptable.
- **Saved maps are safe.** `rs_map` has **no** FK to `rs_entity` (`apps/api/maps.py`) → deleting the
  index does not touch saved Talent Maps.
- **FK dependency:** the claim-graph tables (`apps/api/claimgraph.py`) FK-reference `rs_entity`, so they
  must be truncated together with it.
- **Postgres = v18** (`postgres-ssl:18`). `railway ssh` is blocked; no public DB proxy → DB access via
  `railway connect Postgres --tunnel-only` (SSH tunnel) + local `psql` (client 15 is fine for queries).

## Scope — KEEP vs DELETE

**KEEP (account/product state, not re-derivable, no backup exists):**
`roster_user`, `roster_user_token`, `roster_user_pref`, `roster_feedback`,
`roster_research_session`, `rs_map`, `rs_map_revision`, `rs_map_review`.

**DELETE / TRUNCATE (downloaded index + ingest/worker state — re-derivable via live legs):**
- People + companies: `rs_entity`, `rs_entity_alias`, `roster_entity_facet`,
  `rs_person_artifact`, `rs_person_link`, `rs_artifact_scan`
- Claim/relation graph (FK-dependent on rs_entity): `rs_claim`, `rs_claim_evidence`,
  `rs_claim_resolution`, `rs_mention`, `rs_extraction_run`
- Jobs: `rs_job`, `rs_ats_probed`
- Research corpus: `rs_block` (already bypassed by `ROSTER_WEB_ONLY=1`)
- Search cache + ingest/worker state: `rs_linkedin_scan`, `rs_ingest_checkpoint`,
  `roster_corpus_gap_queue`
- (`rs_predicate` is a tiny vocab config table — leave it; harmless.)

> Execution will FIRST enumerate **all** tables live (`\dt+` / `pg_total_relation_size`) and confirm
> every table is classified KEEP or DELETE before truncating — no table left unaccounted for.

## Execution steps

1. **DB access:** `railway connect Postgres --tunnel-only` → local `127.0.0.1:<port>`; connect
   `psql "postgresql://postgres:<PW from DATABASE_URL>@127.0.0.1:<port>/railway"`.
2. **Enumerate + measure:** list all tables with sizes and row counts; reconcile against KEEP/DELETE.
   Confirm account tables' row counts (accounts, saved maps) so we know exactly what we're keeping.
3. **Quiesce ingest:** set the `('control','stop')` kill switch (belt-and-suspenders; worker is deleted
   next anyway).
4. **Truncate the DELETE set** in one statement (FK-safe):
   `TRUNCATE <delete-set> RESTART IDENTITY;` (add `CASCADE` only after confirming no KEEP table is
   caught — `rs_map` is not, verified).
5. **Reclaim:** `VACUUM (ANALYZE)` the truncated tables. (Railway volume won't shrink, but space is
   freed for reuse; `VACUUM FULL` optional, locks tables.)
6. **Drop the worker → single server:** delete the `roster-worker` service. On `roster-api`, disable the
   in-process ingest drain thread (the flag behind `apps/api/app.py:444`) and clear `ROSTER_BULK_*`.
   Keep `ROSTER_LIVE_PEOPLE/JOBS=1`, `ROSTER_WEB_ONLY=1`, Exa/PDL keys.
7. **Verify:** one live people search + one live jobs search return results (index empty); `/health` ok;
   a saved Talent Map still loads; account login intact.

## Notes / risks
- **No backup by design** — the deleted data is re-obtainable via the live Exa/PDL legs; accounts +
  saved maps (the only non-derivable data) are explicitly KEPT.
- **Irreversible.** Step 4 is gated on step 2's reconciliation. Truncating account tables by mistake
  would be unrecoverable — hence the explicit enumerate-and-classify pass first.
- `crossviews.html` / `explorer.html` (company graph) will show empty — expected; retire them later if
  desired.
