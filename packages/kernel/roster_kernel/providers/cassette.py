"""Generic record/replay cassette store, shared by all provider ports.

A cassette is a JSON file per (namespace) holding a map: request-hash → list of
recorded responses. The hash is SHA-256 over the canonicalized request parts, so
lookup is exact and order-independent within a request. Multiple calls with the
same request accumulate as a list and replay in insertion order.

Determinism is the whole point: a replay MISS raises CassetteMiss rather than
silently falling back to a live call.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CassetteMiss(Exception):
    namespace: str
    request_hash: str
    hint: str = ""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return (
            f"cassette miss in namespace={self.namespace!r} "
            f"hash={self.request_hash[:24]}… — re-record with "
            f"ROSTER_PROVIDER_MODE=record. {self.hint}"
        )


def hash_request(*parts: Any) -> str:
    """Stable SHA-256 over JSON-canonicalized request parts."""
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Cassette:
    """One JSON file of recorded responses for a namespace."""

    root: Path
    namespace: str
    _data: dict[str, list[Any]] = field(default_factory=dict)
    _cursor: dict[str, int] = field(default_factory=dict)
    _loaded: bool = False

    @property
    def path(self) -> Path:
        return self.root / f"{self.namespace}.json"

    def _load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            self._data = json.loads(self.path.read_text())
        self._loaded = True

    def replay(self, request_hash: str, *, hint: str = "") -> Any:
        self._load()
        calls = self._data.get(request_hash)
        if not calls:
            raise CassetteMiss(self.namespace, request_hash, hint)
        i = self._cursor.get(request_hash, 0)
        # Clamp to the last recorded response if replayed more times than recorded.
        entry = calls[min(i, len(calls) - 1)]
        self._cursor[request_hash] = i + 1
        return entry

    def record(self, request_hash: str, response: Any) -> None:
        self._load()
        self._data.setdefault(request_hash, []).append(response)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True, default=str))
