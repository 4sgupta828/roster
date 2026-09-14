"""Tests for the ExternalRecordSearch port + cassette wrapper (no network, no spend)."""
from __future__ import annotations

import asyncio

from .base import ProviderMode
from .record_search import (
    CassetteRecordSearch,
    CompositeRecordSearch,
    ExternalRecord,
    FakeRecordSearch,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_fake_returns_canned_and_caps():
    recs = [ExternalRecord(id=str(i), source="pdl", title=f"r{i}", text="t") for i in range(5)]
    fake = FakeRecordSearch({"q": recs})
    got = _run(fake.search("q", max_results=3))
    assert [r.id for r in got] == ["0", "1", "2"]
    assert _run(fake.search("other")) == []


def test_composite_dedupes_by_source_and_id():
    a = FakeRecordSearch({"q": [ExternalRecord(id="1", source="pdl"), ExternalRecord(id="2", source="pdl")]})
    b = FakeRecordSearch({"q": [ExternalRecord(id="1", source="pdl"), ExternalRecord(id="9", source="exa")]})
    got = _run(CompositeRecordSearch([a, b]).search("q"))
    keys = {(r.source, r.id) for r in got}
    assert keys == {("pdl", "1"), ("pdl", "2"), ("exa", "9")}   # (pdl,1) not duplicated


def test_cassette_record_then_replay_roundtrip(tmp_path):
    inner = FakeRecordSearch({"q": [ExternalRecord(id="1", source="pdl", title="Ada", text="ml",
                                                   url="http://x", fields={"skills": ["ml"]}, score=0.9)]})
    rec = CassetteRecordSearch(inner, cassette_root=tmp_path, mode=ProviderMode.RECORD)
    first = _run(rec.search("q", max_results=5))
    assert first[0].fields == {"skills": ["ml"]}
    # Replay with NO inner client must reproduce the recorded record exactly.
    replay = CassetteRecordSearch(None, cassette_root=tmp_path, mode=ProviderMode.REPLAY)
    second = _run(replay.search("q", max_results=5))
    assert len(second) == 1
    assert second[0].id == "1" and second[0].source == "pdl" and second[0].score == 0.9
    assert second[0].fields == {"skills": ["ml"]}
