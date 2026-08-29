"""Embeddings provider port + backends (fake / local / OpenAI) + cassette wrapper.

A single port with pluggable backends. Default production backend is **OpenAI**
(user decision, for retrieval quality); LocalEmbedder (sentence-transformers) and
FakeEmbedder remain for zero-credit dev/eval. All metered calls can be wrapped by
CassetteEmbedder so eval/CI replay for free. The vector `dim` is fixed at the
index-schema level to whichever backend a deployment chooses.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path
from typing import Protocol, runtime_checkable

from .base import ProviderMode, guard_live, resolve_mode
from .cassette import Cassette, hash_request

# Default hosted model (user decision). 3-small=1536 dims; 3-large=3072.
OPENAI_EMBED_MODEL = "text-embedding-3-small"
OPENAI_EMBED_DIM = 1536


@runtime_checkable
class Embedder(Protocol):
    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder:
    """Deterministic, offline embeddings for tests — hashed, unit-norm vectors.

    Same text → same vector, no network. Not semantically meaningful; it exists
    so retrieval *plumbing* can be tested at zero cost.
    """

    def __init__(self, dim: int = 64):
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec: list[float] = []
            counter = 0
            # Expand a SHA-256 stream into `dim` floats in [-1, 1].
            while len(vec) < self._dim:
                digest = hashlib.sha256(f"{text}|{counter}".encode()).digest()
                for i in range(0, len(digest), 4):
                    if len(vec) >= self._dim:
                        break
                    (u,) = struct.unpack("<I", digest[i:i + 4])
                    vec.append((u / 2**31) - 1.0)
                counter += 1
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


class LocalEmbedder:
    """sentence-transformers backend — zero credit, runs on the box.

    Lazy-imports the model so the dependency is optional until used.
    """

    def __init__(self, model_id: str = "sentence-transformers/all-MiniLM-L6-v2"):
        self._model_id = model_id
        self._model = None
        self._dim: int | None = None

    def _ensure(self) -> None:
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # lazy, optional dep

            self._model = SentenceTransformer(self._model_id)
            self._dim = int(self._model.get_sentence_embedding_dimension())

    @property
    def dim(self) -> int:
        self._ensure()
        assert self._dim is not None
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        assert self._model is not None
        return [v.tolist() for v in self._model.encode(texts, normalize_embeddings=True)]


class OpenAIEmbedder:
    """Hosted OpenAI embeddings — the default production backend (user decision).

    Lazy-imports the client so the dep is optional until used. Costs credits, so
    wrap in CassetteEmbedder for dev/CI/eval. Vectors are L2-normalized for cosine.
    """

    def __init__(self, model: str = OPENAI_EMBED_MODEL, *, dim: int = OPENAI_EMBED_DIM,
                 api_key: str | None = None):
        self._model = model
        self._dim = dim
        self._api_key = api_key
        self._client = None

    def _ensure(self) -> None:
        if self._client is None:
            from openai import OpenAI  # lazy, optional dep

            self._client = OpenAI(api_key=self._api_key) if self._api_key else OpenAI()

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure()
        assert self._client is not None
        resp = self._client.embeddings.create(model=self._model, input=texts)
        out: list[list[float]] = []
        for item in resp.data:
            v = list(item.embedding)
            norm = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / norm for x in v])
        return out


class CassetteEmbedder:
    """Wrap an inner Embedder with replay/record/live — keeps eval credit-free."""

    def __init__(self, inner: "Embedder | None", *, cassette_root: Path, namespace: str,
                 dim: int, mode: ProviderMode | str | None = None):
        self._inner = inner
        self._dim = dim
        self._mode = resolve_mode(mode)
        self._cassette = Cassette(root=cassette_root, namespace=namespace)

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        key = hash_request("embed", self._dim, texts)
        if self._mode is ProviderMode.REPLAY:
            return self._cassette.replay(key, hint=f"dim={self._dim}")
        guard_live(self._mode)
        if self._inner is None:
            raise RuntimeError("CassetteEmbedder in record/live mode requires an inner embedder")
        vecs = self._inner.embed(texts)
        if self._mode is ProviderMode.RECORD:
            self._cassette.record(key, vecs)
        return vecs
