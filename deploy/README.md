# Deploying Roster

One vertical per deployment. The image serves the API + research console.

## Env vars

| Var | Purpose | Example |
|---|---|---|
| `ROSTER_ACTIVE_VERTICAL` | which installed vertical to activate | `medical` |
| `ROSTER_PROVIDER_MODE` | `replay` (offline/free) · `record` (real + save cassette) · `live` (real) | `live` in prod |
| `PORT` | HTTP port | `8000` (Railway sets this) |
| `ANTHROPIC_API_KEY` | LLM (record/live only) | secret |
| `OPENAI_API_KEY` | embeddings (record/live only) | secret |
| `TAVILY_API_KEY` | web search (record/live only) | secret |
| `ROSTER_LLM_MODEL` | override the Anthropic model | `claude-sonnet-5` |
| `ROSTER_CORPUS_DSN` | Postgres+pgvector DSN for the corpus (when wired) | `postgresql://…` |
| `ROSTER_CASSETTE_ROOT` | where cassettes live (replay/record) | `evals/cassettes` |

## Railway

1. New service from this repo (root `railway.toml` builds `deploy/Dockerfile`).
2. Set `ROSTER_ACTIVE_VERTICAL`, `ROSTER_PROVIDER_MODE=live`, and the API keys.
3. Add a Postgres (pgvector) service; set `ROSTER_CORPUS_DSN`.
4. Healthcheck is `/health`; the console is at `/`.

## Local

```bash
ROSTER_ACTIVE_VERTICAL=medical ROSTER_PROVIDER_MODE=replay \
  PYTHONPATH=packages/kernel:packages/vertical_medical:apps \
  .venv/bin/uvicorn api.app:create_app --factory --port 8000
```

## Notes / not-yet-wired
- The default corpus is the vertical's **fixture** until real ingestion → Postgres
  (with OpenAI embeddings) is wired; `/research` in `live` mode already produces
  real answers over it.
- `replay` needs recorded cassettes under `ROSTER_CASSETTE_ROOT`; `live` needs the
  API keys but no cassettes.

## Medical vertical — data infra (added)

- **Dedicated DB:** a fresh pgvector Postgres (container `roster-db`, port 5434) —
  separate from any other project. `ROSTER_CORPUS_DSN=postgresql://roster:roster@localhost:5434/roster`.
- **Object store (R2):** raw fetched artifacts (assembled trial/label markdown) are
  content-addressed into Cloudflare R2 via `S3ObjectStore`. Env: `R2_BUCKET`,
  `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (see gitignored `.env.medical`).
- **Embeddings:** OpenAI `text-embedding-3-small` (1536-d) — `OPENAI_API_KEY`.
- **Download real data (prod-direct):** ingest happens in prod via the admin endpoint, not local.
  ```bash
  curl -X POST "$BASE/admin/corpus/ingest" -H "X-Admin-Token: $ROSTER_ADMIN_TOKEN" \
    -d '{"jobs":[{"connector":"arxiv","query":"large language model inference","limit":40,"facets":{"sector":"ai"}},
                 {"connector":"openalex","query":"retrieval augmented generation","limit":40,"facets":{"sector":"ai"}}]}'
  ```
  Sources: arXiv (preprints) + OpenAlex (peer-reviewed). Raw → R2, index → pgvector. Watch `GET /corpus/queue`.
