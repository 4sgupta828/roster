"""Provider-port tests — all offline, zero credits.

Proves: (1) record→replay round-trips an LLM call with no inner client on
replay; (2) a replay miss is loud; (3) live-under-pytest is blocked unless
opted in; (4) FakeLLM / FakeEmbedder are deterministic.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from roster_kernel.providers.base import LiveCallForbidden, ProviderMode, guard_live
from roster_kernel.providers.cassette import CassetteMiss
from roster_kernel.providers.embeddings import CassetteEmbedder, FakeEmbedder
from roster_kernel.providers.llm import CassetteLLM, FakeLLM, LLMResult


class _Answer(BaseModel):
    text: str
    score: int


def _builder(system, messages, response_format):
    return _Answer(text=f"echo:{messages[-1]['content']}", score=len(messages))


def test_fake_llm_is_deterministic() -> None:
    llm = FakeLLM(_builder)
    r1 = asyncio.run(llm.complete(system="s", messages=[{"content": "hi"}], response_format=_Answer))
    r2 = asyncio.run(llm.complete(system="s", messages=[{"content": "hi"}], response_format=_Answer))
    assert isinstance(r1, LLMResult)
    assert r1.parsed == r2.parsed == _Answer(text="echo:hi", score=1)


def test_record_then_replay_roundtrip(tmp_path) -> None:
    inner = FakeLLM(_builder)
    rec = CassetteLLM(inner, cassette_root=tmp_path, namespace="unit", mode=ProviderMode.RECORD)
    recorded = asyncio.run(
        rec.complete(system="s", messages=[{"content": "q"}], response_format=_Answer)
    )
    # Replay with NO inner client — proves it never touches the network/credits.
    rep = CassetteLLM(None, cassette_root=tmp_path, namespace="unit", mode=ProviderMode.REPLAY)
    replayed = asyncio.run(
        rep.complete(system="s", messages=[{"content": "q"}], response_format=_Answer)
    )
    assert replayed.parsed == recorded.parsed


def test_replay_miss_is_loud(tmp_path) -> None:
    rep = CassetteLLM(None, cassette_root=tmp_path, namespace="empty", mode=ProviderMode.REPLAY)
    with pytest.raises(CassetteMiss):
        asyncio.run(rep.complete(system="s", messages=[{"content": "x"}], response_format=_Answer))


def test_live_call_blocked_under_pytest() -> None:
    with pytest.raises(LiveCallForbidden):
        guard_live(ProviderMode.LIVE)


def test_cassette_embedder_record_then_replay(tmp_path) -> None:
    inner = FakeEmbedder(dim=16)
    rec = CassetteEmbedder(inner, cassette_root=tmp_path, namespace="emb", dim=16,
                           mode=ProviderMode.RECORD)
    recorded = rec.embed(["a", "b"])
    # Replay with NO inner embedder — proves no network/credits.
    rep = CassetteEmbedder(None, cassette_root=tmp_path, namespace="emb", dim=16,
                           mode=ProviderMode.REPLAY)
    assert rep.embed(["a", "b"]) == recorded


def test_fake_embedder_deterministic_and_normed() -> None:
    emb = FakeEmbedder(dim=32)
    a = emb.embed(["hello", "world"])
    b = emb.embed(["hello", "world"])
    assert a == b
    assert len(a) == 2 and all(len(v) == 32 for v in a)
    assert abs(sum(x * x for x in a[0]) ** 0.5 - 1.0) < 1e-6
