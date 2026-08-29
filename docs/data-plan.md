# Roster — Data-Download Plan (panel-reviewed) + Decoupling Plan

Reviewed by panel (Codex GPT-5.x, Gemini 3 Pro, code-grounded subagent). The code-grounded
review found the initial plan **not executable at scale** without fixes; this revises it.

## Strategy (panel consensus)
**Density over breadth** — pick ~30–50 flagship AI-sector companies and map each fully (SEC
filings + granted patents + top GitHub repos + founders' OpenAlex papers), heavily
cross-referenced so the corpus proves **multi-hop diligence** (10-K → patent → repo). ~600–1000
documents to start. Corpus-first for durable evidence; **defer sentiment (GDELT/HN)** entirely.
Honest framing: until EDGAR-by-CIK + Form D land, this is a **tech-landscape / prior-art** corpus,
not yet a private-company-diligence corpus.

## Phase 0 — Pre-ingest code fixes (REQUIRED before any scaled ingest)
1. **Wire a generic per-job facet override** (esp. `sector`) through the queue + `/admin/corpus/ingest`,
   exactly like `source_country` is wired (`app.py:1273-1279`, `gap_queue.py:31-32`,
   `materialize.py:55,66`). Without this, `sector=ai` is never stamped and the corpus is unscoped.
2. **Add a max-token cap to the splitter** (`corpus/splitter.py`) so a long paragraph can't exceed
   OpenAI's 8192-token embedding input and fail the whole batch.
3. **Add retry/backoff + a rate limiter** to the connector HTTP (`connectors/_http.py`): honor SEC
   ≤10 req/s, arXiv ~1 req/3s, GitHub auth 5000/hr, OpenAlex 100k/day + mailto; 429/503 → backoff,
   not a dead job. Set a **real** contact User-Agent (not `@roster.example`).
4. (Optional now) skip re-embedding docs already in Postgres (cost-safety on re-runs).

## Phase 1 — Scale arXiv (the only fully-working path)
Ingest ~200–300 arXiv papers across the target entities' topics (LLM inference, RAG, agents,
training/eval), stamped `sector=ai`. arXiv = `technical_signal` tier (unreviewed).

## Phase 2 — Build OpenAlex connector
Peer-reviewed works (`verified_structured` tier) + citation counts; **DOI-dedup against arXiv** so
the same paper isn't embedded twice at two tiers. ~150–250 works for the target entities/founders.

## Phase 3 — REBUILD the EDGAR connector (not a tweak)
CIK lookup → `data.sec.gov/submissions/CIK…` → fetch the primary document HTML → real section split
(Business / Risk Factors / MD&A) → XBRL financial facts (`companyfacts`). Add **Form D** (private
raises). Fix FTS pagination (`from`/`size`). Then ingest the 30–50 companies (~150–250 filings) as
`primary_filing`. Mitigate forward-looking-as-fact: mark MD&A/forward-looking sections so the answer
labels intent vs realized (statement-level caveat), and add a **recency gate** (latest filing wins).

## Phase 4 — PatentsView + GitHub
Patents with `grant_status` (granted = `primary_filing`; pending = `technical_signal`); GitHub repo
traction (`technical_signal`) for the entities' flagship repos. ~50–100 each.

## Phase 5 — Verify (quality, not counts)
Assert `length(text) > N` AND `facets ? 'sector'` per source (NOT `count(*)`). Run a held-out
multi-hop diligence Q&A battery (funding, tech/IP, competition, team) — grounded, verbatim-cited, at
the declared tier; sentiment questions must be labeled signal. Near-dup + stale-filing checks.

## Grounding traps to gate (panel)
Forward-looking-as-fact · pending-vs-granted patents · GitHub-stars-as-adoption · parent/subsidiary +
CIK/ticker misattribution · sentiment-as-fact · arXiv abstract truncation (don't infer methodology) ·
near-duplicate across tiers · stale-filing/wrong-period. "Never controlling" ≠ "never cited" — verify
the tier LABEL on the answer, not just presence.

---

## Decoupling / de-medicalization plan (from the code inventory)
Independent workstream; do FIRST so the data work lands on a clean base.

### Rebrand (no "noesis" anywhere; fixes the kernel=roster_ / apps=noesis_ split-brain)
- Rename ALL `apps/api` DDL + queries `noesis_*` → `roster_*` **per module atomically**:
  `noesis_research_session` (sessions.py), `noesis_user*`/`noesis_feedback` (accounts.py),
  `noesis_corpus_gap_queue` (gap_queue.py), `noesis_setting` (settings.py),
  `noesis_glossary_term` (glossary.py), `noesis_perf_event` (perf.py). Fresh prod DB → no migration,
  the app recreates roster_-named tables (loses only demo session/queue history — fine).
- Fix `backup.py:22-25` table list to the real `roster_*` names (currently half-stale/broken).
- Rename the `x-noesis-token` header (app.py params + every `apps/web/*.html` fetch header) in
  LOCKSTEP → `x-roster-token`. Rename FE storage keys `noesis-*`→`roster-*`, SVG `#noesis-logo` id,
  and the asset files `noesis-logo.png`/`noesis-mark.png` (+ all refs).
- App title `Noesis Research`→`Roster`; fallback HTML; docstrings/comments; `deploy/README.md`;
  `.env.example` (`ROSTER_ACTIVE_VERTICAL=tech`, an roster DSN).

### Remove medical-only code
- **Delete** `apps/api/people_cms.py`, `people_load.py`, `people_taxonomies.py` (NPPES/CMS physician
  registry) + their `/admin/people/*` and `/people/*` endpoints.
- **Delete** `/admin/corpus/tag-modality` + the CAM modality machinery (`modality_mode_enabled`,
  `_modality_exclude`, `AVAILABLE_MODALITIES`, `ResearchIn.modality`, config/wiring).
- **Delete** the "Noesis IN"/India country machinery (`in_mode_enabled`, `AVAILABLE_COUNTRIES`,
  country-scope/boost flags, IN profile resolution) — tech uses `sector_profiles`, not countries.
- **Delete** the retraction sweep loop + `/admin/pulse/retraction-scan` + the two
  `noesis_vertical_medical.retractions` imports.
- **Trim** `CorpusIngestIn` to the generic `jobs` list (drop `conditions`/`faers_drugs`/`trials`/
  `papers`/`source_country`/`modality`); the generic passthrough already works for edgar/arxiv.
- **De-medicalize** sessions audience (`real_patient`/`layman_answer`/`clinician` → analyst/investor
  or vertical-supplied); topic-registry `kind="condition"` → tech kind; glossary/UI "medical" copy.

### Add (tech replacements)
- A tech `coverage_plan()` (companies/sectors/technologies covered-vs-remaining) so `/admin/coverage`
  isn't empty. A tech Pulse detector (new 8-K/10-K, new arXiv version, patent grant) later.

### Verify decoupling
`grep -rniI noesis` (excl .venv/.git) returns 0 functional hits; app boots vertical=tech; kernel
guardrails green; redeploy; prod /health + a grounded Q&A still work.
