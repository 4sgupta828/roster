"""Offline tests for the podcast-transcript connector — NO network (episodes injected).

Prove discover→list→fetch produces a labeled expert-DISCUSSION document from a publisher-provided
transcript; that VTT/SRT normalization strips all cue timings/indices to clean prose; that facets stamp
`source_kind=podcast`; and that the transcript-only contract holds — an RSS item WITHOUT a
<podcast:transcript> element is excluded at discovery. Self-contained: no manifest, no classify.
"""
from __future__ import annotations

import asyncio

from . import podcast_doc
from .connectors.podcast import PodcastConnector

_VTT = """WEBVTT

00:00:00.000 --> 00:00:04.000
Welcome to the show, today we discuss transformers.

00:00:04.000 --> 00:00:09.500
<v Host>They changed everything about scaling.</v>
"""

_SRT = """1
00:00:00,000 --> 00:00:03,000
Great to have you here.

2
00:00:03,000 --> 00:00:07,000
Let's talk about inference costs.
"""

_EP = {
    "guid": "ep-1", "link": "https://pod.example/ep1",
    "title": "Scaling Transformers", "show": "Deep Tech Talk",
    "pubDate": "Wed, 02 Mar 2025 10:00:00 GMT",
    "transcript_url": "https://pod.example/ep1.vtt",
    "transcript_type": "text/vtt", "transcript_text": _VTT,
}

# Raw RSS with TWO items: one carries <podcast:transcript>, one does not.
_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>Deep Tech Talk</title>
    <item>
      <title>Has Transcript</title>
      <guid>ep-with</guid>
      <link>https://pod.example/with</link>
      <pubDate>Wed, 02 Mar 2025 10:00:00 GMT</pubDate>
      <podcast:transcript url="https://pod.example/with.vtt" type="text/vtt"/>
    </item>
    <item>
      <title>No Transcript</title>
      <guid>ep-without</guid>
      <link>https://pod.example/without</link>
      <pubDate>Thu, 03 Mar 2025 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


def test_discover_list_fetch():
    c = PodcastConnector(episodes=[_EP])
    ents = asyncio.run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["ep-1"]
    md = asyncio.run(c.fetch_artifact(asyncio.run(c.list_documents(ents[0]))[0])).decode()
    assert "Scaling Transformers" in md           # episode title
    assert "Deep Tech Talk" in md                 # show
    assert "discussion" in md.lower()             # discussion framing
    assert "transcript" in md.lower()             # transcript framing
    assert "Welcome to the show" in md            # normalized transcript prose
    assert "They changed everything about scaling" in md
    assert "-->" not in md                        # no cue timings leaked into the doc


def test_normalize_vtt_and_srt_reduce_to_prose():
    vtt = podcast_doc.normalize_transcript(_VTT, "text/vtt")
    assert "-->" not in vtt
    assert "WEBVTT" not in vtt
    assert "<v" not in vtt
    assert vtt == "Welcome to the show, today we discuss transformers. They changed everything about scaling."

    srt = podcast_doc.normalize_transcript(_SRT, "application/x-subrip")
    assert "-->" not in srt
    assert "\n" not in srt
    # SRT block indices ("1", "2") must NOT survive as standalone tokens.
    assert srt == "Great to have you here. Let's talk about inference costs."


def test_normalize_json_segments():
    payload = '{"segments":[{"startTime":0,"body":"First point."},{"startTime":5,"text":"Second point."}]}'
    assert podcast_doc.normalize_transcript(payload, "application/json") == "First point. Second point."


def test_facets_stamp_podcast():
    f = podcast_doc.facets(_EP)
    assert f["source_kind"] == "podcast"
    assert f["show"] == "Deep Tech Talk"
    assert f["entity_type"] == "podcast_episode"
    assert f["year"] == "2025"


def test_transcript_only_contract_filters_feed():
    # Only the item WITH a <podcast:transcript> element survives parsing.
    show, eps = PodcastConnector._parse_feed(_RSS)
    assert show == "Deep Tech Talk"
    assert [e["guid"] for e in eps] == ["ep-with"]
    assert eps[0]["transcript_url"] == "https://pod.example/with.vtt"
    assert eps[0]["transcript_type"] == "text/vtt"
