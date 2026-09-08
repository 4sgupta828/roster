"""Give vectors to corpus rows that have none — the other half of `embedder=None` ingest.

A corpus can be built keyword-only (`rs_block.tsv` is a generated column, so rows are searchable the
moment they land) and vectorised later, when the semantics are worth buying. This is that later pass:
it reads the rows whose `embedding IS NULL`, embeds them in batches, and writes the vectors back.

Domain-free by construction — it names no source, no facet and no vocabulary. The caller says WHICH
rows (`source_keys`) and supplies the embedder; the mechanics of "some rows lack vectors, fill them"
belong to any vertical that ingests text.

Idempotent and resumable: it only ever selects rows with no vector, so a run that dies halfway is
continued by running it again. A batch that fails to embed is skipped, not retried forever — one bad
row must not stop the rest of a corpus getting its vectors.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def count_missing(pool, *, source_keys: list[str] | None = None, table: str = "rs_block") -> tuple[int, int]:
    """(rows without a vector, total characters in them) — what a run would cost, before it runs."""
    where = "embedding IS NULL" + (" AND source_key = ANY($1)" if source_keys else "")
    args = [list(source_keys)] if source_keys else []
    async with pool.acquire() as conn:
        r = await conn.fetchrow(f"SELECT count(*) n, coalesce(sum(length(text)), 0) c FROM {table} WHERE {where}", *args)
    return int(r["n"] or 0), int(r["c"] or 0)


async def embed_missing(pool, embed_batch, *, source_keys: list[str] | None = None,
                        table: str = "rs_block", batch: int = 128, limit: int = 100_000,
                        max_chars: int = 8000) -> dict:
    """Fill in the missing vectors. `embed_batch(list[str]) -> list[str | None]` returns one pgvector
    literal per text (None for a text it could not embed). Returns {seen, embedded, failed}."""
    where = "embedding IS NULL" + (" AND source_key = ANY($1)" if source_keys else "")
    args = [list(source_keys)] if source_keys else []
    seen = embedded = failed = 0
    while seen < limit:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT document_id, block_id, left(text, {int(max_chars)}) AS text FROM {table} "
                f"WHERE {where} ORDER BY document_id, block_id LIMIT {int(min(batch, limit - seen))}", *args)
        if not rows:
            break
        seen += len(rows)
        try:
            vecs = await embed_batch([str(r["text"] or "") for r in rows])
        except Exception as e:      # noqa: BLE001 — one bad batch is not a dead run
            log.warning("backfill: batch of %d failed: %s", len(rows), e)
            failed += len(rows)
            break                   # a provider that is failing will fail the next batch too
        pairs = [(r["document_id"], r["block_id"], v) for r, v in zip(rows, vecs) if v]
        failed += len(rows) - len(pairs)
        if not pairs:
            break                   # nothing came back: stop rather than spin over the same rows
        async with pool.acquire() as conn:
            async with conn.transaction():
                for doc, blk, vec in pairs:
                    await conn.execute(
                        f"UPDATE {table} SET embedding = $3::vector WHERE document_id = $1 AND block_id = $2",
                        doc, blk, vec)
        embedded += len(pairs)
    return {"seen": seen, "embedded": embedded, "failed": failed}
