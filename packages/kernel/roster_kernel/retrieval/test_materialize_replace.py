"""Evidence Pulse A1 — clean-replace on re-ingest (the mixed-edition bug).

Block ids are content-addressed, so re-ingesting an edited document INSERTS its changed blocks;
without clean-replace the previous text's rows linger under the same document_id and the corpus
serves both editions mixed. These tests pin the contract: after materializing a run, every touched
document's stale rows are deleted; untouched documents are never touched.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class _Doc:
    id: str
    tenant_id: str = "t"
    workspace_id: str | None = None
    facets: dict = field(default_factory=dict)
    title: str = "T"
    content_type: str = "text/markdown"
    source_key: str = "s"


@dataclass
class _Block:
    content_key: str
    text: str
    facets: dict = field(default_factory=dict)


class _Repo:
    def __init__(self, docs: dict[str, list[_Block]]):
        self._docs = docs

    def iter_documents(self):
        return [_Doc(id=d) for d in self._docs]

    def blocks_for(self, doc_id):
        return self._docs[doc_id]

    def block_content(self, key):
        return None                      # no embeddings needed for this contract


class _FakePg:
    def __init__(self):
        self.upserted: list[dict] = []
        self.deletes: list[tuple[str, str, list[str]]] = []

    async def upsert_blocks(self, rows):
        self.upserted += rows

    async def delete_stale_blocks(self, *, tenant_id, document_id, keep_block_ids):
        self.deletes.append((tenant_id, document_id, keep_block_ids))
        return 0


def test_clean_replace_deletes_stale_rows_per_touched_document():
    from roster_kernel.retrieval.materialize import materialize_to_postgres
    repo = _Repo({"doc1": [_Block("k_b", "B"), _Block("k_c", "C")],   # edition 2: B kept, C new (A gone)
                  "doc2": [_Block("k_x", "X")]})
    pg = _FakePg()
    added = asyncio.run(materialize_to_postgres(repo, pg))
    assert added == 3
    # every document THIS run touched gets a stale-row sweep with exactly its new key set
    assert sorted(pg.deletes) == [("t", "doc1", ["k_b", "k_c"]), ("t", "doc2", ["k_x"])]


def test_pg_source_without_delete_support_still_works():
    from roster_kernel.retrieval.materialize import materialize_to_postgres

    class _Minimal:
        def __init__(self): self.rows = []
        async def upsert_blocks(self, rows): self.rows += rows

    repo = _Repo({"doc1": [_Block("k_a", "A")]})
    pg = _Minimal()
    assert asyncio.run(materialize_to_postgres(repo, pg)) == 1   # no crash, blocks land
