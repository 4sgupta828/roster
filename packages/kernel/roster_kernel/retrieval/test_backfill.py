"""The vector backfill: resumable, idempotent, and it never spins on rows it cannot embed."""
import asyncio


class _Conn:
    def __init__(self, store): self.store = store
    async def fetch(self, sql, *args):
        keys = args[0] if args else None
        rows = [r for r in self.store if r["embedding"] is None and (keys is None or r["source_key"] in keys)]
        lim = int(sql.rsplit("LIMIT", 1)[1])
        return [{"document_id": r["document_id"], "block_id": r["block_id"], "text": r["text"]} for r in rows[:lim]]
    async def fetchrow(self, sql, *args):
        keys = args[0] if args else None
        rows = [r for r in self.store if r["embedding"] is None and (keys is None or r["source_key"] in keys)]
        return {"n": len(rows), "c": sum(len(r["text"]) for r in rows)}
    async def execute(self, sql, *args):
        doc, blk, vec = args
        for r in self.store:
            if r["document_id"] == doc and r["block_id"] == blk:
                r["embedding"] = vec
    def transaction(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Pool:
    def __init__(self, store): self.store = store
    def acquire(self): return _Conn(self.store)


class _Acq(_Conn):
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _pool(store):
    p = _Pool(store)
    p.acquire = lambda: _Acq(store)
    return p


def _rows(n, key="k"):
    # (document_id, block_id) is the real primary key, so rows of different sources never collide
    return [{"document_id": "d-" + key, "block_id": f"b{i}", "text": f"text {i}", "source_key": key,
             "embedding": None} for i in range(n)]


def _run(c):
    loop = asyncio.new_event_loop()
    try: return loop.run_until_complete(c)
    finally: loop.close()


def test_counts_what_a_run_would_cost_before_running_it():
    from roster_kernel.retrieval.backfill import count_missing
    n, chars = _run(count_missing(_pool(_rows(5)), source_keys=["k"]))
    assert n == 5 and chars == sum(len(f"text {i}") for i in range(5))


def test_every_row_gets_a_vector_and_a_second_run_does_nothing():
    from roster_kernel.retrieval.backfill import embed_missing
    store = _rows(5)
    calls = []

    async def embed(texts):
        calls.append(len(texts))
        return ["[0.1]"] * len(texts)
    out = _run(embed_missing(_pool(store), embed, source_keys=["k"], batch=2))
    assert out == {"seen": 5, "embedded": 5, "failed": 0}
    assert calls == [2, 2, 1]                              # batched, not one call per row
    assert all(r["embedding"] == "[0.1]" for r in store)
    assert _run(embed_missing(_pool(store), embed, source_keys=["k"]))["embedded"] == 0   # idempotent


def test_a_provider_outage_stops_the_run_instead_of_spinning():
    """A failing provider fails the next batch too; retrying it forever is how a job burns a night."""
    from roster_kernel.retrieval.backfill import embed_missing
    store = _rows(10)

    async def embed(texts): raise RuntimeError("429 insufficient_quota")
    out = _run(embed_missing(_pool(store), embed, source_keys=["k"], batch=3))
    assert out["embedded"] == 0 and out["failed"] == 3 and out["seen"] == 3
    assert all(r["embedding"] is None for r in store)


def test_a_batch_that_embeds_nothing_stops_rather_than_re_reading_the_same_rows():
    from roster_kernel.retrieval.backfill import embed_missing
    store = _rows(6)

    async def embed(texts): return [None] * len(texts)
    out = _run(embed_missing(_pool(store), embed, source_keys=["k"], batch=3))
    assert out["embedded"] == 0 and out["failed"] == 3


def test_only_the_named_sources_are_touched():
    from roster_kernel.retrieval.backfill import embed_missing
    store = _rows(2, "mine") + _rows(2, "someone_elses")

    async def embed(texts): return ["[0.2]"] * len(texts)
    _run(embed_missing(_pool(store), embed, source_keys=["mine"]))
    assert [r["embedding"] for r in store] == ["[0.2]", "[0.2]", None, None]
