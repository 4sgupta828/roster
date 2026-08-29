"""PostgresRetrievalSource — the decided production backend (pgvector + tsvector).

Division of labor (panel decision): the DB does the expensive, scalable parts —
tenant/workspace/facet HARD FILTERS (JSONB `facets @>`/`->>`) and CANDIDATE
GENERATION (pgvector HNSW ANN + tsvector lexical prefilter). The shared,
already-tested `rank_candidates` (BM25 + dense + RRF + signal) ranks the pool, so
this source ranks identically to the in-memory reference. asyncpg is an optional
dep (lazy import); nothing here names a domain noun.

Provenance loader note: verification always runs over blocks just retrieved this
request, so `make_block_loader` reads a per-search cache keyed by
(tenant, document_id, block_id) — sync, no re-query, and cross-tenant-safe
(only the querying tenant's blocks are ever cached).
"""
from __future__ import annotations

import json

from roster_kernel.contract.dto import (
    BlockHit,
    Capability,
    FacetFilter,
    Locator,
    RetrievalRequest,
)
from roster_kernel.retrieval.rank import Candidate, rank_candidates
from roster_kernel.retrieval.scoring import tokens as _tokens

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS {table} (
    tenant_id       text        NOT NULL,
    workspace_id    text,
    document_id     text        NOT NULL,
    block_id        text        NOT NULL,
    text            text        NOT NULL,
    tsv             tsvector    GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding       vector({dim}),
    facets          jsonb       NOT NULL DEFAULT '{{}}'::jsonb,
    document_title  text        NOT NULL DEFAULT '',
    content_type    text        NOT NULL DEFAULT '',
    source_key      text        NOT NULL DEFAULT '',
    PRIMARY KEY (tenant_id, document_id, block_id)
);
CREATE INDEX IF NOT EXISTS {table}_facets_gin ON {table} USING gin (facets);
CREATE INDEX IF NOT EXISTS {table}_tsv_gin    ON {table} USING gin (tsv);
CREATE INDEX IF NOT EXISTS {table}_emb_hnsw   ON {table} USING hnsw (embedding vector_cosine_ops);
-- TIME AXIS (Evidence Pulse): when each block first landed — the primitive that makes corpus
-- deltas queryable over time ("what arrived on topic T in the last 30 days"). Additive; rows
-- ingested before the column exists stay NULL (= unknown, honestly excluded from windows).
-- NOTE: added WITHOUT a default, then default set separately — ADD COLUMN ... DEFAULT now()
-- BACKFILLS every existing row with the migration instant (Postgres fast-path), which stamped
-- the whole legacy corpus as "new today" (the saturated first coverage board). Two statements
-- keep pre-existing rows NULL while new inserts get now().
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT now();
CREATE INDEX IF NOT EXISTS {table}_created ON {table} (created_at DESC);
"""


class PostgresRetrievalSource:
    def __init__(self, dsn: str, *, key: str = "postgres", dim: int = 1536,
                 table: str = "rs_block", covers: FacetFilter | None = None,
                 currency_demote: bool = False):
        self.key = key
        self._dsn = dsn
        self._dim = dim
        self._table = table
        self._covers = covers or {}
        self._pool = None
        self._cache: dict[tuple[str, str, str], str] = {}
        # Evidence Pulse C1 (flag-fed by the app): exclude retracted / demote superseded at retrieval
        self._currency_demote = currency_demote

    # --- lifecycle ---
    async def _get_pool(self):
        if self._pool is None:
            import asyncpg
            from pgvector.asyncpg import register_vector

            # Bootstrap the pgvector extension BEFORE the pool's per-connection init registers the
            # vector type. On a FRESH database the `vector` type does not exist yet, so register_vector
            # would fail ("unknown type: public.vector") and the whole pool creation would throw. One
            # short-lived connection creates it idempotently (needs CREATE privilege — the DB owner has it).
            _boot = await asyncpg.connect(self._dsn)
            try:
                await _boot.execute("CREATE EXTENSION IF NOT EXISTS vector")
            finally:
                await _boot.close()

            async def _init(conn):
                await register_vector(conn)

            self._pool = await asyncpg.create_pool(self._dsn, init=_init, min_size=1, max_size=8)
        return self._pool

    async def ensure_schema(self) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(DDL.format(table=self._table, dim=self._dim))

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def upsert_block(self, *, tenant_id: str, document_id: str, block_id: str,
                           text: str, embedding: list[float] | None = None,
                           facets: dict | None = None, workspace_id: str | None = None,
                           document_title: str = "", content_type: str = "",
                           source_key: str = "") -> None:
        import json
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"""INSERT INTO {self._table}
                    (tenant_id, workspace_id, document_id, block_id, text, embedding,
                     facets, document_title, content_type, source_key)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10)
                    ON CONFLICT (tenant_id, document_id, block_id) DO UPDATE
                    SET text=EXCLUDED.text, embedding=EXCLUDED.embedding,
                        facets=EXCLUDED.facets, document_title=EXCLUDED.document_title,
                        content_type=EXCLUDED.content_type, source_key=EXCLUDED.source_key""",
                tenant_id, workspace_id, document_id, block_id, text, embedding,
                json.dumps(facets or {}), document_title, content_type, source_key,
            )

    async def upsert_blocks(self, rows: list[dict]) -> None:
        """Batch upsert many blocks in one round-trip (executemany). Each row dict:
        tenant_id, document_id, block_id, text, embedding, facets, workspace_id,
        document_title, content_type, source_key."""
        if not rows:
            return
        import json
        pool = await self._get_pool()
        args = [(
            r["tenant_id"], r.get("workspace_id"), r["document_id"], r["block_id"],
            r["text"], r.get("embedding"), json.dumps(r.get("facets") or {}),
            r.get("document_title", ""), r.get("content_type", ""), r.get("source_key", ""),
        ) for r in rows]
        async with pool.acquire() as conn:
            await conn.executemany(
                f"""INSERT INTO {self._table}
                    (tenant_id, workspace_id, document_id, block_id, text, embedding,
                     facets, document_title, content_type, source_key)
                    VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10)
                    ON CONFLICT (tenant_id, document_id, block_id) DO UPDATE
                    SET text=EXCLUDED.text, embedding=EXCLUDED.embedding,
                        facets=EXCLUDED.facets, document_title=EXCLUDED.document_title,
                        content_type=EXCLUDED.content_type, source_key=EXCLUDED.source_key""",
                args,
            )

    async def delete_stale_blocks(self, *, tenant_id: str, document_id: str,
                                  keep_block_ids: list[str]) -> int:
        """CLEAN-REPLACE (Evidence Pulse A1): after re-ingesting a document, remove rows whose
        block_id is not in this ingest's key set. Block ids are content-addressed, so an edited
        document INSERTS its changed blocks while the old text's rows would otherwise linger
        forever — the mixed-edition corpus bug (one document silently serving both editions).
        Returns rows deleted."""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            res = await conn.execute(
                f"""DELETE FROM {self._table}
                    WHERE tenant_id=$1 AND document_id=$2 AND NOT (block_id = ANY($3::text[]))""",
                tenant_id, document_id, list(keep_block_ids))
        try:
            return int((res or "DELETE 0").split()[-1])
        except ValueError:
            return 0

    async def tag_modality_by_journal(self, *, patterns: list[str], modality: str,
                                      apply: bool = False) -> dict:
        """Retro-tag EXISTING blocks by SOURCE (structural, not content — Rule 18): stamp
        facets.modality=<modality> on blocks whose `journal` facet name matches a CAM-journal LIKE
        pattern AND that have no modality yet. `apply=False` is a DRY RUN: it returns the distinct
        matching journals + per-journal block counts so the exact sources can be reviewed before any
        mutation. Never overwrites an existing modality, never touches non-matching journals."""
        patterns = [p.lower() for p in (patterns or []) if p and p.strip()]
        if not patterns:
            return {"apply": False, "journals": {}, "total": 0}
        like = " OR ".join(f"lower(facets->>'journal') LIKE ${i+1}" for i in range(len(patterns)))
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            where = (f"(facets ? 'journal') AND NOT (facets ? 'modality') AND ({like})")
            rows = await conn.fetch(
                f"SELECT facets->>'journal' AS j, count(*) AS n FROM {self._table} "
                f"WHERE {where} GROUP BY 1 ORDER BY 2 DESC", *patterns)
            journals = {r["j"]: int(r["n"]) for r in rows}
            total = sum(journals.values())
            if not apply or not total:
                return {"apply": False, "journals": journals, "total": total}
            res = await conn.execute(
                f"UPDATE {self._table} SET facets = facets || $%d::jsonb WHERE {where}"
                % (len(patterns) + 1),
                *patterns, json.dumps({"modality": modality}))
        try:
            updated = int((res or "UPDATE 0").split()[-1])
        except ValueError:
            updated = 0
        return {"apply": True, "journals": journals, "updated": updated}

    # --- port ---
    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.RETRIEVAL, Capability.PRECISION})

    def covers(self) -> FacetFilter:
        return dict(self._covers)

    def make_block_loader(self, tenant_id: str, workspace_id: str | None = None):
        def _load(document_id: str, block_id: str) -> str | None:
            return self._cache.get((tenant_id, document_id, block_id))
        return _load

    def _filter_sql(self, req: RetrievalRequest) -> tuple[str, list]:
        preds = ["tenant_id = $1"]
        params: list = [req.tenant_id]
        if req.workspace_id is None:
            preds.append("workspace_id IS NULL")
        else:
            params.append(req.workspace_id)
            preds.append(f"(workspace_id IS NULL OR workspace_id = ${len(params)})")
        for key, allowed in req.facets.items():
            vals = [allowed] if isinstance(allowed, str) else list(allowed)
            params.append(key)
            key_idx = len(params)
            params.append(vals)
            preds.append(f"(facets ->> ${key_idx}) = ANY(${len(params)})")
        # exclusion: drop a block only if it HAS the key with a listed value (untagged passes)
        for key, banned in getattr(req, "exclude_facets", {}).items():
            vals = [banned] if isinstance(banned, str) else list(banned)
            params.append(key)
            key_idx = len(params)
            params.append(vals)
            preds.append(f"(NOT (facets ? ${key_idx}) OR (facets ->> ${key_idx}) <> ALL(${len(params)}))")
        return " AND ".join(preds), params

    async def search(self, req: RetrievalRequest) -> list[BlockHit]:
        pool = await self._get_pool()
        where, params = self._filter_sql(req)
        pool_n = req.fetch_pool
        qvec = req.query_embedding

        # candidate generation: lexical (tsv) ∪ dense (ANN), both within the filter.
        legs_sql: list[str] = []
        q_terms = _tokens(req.query)
        if q_terms:
            params.append(" | ".join(q_terms))          # OR query for recall (like factra)
            q_idx = len(params)
            legs_sql.append(
                f"SELECT block_id, document_id FROM f "
                f"WHERE tsv @@ to_tsquery('english', ${q_idx}) "
                f"ORDER BY ts_rank_cd(tsv, to_tsquery('english', ${q_idx})) DESC LIMIT {pool_n}")
        if qvec is not None:
            params.append(qvec)
            v_idx = len(params)
            legs_sql.append(
                f"SELECT block_id, document_id FROM f "
                f"WHERE embedding IS NOT NULL ORDER BY embedding <=> ${v_idx} LIMIT {pool_n}")
        if not legs_sql:
            return []
        cand_union = " UNION ".join(f"({s})" for s in legs_sql)
        sql = f"""
            WITH f AS (SELECT * FROM {self._table} WHERE {where}),
                 cand AS ({cand_union})
            SELECT f.block_id, f.document_id, f.text, f.embedding, f.facets,
                   f.document_title, f.content_type, f.source_key
            FROM f JOIN cand USING (block_id, document_id)
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)

        import json
        cands: list[Candidate] = []
        for r in rows:
            self._cache[(req.tenant_id, r["document_id"], r["block_id"])] = r["text"]
            raw = r["embedding"]
            if raw is None:
                emb = ()
            elif hasattr(raw, "to_list"):        # pgvector.Vector
                emb = tuple(raw.to_list())
            else:                                # numpy array / sequence
                emb = tuple(float(x) for x in raw)
            facets = r["facets"] if isinstance(r["facets"], dict) else json.loads(r["facets"])
            cands.append(Candidate(
                block_id=r["block_id"], document_id=r["document_id"], text=r["text"],
                embedding=emb, facets=facets,
                locator=Locator("block_span", r["document_id"], {"block_id": r["block_id"]}),
                document_title=r["document_title"], content_type=r["content_type"],
                source_key=r["source_key"],
            ))
        hits = rank_candidates(req.query, qvec, cands, k=req.k, fetch_pool=req.fetch_pool)
        # Corpus currency (Evidence Pulse C1): RETRACTED sources never ground an answer (hard
        # exclusion); SUPERSEDED blocks are stable-partitioned to the bottom — demoted, never
        # deleted (they may be the only source for an unrevised topic, and are the evidence a
        # change brief cites). Retrieval-level, so a superseded edition cannot crowd the current
        # one out of the candidate pool before claim ranking ever sees it (spec A3).
        if self._currency_demote:
            hits = [h for h in hits if not (h.facets or {}).get("retracted")]
            hits.sort(key=lambda h: bool((h.facets or {}).get("superseded_by")))
        return hits
