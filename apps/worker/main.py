"""Standalone INGEST WORKER — runs ONLY the corpus-ingest drain loop, with docling available for
full-text PDF parsing. This lets the API image stay lean (no torch): the API serves; the worker
ingests. Same corpus DSN + connectors as the in-API processor; job claims are atomic so the two
never double-process (in prod, set ROSTER_INGEST_IN_API=false on the API so ingestion runs ONLY here).

A tiny stdlib HTTP health server answers Railway's healthcheck on $PORT while the drain loop blocks.
"""
from __future__ import annotations

import os
import threading


def _serve_health() -> None:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):                     # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):            # silence request logging
            return

    port = int(os.environ.get("PORT", "8000"))
    HTTPServer(("0.0.0.0", port), _H).serve_forever()


def main() -> None:
    dsn = os.environ.get("ROSTER_CORPUS_DSN")
    if not dsn:
        raise SystemExit("ROSTER_CORPUS_DSN is required for the ingest worker")
    # health server in a daemon thread so Railway sees the service as up while we ingest
    threading.Thread(target=_serve_health, daemon=True, name="health").start()
    # BULK-INGEST worker (people/jobs corpus → 2x): durable, resumable loop off the API process.
    if os.environ.get("ROSTER_BULK_INGEST", "").lower() in ("1", "true", "yes"):
        from worker.bulk_ingest import run_bulk_ingest_loop
        print("[worker] roster BULK-INGEST WORKER (people/jobs)", flush=True)
        run_bulk_ingest_loop()                 # blocks forever on the bulk-ingest loop
        return
    # import here (not at module top) so a missing FastAPI dep can't break health startup
    from api.app import _run_gap_processor
    from roster_kernel.runtime.build import load_active_vertical
    vertical = load_active_vertical().name
    print(f"[worker] roster INGEST WORKER — vertical={vertical} (docling PDF parsing enabled)", flush=True)
    _run_gap_processor(dsn, vertical)          # blocks forever on the drain loop


if __name__ == "__main__":
    main()
