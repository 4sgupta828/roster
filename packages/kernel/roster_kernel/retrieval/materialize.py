"""Materialize the corpus repository into a retrieval index — domain-free.

Joins Document (facets + tenant/workspace) × Block (locator) × BlockContent
(embedding) into IndexedBlocks the retrieval source can search. This is the
in-memory analogue of the SQL join a PostgresRetrievalSource does at query time;
keeping it explicit lets the whole ingest→retrieve path run offline.
"""
from __future__ import annotations

from roster_kernel.contract.dto import Locator
from roster_kernel.corpus.repository import CorpusRepository
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


def _pg_safe(s):
    """Make a string safe for a Postgres `text` column: strip NUL bytes (Postgres text cannot hold
    \\x00) and drop any invalid/half-surrogate UTF-8 that would raise 'invalid byte sequence for
    encoding UTF8' on INSERT — which otherwise fails the WHOLE batch and loses every doc in the job
    (seen on some arXiv/source docs). Non-strings pass through unchanged."""
    if not isinstance(s, str):
        return s
    if "\x00" in s:
        s = s.replace("\x00", "")
    # round-trip through UTF-8 dropping anything Postgres would reject (lone surrogates, etc.)
    return s.encode("utf-8", "ignore").decode("utf-8", "ignore")


def _safe_facets(facets: dict) -> dict:
    """Sanitize facet keys+values (they land in a jsonb text column too)."""
    return {_pg_safe(k): _pg_safe(v) for k, v in (facets or {}).items()}


def materialize(repo: CorpusRepository, source: InMemoryRetrievalSource) -> int:
    """Load all embedded blocks from `repo` into `source`. Returns count added."""
    added = 0
    for doc in repo.iter_documents():
        for block in repo.blocks_for(doc.id):
            bc = repo.block_content(block.content_key)
            embedding = tuple(bc.embedding) if (bc and bc.embedding) else ()
            # narrowing facets: document dims + any per-block tags (block wins).
            facets = {**doc.facets, **block.facets}
            source.add(IndexedBlock(
                block_id=block.content_key,       # content-addressed block id
                document_id=doc.id,
                text=block.text,
                tenant_id=doc.tenant_id,
                workspace_id=doc.workspace_id,
                embedding=embedding,
                facets=facets,
                locator=Locator("block_span", doc.id, {"block_id": block.content_key}),
                document_title=doc.title,
                content_type=doc.content_type,
                source_key=doc.source_key,
            ))
            added += 1
    return added


async def materialize_to_postgres(repo: CorpusRepository, pg_source, *, batch_size: int = 500,
                                  facet_overrides: dict | None = None) -> int:
    """Same join, into a PostgresRetrievalSource's index table — BATCHED upserts
    (one round-trip per `batch_size` blocks instead of one per block).

    `facet_overrides` (generic) is merged LAST onto every block's facets — the caller-chosen way to
    stamp a whole ingest run (e.g. source_country for a country-specific ingest); it never encodes
    domain meaning here, only applies whatever the caller passed.

    CLEAN-REPLACE (Evidence Pulse A1): block ids are content-addressed, so re-ingesting an EDITED
    document inserts its changed blocks while the previous text's rows would linger under the same
    document_id (the mixed-edition bug — one document serving both editions). After upserting, any
    row of a document touched by THIS run whose block_id is not in the run's key set is deleted.
    Documents not in this run are never touched."""
    ov = facet_overrides or {}
    rows: list[dict] = []
    added = 0
    doc_keys: dict[tuple[str, str], set[str]] = {}   # (tenant_id, document_id) -> this run's block ids
    for doc in repo.iter_documents():
        for block in repo.blocks_for(doc.id):
            bc = repo.block_content(block.content_key)
            doc_keys.setdefault((doc.tenant_id, doc.id), set()).add(block.content_key)
            rows.append(dict(
                tenant_id=doc.tenant_id, document_id=doc.id, block_id=block.content_key,
                text=_pg_safe(block.text), embedding=list(bc.embedding) if (bc and bc.embedding) else None,
                facets=_safe_facets({**doc.facets, **block.facets, **ov}), workspace_id=doc.workspace_id,
                document_title=_pg_safe(doc.title), content_type=doc.content_type, source_key=doc.source_key,
            ))
            if len(rows) >= batch_size:
                await pg_source.upsert_blocks(rows)
                added += len(rows)
                rows = []
    if rows:
        await pg_source.upsert_blocks(rows)
        added += len(rows)
    if hasattr(pg_source, "delete_stale_blocks"):
        for (tenant_id, document_id), keys in doc_keys.items():
            await pg_source.delete_stale_blocks(
                tenant_id=tenant_id, document_id=document_id, keep_block_ids=sorted(keys))
    return added
