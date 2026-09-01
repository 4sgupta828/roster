# AGENTS.md — Roster

Guidance for Codex when working in this repo: **Roster**, an AI platform for searching
**professionals and companies and how they are connected** — a "shadow LinkedIn" reconstructed from
publicly available information (company registries, filings, code hosts, scholarly graphs, the open
web). The user asks who a person/company is, who they're connected to, and how — and gets grounded,
evidence-cited answers over a corpus of public professional/company signals. It is a domain-agnostic
**kernel** (`packages/kernel/roster_kernel/`) plus a plug-in **roster vertical**
(`packages/vertical_roster/roster_vertical/`), served by a vertical-neutral app shell (`apps/`).

**Provenance & current state (READ FIRST).** This repo was forked from the `eigen` deep-tech research
platform (itself forked from `noesis`), reusing the proven grounded-research kernel wholesale. The
**kernel is domain-agnostic and reused as-is.** The **vertical still carries eigen's inherited tech
persona / answer-format / entity vocabulary and connector set** — it is the starting point, not the
finished roster domain. Many inherited connectors are already the raw material for a connection graph
(`companies_house` = directors↔companies, `github` = people↔repos↔orgs, `wikidata` =
employer/founder/board relations, `openalex`/`crossref` = co-authorship, `yc` = founders↔startups,
`edgar` = insiders↔issuers). Reshaping the vertical toward people/company **edges** (the shadow-LinkedIn
core) is the primary ongoing work — see "Roadmap" below. When you touch the vertical, move it toward
the connection-graph mission; do not assume the current tech framing is intended.

## What this is / layout

- `packages/kernel/roster_kernel/` — DOMAIN-FREE platform: ingest → corpus (`Document→ParsedDoc→Block`
  → flat `rs_block` in Postgres: tsv + pgvector + jsonb facets) → hybrid retrieval → ReAct research
  loop → **verbatim span-check provenance gate** → synthesis. Names no domain noun (enforced by
  `packages/kernel/roster_kernel/conformance/` + `tools/check_kernel_*`).
- `packages/vertical_roster/roster_vertical/` — the roster domain vocabulary: public-data connectors,
  persona, authority pyramid, answer format, extraction lenses, UI, eval gold — bundled into one
  `VerticalManifest` via `manifest.py:build_manifest()` (registered as the `roster` entry point).
  **Inherited-tech today; being reshaped toward professionals/companies + connections.**
- `apps/{api,web}` — the FastAPI app + web shell. One vertical active per deployment
  (`ROSTER_ACTIVE_VERTICAL=roster`), provider mode `ROSTER_PROVIDER_MODE` (replay/record/live).

## Roadmap (the shadow-LinkedIn core, not yet built)

1. **Entity model** — first-class `person` / `company` / `organization` / `institution` entities with
   stable IDs and cross-source resolution (the same person across GitHub, Wikidata, filings).
2. **Edge extraction** — the connective tissue: `worked_at`, `founded`, `board_member`, `co_authored`,
   `invested_in`, `advised`, `colleague_of` — extracted (LLM-owned semantics, Rule 18) from the public
   corpus with grounded evidence per edge.
3. **Connection queries** — "how is X connected to Y", "who connects X and Y", "who worked with X" —
   answered over the edge graph with cited provenance.
4. **People/company profiles** — a grounded, source-labeled dossier per entity (the inherited
   `person_reader` / `company_reader` are the seed).

## Commands

