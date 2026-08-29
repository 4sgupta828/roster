"""Corpus gap-fill queue — durable jobs that add evidence to heal a gap.

Each row is one connector ingest job ({connector, query, limit}) proposed by the gap planner and
approved by a user. A background processor claims pending jobs atomically (FOR UPDATE SKIP LOCKED,
so multiple API replicas never double-run one) and ingests them into the SAME corpus the research
path searches — so a healed gap is immediately answerable.

VERTICAL-ISOLATED and best-effort like the session store: a queue failure must never break research.
Backed by the corpus Postgres (ROSTER_CORPUS_DSN).
"""
from __future__ import annotations

import uuid
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS roster_corpus_gap_queue (
    id            TEXT PRIMARY KEY,
    vertical      TEXT NOT NULL,
    tenant_id     TEXT NOT NULL DEFAULT 'demo',
    connector     TEXT NOT NULL,
    query         TEXT NOT NULL,
    lim           INTEGER NOT NULL DEFAULT 200,
    kind          TEXT DEFAULT '',
    rationale     TEXT DEFAULT '',
    quality       TEXT DEFAULT '',
    question      TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | running | done | failed
    blocks_added  INTEGER NOT NULL DEFAULT 0,
    error         TEXT DEFAULT '',
    source_country TEXT DEFAULT '',                     -- stamp every ingested block (country sources)
    modality      TEXT DEFAULT '',                      -- stamp every ingested block (e.g. 'alternative' for CAM)
    facets        JSONB NOT NULL DEFAULT '{}',          -- generic per-job facet overrides stamped on every block (e.g. {"sector":"ai"})
    params        JSONB NOT NULL DEFAULT '{}',          -- generic per-job connector params merged into the fetch window (e.g. {"forms":["D"]})
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE roster_corpus_gap_queue ADD COLUMN IF NOT EXISTS source_country TEXT DEFAULT '';
ALTER TABLE roster_corpus_gap_queue ADD COLUMN IF NOT EXISTS modality TEXT DEFAULT '';
ALTER TABLE roster_corpus_gap_queue ADD COLUMN IF NOT EXISTS facets JSONB NOT NULL DEFAULT '{}';
ALTER TABLE roster_corpus_gap_queue ADD COLUMN IF NOT EXISTS params JSONB NOT NULL DEFAULT '{}';
ALTER TABLE roster_corpus_gap_queue ADD COLUMN IF NOT EXISTS priority INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_gapq_claim ON roster_corpus_gap_queue (vertical, status, priority DESC, created_at);
"""

# A 'running' job older than this (crash / redeploy mid-ingest) is reclaimed to 'pending'.
_STALE_MINUTES = 20


class GapQueue:
    def __init__(self, dsn: str, *, vertical: str):
        self._dsn = dsn
        self._vertical = vertical
        self._pool = None
        self._ready = False

    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=3)
        return self._pool

    async def _ensure(self) -> None:
        if self._ready:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(_DDL)
        self._ready = True

    async def enqueue(self, *, tenant_id: str, question: str, jobs: list[dict]) -> list[str]:
        """Insert approved jobs; returns their ids. Caller validates connector/limit first."""
        await self._ensure()
        pool = await self._get_pool()
        ids: list[str] = []
        async with pool.acquire() as conn:
            for j in jobs:
                jid = uuid.uuid4().hex
                import json as _json
                await conn.execute(
                    """INSERT INTO roster_corpus_gap_queue
                       (id, vertical, tenant_id, connector, query, lim, kind, rationale, quality, question, source_country, modality, facets, params, priority)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14::jsonb,$15)""",
                    jid, self._vertical, tenant_id, j["connector"], j["query"],
                    int(j.get("limit", 200)), j.get("kind", ""), j.get("rationale", ""),
                    j.get("quality", ""), question, j.get("source_country", ""), j.get("modality", ""),
                    _json.dumps(j.get("facets") or {}), _json.dumps(j.get("params") or {}),
                    int(j.get("priority", 0)))
                ids.append(jid)
        return ids

    async def claim_one(self) -> dict[str, Any] | None:
        """Atomically move the oldest pending job to 'running' and return it (or None)."""
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE roster_corpus_gap_queue SET status='pending', updated_at=now() "
                    "WHERE vertical=$1 AND status='running' AND updated_at < now() - ($2 || ' minutes')::interval",
                    self._vertical, str(_STALE_MINUTES))
                row = await conn.fetchrow(
                    """UPDATE roster_corpus_gap_queue q SET status='running', updated_at=now()
                       FROM (SELECT id FROM roster_corpus_gap_queue
                             WHERE vertical=$1 AND status='pending'
                             ORDER BY priority DESC, created_at LIMIT 1 FOR UPDATE SKIP LOCKED) c
                       WHERE q.id=c.id
                       RETURNING q.id, q.connector, q.query, q.lim, q.tenant_id, q.source_country, q.modality, q.facets, q.params""",
                    self._vertical)
        if row is None:
            return None
        import json as _json

        def _loadj(v):
            if isinstance(v, str):
                try:
                    return _json.loads(v or "{}")
                except Exception:   # noqa: BLE001
                    return {}
            return v or {}
        return {"id": row["id"], "connector": row["connector"], "query": row["query"],
                "limit": row["lim"], "tenant_id": row["tenant_id"],
                "source_country": row["source_country"] or "",
                "modality": row["modality"] or "",
                "facets": _loadj(row["facets"]),
                "params": _loadj(row["params"])}

    async def complete(self, job_id: str, blocks_added: int) -> None:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE roster_corpus_gap_queue SET status='done', blocks_added=$2, "
                "error='', updated_at=now() WHERE id=$1", job_id, int(blocks_added))

    async def fail(self, job_id: str, error: str) -> None:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE roster_corpus_gap_queue SET status='failed', error=$2, updated_at=now() "
                "WHERE id=$1", job_id, (error or "")[:800])

    async def list(self, *, tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        await self._ensure()
        pool = await self._get_pool()
        where = "vertical=$1"
        params: list[Any] = [self._vertical]
        if tenant_id:
            params.append(tenant_id); where += f" AND tenant_id=${len(params)}"
        params.append(limit)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""SELECT id, connector, query, lim, kind, rationale, quality, question,
                          status, blocks_added, error, created_at, updated_at
                    FROM roster_corpus_gap_queue WHERE {where}
                    ORDER BY created_at DESC LIMIT ${len(params)}""", *params)
        return [{
            "id": r["id"], "connector": r["connector"], "query": r["query"], "limit": r["lim"],
            "kind": r["kind"], "rationale": r["rationale"], "quality": r["quality"],
            "question": r["question"], "status": r["status"], "blocks_added": r["blocks_added"],
            "error": r["error"], "created_at": r["created_at"].isoformat(),
            "updated_at": r["updated_at"].isoformat(),
        } for r in rows]

    async def summary(self) -> dict[str, Any]:
        await self._ensure()
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, count(*) n, coalesce(sum(blocks_added),0) blocks "
                "FROM roster_corpus_gap_queue WHERE vertical=$1 GROUP BY status", self._vertical)
        by = {r["status"]: {"n": r["n"], "blocks": r["blocks"]} for r in rows}
        return {"by_status": by,
                "pending": by.get("pending", {}).get("n", 0),
                "running": by.get("running", {}).get("n", 0),
                "done": by.get("done", {}).get("n", 0),
                "failed": by.get("failed", {}).get("n", 0),
                "blocks_added": sum(v.get("blocks", 0) for v in by.values())}
