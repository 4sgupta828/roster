"""External-record search provider port + deterministic fake.

Mechanism only. A domain-agnostic port for querying an EXTERNAL structured-record
provider (a live directory / enrichment API) and getting back typed records. The
kernel knows nothing about what a record *is* — the vertical supplies the concrete
provider clients and maps `ExternalRecord.fields` into its own entity vocabulary.

Mirrors `websearch.py` (WebSearchClient): same cassette-backed replay/record/live
discipline, so tests and evals cost nothing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from .base import ProviderMode, guard_live, resolve_mode
from .cassette import Cassette, hash_request


@dataclass
class ExternalRecord:
    id: str                                   # provider-stable id (for dedup / upsert keying)
    source: str                               # which provider surfaced this ("pdl", "exa", …)
    title: str = ""                           # a short human label for the record
    text: str = ""                            # free text used for embedding / relevance
    url: str = ""                             # canonical link when the provider has one
    fields: dict = field(default_factory=dict)  # provider-native structured payload (opaque here)
    score: float = 0.0                        # provider-reported relevance, when any (0 = none)


@runtime_checkable
class ExternalRecordSearch(Protocol):
    async def search(self, query: str, *, max_results: int = 20,
                     filters: dict | None = None) -> list[ExternalRecord]: ...


class FakeRecordSearch:
    """Offline record search returning canned records per query (tests)."""

    def __init__(self, canned: dict[str, list[ExternalRecord]] | None = None):
        self._canned = canned or {}

    async def search(self, query: str, *, max_results: int = 20,
                     filters: dict | None = None) -> list[ExternalRecord]:
        return list(self._canned.get(query, []))[:max_results]


class CompositeRecordSearch:
    """ADDITIVE leg: fan out to several providers CONCURRENTLY and merge, deduping by (source, id).
    Each provider is best-effort — a failure or empty list contributes nothing. Provider-major
    interleave so every provider gets representation rather than only the first one's list."""

    def __init__(self, clients: list):
        self._clients = [c for c in clients if c is not None]

    async def search(self, query: str, *, max_results: int = 20,
                     filters: dict | None = None) -> list[ExternalRecord]:
        import asyncio
        lists = await asyncio.gather(
            *(c.search(query, max_results=max_results, filters=filters) for c in self._clients),
            return_exceptions=True)
        lists = [r for r in lists if isinstance(r, list)]
        out: list[ExternalRecord] = []
        seen: set[tuple[str, str]] = set()
        for rank in range(max((len(r) for r in lists), default=0)):
            for r in lists:
                if rank < len(r):
                    rec = r[rank]
                    key = (rec.source, rec.id)
                    if rec.id and key not in seen:
                        seen.add(key)
                        out.append(rec)
        return out[:max_results]


class CassetteRecordSearch:
    """Wrap an inner ExternalRecordSearch with replay/record/live — free eval/CI, no live spend."""

    def __init__(self, inner: ExternalRecordSearch | None, *, cassette_root: Path,
                 namespace: str = "records", mode: ProviderMode | str | None = None):
        self._inner = inner
        self._mode = resolve_mode(mode)
        self._cassette = Cassette(root=cassette_root, namespace=namespace)

    async def search(self, query: str, *, max_results: int = 20,
                     filters: dict | None = None) -> list[ExternalRecord]:
        # Cassette key folds in the filters so a different filter set records under its own key.
        import json as _json
        fkey = _json.dumps(filters or {}, sort_keys=True)
        key = hash_request("records", query, max_results, fkey)
        if self._mode is ProviderMode.REPLAY:
            return [ExternalRecord(**r) for r in self._cassette.replay(key, hint=query)]
        guard_live(self._mode)
        if self._inner is None:
            raise RuntimeError("CassetteRecordSearch in record/live mode requires an inner client")
        results = await self._inner.search(query, max_results=max_results, filters=filters)
        if self._mode is ProviderMode.RECORD:
            self._cassette.record(key, [asdict(r) for r in results])
        return results