- Dev venv: `.venv/bin/python` (Python 3.13; system python is older and can't import the project).
- Kernel + vertical tests: `.venv/bin/python -m pytest packages -q`
- Kernel guardrails (must stay green): `bash tools/check_kernel_invariant.sh` and
  `.venv/bin/python tools/check_kernel_imports.py`
- Conformance: `run_conformance(build_manifest(), phase="P4")`
- App locally: `ROSTER_ACTIVE_VERTICAL=roster PYTHONPATH=apps .venv/bin/python -c "from api.app import create_app; create_app()"`

## Prod / deploy (Railway) — LIVE

- Repo remote: **https://github.com/4sgupta828/roster**. Railway project **`roster`** is provisioned
  and serving: prod URL **https://roster-api-production-3405.up.railway.app** (note the `-3405`),
  services `roster-api` (uvicorn) and `roster-worker` (`ROSTER_ROLE=worker` → `python -m worker.main`,
  runs the corpus gap-processor and, behind `ROSTER_BULK_INGEST`, the people/jobs bulk-ingest loop in
  `apps/worker/bulk_ingest.py`; kill switch = `rs_ingest_checkpoint` row `('control','stop')`).
- Deploy: image = `deploy/Dockerfile`; `railway up -d -s <service>` (uploads cwd + builds — NOT wired
  to GitHub auto-deploy, so a `git push` does not redeploy); poll `railway service status` until
  `SUCCESS`. `railway run` from a laptop injects the INTERNAL Postgres hostname (unreachable locally);
  for DB work use `railway ssh --service roster-api`.
- Ingest (prod-direct): `POST /admin/corpus/ingest` with `{"jobs":[{"connector","query","limit"}]}`
  and header `X-Admin-Token: $ROSTER_ADMIN_TOKEN`; watch `GET /corpus/queue`. People/jobs index
  progress: public `GET /admin/people-coverage` + `rs_ingest_checkpoint` (people windows / job boards).

## No coupling with noesis — code or data (STANDING DIRECTIVE)

Roster is an INDEPENDENT product. It shares the kernel *design lineage* with noesis but must not
depend on noesis at runtime, in data, or in code:
- **No `noesis` anywhere.** No `noesis`/`noesis_kernel`/`noesis_vertical_*` imports, package names,
  strings, table names, env vars, branding, or comments. The kernel package is `roster_kernel`; env
  vars are `ROSTER_*`; the entry-point group is `roster.verticals`.
- **No medical-vertical vocabulary or code.** No `condition`/`drug`/`trial`/`clinical`/`patient`/CAM/
  retraction/`europepmc` code paths in the app shell or kernel. If a shared endpoint carries medical
  fields, generalize it or remove it — never leave dormant medical logic in roster.
- **Separate data.** Roster has its OWN R2 bucket and its OWN Postgres. Never read noesis's bucket,
  DSN, or `.env`. Secrets live in roster's own Railway service.

## Sentiment is a SIGNAL, not a fact (STANDING DIRECTIVE — the tech grounding discipline)

Market sentiment (news tone, Hacker News, social) is the LOWEST authority tier and can NEVER be
`is_controlling` (`authority.py`). It must be presented in a clearly-labeled "market signal" register
("coverage suggests…", "sentiment is…"), never as an established fact and never as investment advice.
Likewise separate STATED INTENT from REALIZED FACT: a patent APPLICATION's claims, a press release,
or a roadmap are intent; a GRANTED patent or an audited SEC number is fact. This is the tech analogue
of the evidence-typed discipline below — a fabricated funding round, a sentiment-as-fact claim, or a
wrong-company attribution is the failure class the gates must make impossible.

## API-Credit Discipline (STANDING DIRECTIVE — spend very carefully)

Anthropic/OpenAI credits are a scarce, shared resource: prod answers, ingest embeddings, and evals
draw on the SAME account. Rules, always:
1. **Free checks first.** Structural tests, retrieval-only probes, TestClient runs, and DB checks cost
   nothing — exhaust them before any LLM/embedding-spending run.
2. **Every spending run is projected + gated.** Print a projected budget; a large ingest or eval sweep
   needs an explicit user go with the number in front of them.
3. **Targeted before broad.** Stage ingest/campaigns in tranches with a review between; one flagship
   prod verification beats a 50-question sweep.
4. **Never launch a spending run while another is in flight** against shared state; never re-run a
   failed batch before diagnosing WHY it failed.
5. **Validate the pipeline on 1–2 items before the batch.** A bug found after 20 items costs 20 items.

## Kernel/Vertical Split (STANDING DIRECTIVE — nothing hardwired to the tech vertical)

Every feature is built kernel-first: `packages/kernel/roster_kernel/` owns MECHANICS (retrieval,
grounding, graph, currency, evals plumbing) with ZERO domain vocabulary — no "startup", "patent",
"valuation", "ticker", "competitor", "sentiment" in kernel code or kernel prompts. The vertical
supplies VOCABULARY and JUDGMENT via the manifest: prompts/directives, connectors, authority policy,
sector profiles, curated data. Litmus before landing anything: "could a LEGAL or a BIOTECH vertical
reuse this by supplying its own manifest, kernel untouched?" If not, move the domain part into the
vertical. The grep + AST guardrails (`tools/check_kernel_*`) enforce this in CI — keep them green.

## Corpus-First Sourcing (STANDING DIRECTIVE)

DOWNLOAD everything downloadable (public + API-accessible: SEC filings, patents, papers, code) into the
corpus — internal semantic + keyword search with our own ranking/tiering beats answer-time web
retrieval. Use the WEB LEG only for content that is (a) frequently changing (news-grade, sentiment) or
(b) not legally/technically downloadable. When a source is available both ways, corpus wins: durable,
tier-classified, reproducible. **Ingest happens in PROD** (prod-direct), never local-then-push.

## Evidence Is Typed, Not Text (STANDING DIRECTIVE)

Provenance is never correctness. "The quote exists" (span-check) and "the quote supports the sentence"
(entailment) are string-level facts. A claim additionally requires CONGRUENCE: the evidence's SUBJECT
is the claim's subject (this company's revenue, not a peer's), and the evidence's KIND matches what the
claim asserts (a benchmark backs a performance claim; a filing backs a financial claim; sentiment backs
neither). Never strip identity (source, subject/company, evidence kind, period) from evidence shown to
any model or judge. A claim that cannot bind congruent evidence does not exist — fail-safe is
abstain/gap, never "close enough." Every gate must have a held-out eval case DESIGNED to pass the gate
while being wrong (wrong-company attribution, intent-as-fact, sentiment-as-fact, stale-filing).

## Never Normalize a Failing Test / Convene a Panel for Big Calls

- A persistently failing test is a CLAIM about the system; "pre-existing" describes WHEN it broke, not
  whether it matters. Find what it asserts before dismissing it, or file it as a tracked bug.
- For significant, complex, or irreversible decisions/investigations, get an independent critique BEFORE
  committing: run a panel of two external SOTA CLIs (Codex `codex exec … < /dev/null`, Gemini
  `gemini -m gemini-3-pro-preview … < /dev/null`) PLUS a code-grounded subagent that verifies file:line
  claims. Relay each honestly + your synthesized call.

## UI must be mobile-friendly — always

Every user-facing change to `apps/web/**` MUST work and look good on a phone: responsive units,
`overflow-x:auto` for wide content, viewport meta, a `@media (max-width:560px)` pass, touch targets
≥~40px. Verify visually at ≤400px before declaring done — reading the CSS is not enough.
