#!/usr/bin/env bash
# Roster entrypoint — ONE image, TWO roles (shared railway.toml startCommand runs this for both
# services). ROSTER_ROLE=worker → the corpus-ingest drain loop (docling-enabled) + a health server.
# else (default) → the API (uvicorn). Provider mode via ROSTER_PROVIDER_MODE (replay=offline/free,
# live=real Anthropic/OpenAI/Tavily). pgvector schema is created on demand by the corpus source.
set -euo pipefail
PORT="${PORT:-8000}"
if [ "${ROSTER_ROLE:-api}" = "worker" ]; then
  echo "[start] roster INGEST WORKER — vertical=${ROSTER_ACTIVE_VERTICAL:-?} mode=${ROSTER_PROVIDER_MODE:-replay} (docling PDF parsing)"
  exec python -m worker.main
fi
echo "[start] roster api — vertical=${ROSTER_ACTIVE_VERTICAL:-?} mode=${ROSTER_PROVIDER_MODE:-replay} port=$PORT"
exec uvicorn api.app:create_app --factory --host 0.0.0.0 --port "$PORT"
