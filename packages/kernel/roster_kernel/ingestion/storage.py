"""Content-addressed object store — domain-free.

Bytes in → SHA-256 key out; identical bytes dedupe to one object regardless of
which entity/source they came from. Production backends are disk / S3-R2; the
port here is backend-agnostic and `InMemoryObjectStore` is the offline impl.
"""
from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable


def content_key(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class ObjectStore(Protocol):
    def put(self, data: bytes) -> str: ...   # returns the sha256 key
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self.put_count = 0          # number of actual stores (dedup misses)

    def put(self, data: bytes) -> str:
        key = content_key(data)
        if key not in self._data:
            self._data[key] = data
            self.put_count += 1
        return key

    def get(self, key: str) -> bytes:
        return self._data[key]

    def exists(self, key: str) -> bool:
        return key in self._data
