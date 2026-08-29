"""Recreatability backups → R2 (the user directive: "ensure we can always recreate").

WHAT: every irreplaceable table dumped as gzipped JSONL to the R2 bucket under
`backups/<UTC-date>/…` — the operational/derived state (users, sessions, ledger, graph,
registry, watches, prefs, feedback, gap-queue history) PLUS the corpus text+facets
(`rs_block` minus embeddings/tsv — both recreatable by re-embedding/re-indexing). With this,
a total Postgres loss restores from R2 alone; raw artifacts in R2 remain the provenance tier.

HOW: streamed in row chunks (rs_block ships as multiple ~50k-row parts so memory stays flat);
replica-safe daily trigger rides the gap-processor idle path (same pattern as the weekly
retraction sweep); manual trigger via POST /admin/backup/run. Restore:
`scripts/restore_from_backup.py`.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json

# (table, order_col or None) — dumped fully; keep in DEPENDENCY-FRIENDLY restore order.
CRITICAL_TABLES = (
    "roster_user", "roster_user_token", "roster_user_pref", "roster_feedback",
    "roster_change_event", "roster_topic", "roster_topic_edge", "roster_edge_evidence",
    "roster_watch", "roster_watch_seen", "roster_pulse_state",
    "roster_research_session", "roster_corpus_gap_queue",
)
_BLOCK_COLS = ("tenant_id", "workspace_id", "document_id", "block_id", "text", "facets",
               "document_title", "content_type", "source_key", "created_at")
_PART_ROWS = 50_000


def _ser(v):
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return v


class R2Named:
    """Named-key writer for the R2 bucket (the kernel S3ObjectStore is content-addressed —
    backups need stable, dated keys). Same env vars, lazy boto3."""

    def __init__(self):
        import os
        self._cfg = dict(endpoint_url=os.environ["R2_ENDPOINT"], region_name="auto",
                         aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                         aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"])
        self._bucket = os.environ["R2_BUCKET"]
        self._client = None

    def _c(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("s3", **self._cfg)
        return self._client

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream"):
        self._c().put_object(Bucket=self._bucket, Key=key, Body=data,
                             ContentType=content_type)

    def list(self, prefix: str) -> list[str]:
        out = []
        for page in self._c().get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket, Prefix=prefix):
            out += [o["Key"] for o in page.get("Contents", [])]
        return out

    def get(self, key: str) -> bytes:
        return self._c().get_object(Bucket=self._bucket, Key=key)["Body"].read()


async def run_backup(dsn: str, store) -> dict:
    """Dump all critical tables + corpus text to R2 via `store` (R2Named: put(key, data,
    content_type)). Returns a manifest summary. Any table that fails is
    recorded and skipped — a partial backup with a manifest beats none."""
    import asyncpg
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    base = f"backups/{day}"
    manifest: dict = {"date": day, "tables": {}, "corpus_parts": 0, "errors": []}
    conn = await asyncpg.connect(dsn)
    try:
        for t in CRITICAL_TABLES:
            try:
                rows = await conn.fetch(f"SELECT * FROM {t}")   # noqa: S608 — fixed allowlist
                payload = "\n".join(json.dumps({k: _ser(v) for k, v in dict(r).items()},
                                               default=str) for r in rows)
                store.put(f"{base}/{t}.jsonl.gz", gzip.compress(payload.encode()),
                          content_type="application/gzip")
                manifest["tables"][t] = len(rows)
            except Exception as e:   # noqa: BLE001
                manifest["errors"].append({t: str(e)[:200]})
        # corpus text+facets in flat-memory parts (embeddings/tsv EXCLUDED — recreatable)
        part, buf, n_in_part = 0, [], 0
        cols = ", ".join(_BLOCK_COLS)
        async with conn.transaction():
            async for r in conn.cursor(f"SELECT {cols} FROM rs_block"):   # noqa: S608
                buf.append(json.dumps({k: _ser(v) for k, v in dict(r).items()}, default=str))
                n_in_part += 1
                if n_in_part >= _PART_ROWS:
                    store.put(f"{base}/rs_block-part{part:04d}.jsonl.gz",
                              gzip.compress("\n".join(buf).encode()),
                              content_type="application/gzip")
                    part += 1
                    buf, n_in_part = [], 0
        if buf:
            store.put(f"{base}/rs_block-part{part:04d}.jsonl.gz",
                      gzip.compress("\n".join(buf).encode()), content_type="application/gzip")
            part += 1
        manifest["corpus_parts"] = part
    finally:
        await conn.close()
    manifest["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    store.put(f"{base}/MANIFEST.json", json.dumps(manifest, indent=1).encode(),
              content_type="application/json")
    return manifest
