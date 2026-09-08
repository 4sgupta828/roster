"""Run the voice connectors into the corpus.

Three settings differ from the generic corpus ingest and all three are load-bearing:

  target_chars=0   The generic path coalesces paragraphs into ~1,800-character blocks, which is right
                   for a paper and fatal here: it would weld twenty chapters into one block and every
                   chapter would lose its own timestamp.
  min_chars=1      A chapter title is short. The generic path drops anything under 40 characters as
                   metadata noise, which would delete most of this corpus.
  embedder=None    A corpus that grows keyword-only today beats one that does not exist, and this
                   material does not justify model spend before anyone has searched it. `rs_block.tsv`
                   is a generated column, so the rows are searchable the moment they land; vectors
                   backfill when they are worth buying (docs/specs/voices.md §4).
"""
from __future__ import annotations

import logging

from roster_kernel.runtime.ingest import ingest_connector_to_postgres

log = logging.getLogger(__name__)

VOICE_CONNECTORS = ("practitioner_essay", "show_notes", "youtube_chapters")


async def ingest_voices(manifest, pg_source, *, tenant_id: str, connectors=VOICE_CONNECTORS,
                        limit: int = 60, embedder=None) -> dict:
    """Ingest each voice connector. Returns {connector: blocks}, and never lets one bad feed stop the
    rest — a publisher changing their feed shape must not take the mode down."""
    out: dict[str, int] = {}
    for key in connectors:
        conn = manifest.connectors.get(key)
        if conn is None:
            out[key] = 0
            continue
        try:
            out[key] = await ingest_connector_to_postgres(
                conn, pg_source, tenant_id=tenant_id, embedder=embedder,
                window={"query": "", "limit": limit},
                min_chars=1,        # a chapter title is short; the generic 40-char floor deletes it
                target_chars=0,     # NEVER coalesce: one chapter must stay one block
            )
        except Exception as e:              # noqa: BLE001 — one connector's outage is not an outage
            log.warning("voices: connector %s failed: %s", key, e)
            out[key] = 0
    return out


async def mark_boilerplate(pool, *, table: str = "rs_block", min_sources: int = 3) -> int:
    """Stamp `boilerplate` on text that repeats across three or more items of ONE source.

    Newsletters paste a sidebar of their own post titles into every issue, and that text ranks like
    prose: without this the same block answers five queries in a row. Stamping rather than deleting
    keeps the decision reversible and visible.
    """
    from .search import VOICE_SOURCE_KEYS
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT md5(text) AS h, source_key, count(DISTINCT document_id) AS n
                FROM {table} WHERE source_key = ANY($1) AND length(text) BETWEEN 40 AND 2000
                  AND NOT (facets ? 'boilerplate')
                GROUP BY 1, 2 HAVING count(DISTINCT document_id) >= $2""",
            list(VOICE_SOURCE_KEYS), int(min_sources))
        if not rows:
            return 0
        n = 0
        for r in rows:
            res = await conn.execute(
                f"UPDATE {table} SET facets = facets || '{{\"boilerplate\":\"1\"}}'::jsonb "
                f"WHERE source_key = $2 AND md5(text) = $1", r["h"], r["source_key"])
            n += int(res.rsplit(" ", 1)[-1] or 0)
        return n
