"""Recreatability backups → R2 (the user directive: "ensure we can always recreate").

WHAT: every irreplaceable table dumped as gzipped JSONL to the R2 bucket under
`backups/<UTC-date>/…` — the operational/derived state (users, sessions, ledger, graph,
registry, watches, prefs, feedback, gap-queue history) PLUS the corpus text+facets
(`rs_block` minus embeddings/tsv — both recreatable by re-embedding/re-indexing). With this,
a total Postgres loss restores from R2 alone; raw artifacts in R2 remain the provenance tier.

HOW: streamed in row chunks (rs_block ships as multiple ~50k-row parts so memory stays flat);
replica-safe daily trigger rides the gap-processor idle path (same pattern as the weekly
retraction sweep); manual trigger via POST /admin/backup/run; completion = `backups/<date>/MANIFEST.json` in R2
(the status endpoint has no state store in roster).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json

# (table, order_col or None) — dumped fully; keep in DEPENDENCY-FRIENDLY restore order.
CRITICAL_TABLES = (
    "roster_user", "roster_user_token", "roster_user_pref", "roster_feedback",
    "roster_research_session", "roster_corpus_gap_queue",
    "rs_map",                 # saved Talent Maps (review states + notes) — not re-derivable
    "rs_linkedin_scan",       # LinkedIn snippet resolutions (daily-capped search spend)
    "rs_ingest_checkpoint",   # people windows / job boards / kill switch — where the grind resumes
)
# LARGE tables streamed in flat-memory row parts. Columns are resolved dynamically and vector/tsvector
# columns are EXCLUDED (embeddings + full-text indexes are recreatable by re-embedding/re-indexing):
#   rs_block            — research corpus text + facets
#   rs_entity           — people/company IDENTITIES (the people index backbone)
#   roster_entity_facet — the extracted people/company FACETS (title/role/skill/company/links/…)
#   rs_job              — the jobs index (embedding excluded, recreatable)
# With these, a total Postgres loss restores the people + jobs + corpus from R2 alone.
#   rs_person_artifact  — linked public artifacts per person (papers/repos/posts/talks)
#   rs_artifact_scan    — per-source scan ledger (what has been scanned; drives resumable linking)
#   rs_person_link      — cross-source identity links (one person across GitHub/OpenAlex/…) — not re-derivable
#   rs_ats_probed       — job-board probe ledger (which ATS/company pairs were tried; resumable sweeps)
_STREAM_TABLES = ("rs_block", "rs_entity", "roster_entity_facet", "rs_job",
                  "rs_person_artifact", "rs_artifact_scan", "rs_person_link", "rs_ats_probed")
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


async def _dumpable_cols(conn, table: str) -> list[str]:
    """Columns of `table` safe to dump — EXCLUDING vector/tsvector columns (embeddings + full-text
    indexes, which are large and recreatable by re-embedding/re-indexing). Empty list = table absent."""
    rows = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = $1 AND udt_name NOT IN ('vector', 'tsvector') "
        "ORDER BY ordinal_position", table)
    return [r["column_name"] for r in rows]


async def _dump_streamed(conn, store, base: str, table: str, manifest: dict) -> None:
    """Stream a large table to R2 in flat-memory row parts (memory stays flat regardless of size).
    Columns resolved dynamically with vector/tsv excluded. Records parts + row count in the manifest;
    a missing table or mid-dump failure is recorded and skipped (a partial backup beats none)."""
    try:
        cols = await _dumpable_cols(conn, table)
        if not cols:
            manifest["errors"].append({table: "absent or no dumpable columns"})
            return
        collist = ", ".join(f'"{c}"' for c in cols)
        part, buf, n_in_part, total = 0, [], 0, 0
        async with conn.transaction():
            async for r in conn.cursor(f"SELECT {collist} FROM {table}"):   # noqa: S608 — cols from info_schema
                buf.append(json.dumps({k: _ser(v) for k, v in dict(r).items()}, default=str))
                n_in_part += 1; total += 1
                if n_in_part >= _PART_ROWS:
                    store.put(f"{base}/{table}-part{part:04d}.jsonl.gz",
                              gzip.compress("\n".join(buf).encode()), content_type="application/gzip")
                    part += 1; buf, n_in_part = [], 0
        if buf:
            store.put(f"{base}/{table}-part{part:04d}.jsonl.gz",
                      gzip.compress("\n".join(buf).encode()), content_type="application/gzip")
            part += 1
        manifest["streamed"][table] = {"rows": total, "parts": part}
    except Exception as e:   # noqa: BLE001
        manifest["errors"].append({table: str(e)[:200]})


async def run_backup(dsn: str, store) -> dict:
    """Dump all critical tables + the large streamed tables (corpus, people identities + facets, jobs)
    to R2 via `store` (R2Named: put(key, data, content_type)). Returns a manifest summary. Any table
    that fails is recorded and skipped — a partial backup with a manifest beats none."""
    import asyncpg
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    base = f"backups/{day}"
    manifest: dict = {"date": day, "tables": {}, "streamed": {}, "corpus_parts": 0, "errors": []}
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
        # LARGE tables streamed in parts (people index + jobs + corpus; vector/tsv excluded).
        for t in _STREAM_TABLES:
            await _dump_streamed(conn, store, base, t, manifest)
        manifest["corpus_parts"] = manifest["streamed"].get("rs_block", {}).get("parts", 0)  # back-compat
    finally:
        await conn.close()
    manifest["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    store.put(f"{base}/MANIFEST.json", json.dumps(manifest, indent=1).encode(),
              content_type="application/json")
    return manifest
