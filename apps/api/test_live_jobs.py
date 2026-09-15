"""Live ATS jobs leg — offline tests (FakeRecordSearch + deterministic embed, no network/spend)."""
from __future__ import annotations

import asyncio

import api.live_jobs as lj
from roster_kernel.providers.record_search import ExternalRecord, FakeRecordSearch
from roster_vertical.live_jobs import normalize_job_record


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _rec(url, title, text="ml engineer"):
    return ExternalRecord(id=url, source="exa", title=title, text=text, url=url, score=0.7)


def test_normalize_company_from_ats_url_and_title():
    j = normalize_job_record(_rec("https://boards.greenhouse.io/faire/jobs/8731172002",
                                  "Senior Staff Machine Learning Platform Engineer at Faire"))
    assert j["company"] == "Faire"                 # from URL slug
    assert j["title"] == "Senior Staff Machine Learning Platform Engineer"
    assert j["source"] == "greenhouse" and j["live"] is True and j["citation"] is None
    assert j["found_by"] == "live:exa"
    # "Company - Title" ordering
    j2 = normalize_job_record(_rec("https://jobs.lever.co/spotify/abc", "Spotify - Senior ML Engineer"))
    assert j2["company"] == "Spotify" and j2["title"] == "Senior ML Engineer" and j2["source"] == "lever"


def test_normalize_drops_board_landing_pages():
    assert normalize_job_record(_rec("https://jobs.ashbyhq.com/handshake/x", "Jobs")) is None
    assert normalize_job_record(_rec("https://jobs.ashbyhq.com/foo/y", "Careers")) is None


def _patch_embed(monkeypatch, mapping):
    monkeypatch.setattr(lj, "embed_texts", lambda texts, **kw: [mapping.get((t or "").strip(), None) for t in texts])


def test_merge_live_jobs_ranks_and_labels(monkeypatch):
    _patch_embed(monkeypatch, {"ml platform at scale": "[1,0,0]"})
    rec = _rec("https://boards.greenhouse.io/acme/jobs/1", "ML Platform Engineer at Acme", "ml platform at scale")
    client = FakeRecordSearch({"senior ml platform engineer": [rec]})
    out = _run(lj.merge_live_jobs("[1,0,0]", {}, search_client=client, query_text="senior ml platform engineer"))
    assert len(out) == 1
    assert out[0]["live"] is True and out[0]["company"] == "Acme"
    assert out[0]["_score"] > 0 and any("live" in r for r in out[0]["reasons"])


def test_merge_live_jobs_obeys_metro(monkeypatch):
    _patch_embed(monkeypatch, {})
    sf = _rec("https://boards.greenhouse.io/a/jobs/1", "ML Engineer at A", "Based in San Francisco, California")
    la = _rec("https://boards.greenhouse.io/b/jobs/2", "ML Engineer at B", "Based in Los Angeles, California")
    client = FakeRecordSearch({"ml engineer": [sf, la]})
    out = _run(lj.merge_live_jobs("[1,0,0]", {"metro": "bay_area", "country": "us"}, search_client=client,
                                  query_text="ml engineer"))
    cos = {r["company"] for r in out}
    assert "A" in cos and "B" not in cos           # SF kept, LA (different metro) dropped


def test_no_query_text_skips():
    assert _run(lj.merge_live_jobs("[1,0,0]", {}, search_client=FakeRecordSearch({}))) == []
