# Roster

**An AI platform for searching professionals and companies — and how they are connected.**

Roster reconstructs a "shadow LinkedIn" from *publicly available information* (company registries,
regulatory filings, code hosts, scholarly graphs, the open web) and answers grounded, evidence-cited
questions about people, companies, and the relationships between them:

- *Who is this person / company?* — a source-labeled dossier built from public signals.
- *Who is X connected to, and how?* — colleagues, co-founders, boards, co-authors, investors.
- *How is X connected to Y?* — the path(s) between two entities, each hop backed by evidence.

Every answer is grounded: claims are tied to real source spans through a verbatim span-check
provenance gate. Semantics (who an entity is, what a relationship means) are owned by the LLM, never
by keyword heuristics.

## Architecture

Roster is a domain-agnostic **kernel** plus a single active **vertical**, served by a vertical-neutral
app shell. It was forked from the `eigen` research platform (lineage: `noesis` → `eigen` → `roster`),
reusing the proven grounded-research engine wholesale.

```
packages/kernel/roster_kernel/     domain-free engine: ingest → corpus (Postgres: tsvector +
                                   pgvector + jsonb facets) → hybrid retrieval → ReAct research
                                   loop → verbatim span-check provenance gate → synthesis
packages/vertical_roster/          the roster domain: public-data connectors, persona, answer
  roster_vertical/                 format, eval gold — one VerticalManifest (entry point: `roster`)
apps/{api,web,worker}              FastAPI app + web shell + ingest worker
deploy/                            Dockerfile + start script (Railway)
```

The kernel **names no domain noun** — an architectural invariant enforced by
`packages/kernel/roster_kernel/conformance/` and `tools/check_kernel_*`. All vocabulary lives in the
vertical, so the same engine can host any domain.

> **Status.** The kernel is reused as-is. The **vertical still carries eigen's inherited tech
> vocabulary and connector set** — the starting point, not the finished roster domain. Many inherited
> connectors are already the raw material for a connection graph (`companies_house` =
> directors↔companies, `github` = people↔repos↔orgs, `wikidata` = employer/founder/board relations,
> `openalex`/`crossref` = co-authorship, `yc` = founders↔startups, `edgar` = insiders↔issuers).
> Reshaping the vertical toward people/company **edges** is the primary ongoing work — see the Roadmap
> in `CLAUDE.md`.

## Quickstart

```bash
# Python 3.13 + uv
uv venv --python 3.13 .venv
VIRTUAL_ENV=.venv uv pip install "./packages/kernel[serve,postgres]" "./packages/vertical_roster"

# verify the engine loads the roster vertical
ROSTER_ACTIVE_VERTICAL=roster .venv/bin/python -c \
  "from roster_kernel.runtime.build import load_active_vertical as L; print('vertical:', L().name)"

# run the offline test suite (replay mode — no network, no API keys)
ROSTER_ACTIVE_VERTICAL=roster ROSTER_PROVIDER_MODE=replay PYTHONPATH=apps \
  .venv/bin/python -m pytest packages -q
```

Config lives in `.env` (see `.env.example`): `ROSTER_ACTIVE_VERTICAL`, `ROSTER_PROVIDER_MODE`
(`replay` = free/offline, `record`, `live`), provider keys, and `ROSTER_CORPUS_DSN`.

## Kernel guardrails

```bash
.venv/bin/python tools/check_kernel_imports.py  # GREEN: kernel imports no vertical
bash tools/check_kernel_invariant.sh            # currently RED — see note
```

- The **pytest conformance suite** (`packages/kernel/roster_kernel/conformance/`, run as part of
  `pytest packages`) is green: the shipped kernel runtime imports no vertical and names no
  regulatory domain noun.
- `tools/check_kernel_invariant.sh` uses eigen's **tech** banlist (`moat`, `valuation`, `startup`,
  `arxiv`, …) and currently reports genuine inherited leaks — eigen let tech vocabulary bleed into a
  few kernel prompts (`research/react.py`, `research/web_coverage.py`). This debt predates the fork;
  de-tech'ing those prompts is part of reshaping toward the roster domain (see Roadmap in `CLAUDE.md`).

See `CLAUDE.md` for the full working guide, invariants, and roadmap.
