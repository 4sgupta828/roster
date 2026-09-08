"""The Voices corpus: the registry that admits a source, and the chapter contract that makes a
pointer a pointer. Each test here is a trap the design is meant to survive.
"""
import asyncio

from roster_vertical import voices_doc, voices_sources
from roster_vertical.authority import TechAuthorityPolicy
from roster_vertical.connectors.show_notes import ShowNotesConnector
from roster_vertical.connectors.youtube_chapters import YoutubeChaptersConnector
from roster_vertical.evidence_kind import classify


def _run(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


# ── the registry: measurement, not taste ─────────────────────────────────────────────────────────

def test_every_registered_source_still_meets_the_bar_it_was_admitted_on():
    assert voices_sources.check() == []


def test_a_podcast_or_channel_is_admitted_only_on_chapters():
    """Full show notes are not a substitute: without a timestamp there is no moment to point at."""
    for s in voices_sources.by_kind("podcast") + voices_sources.by_kind("youtube"):
        assert s.yield_pct >= 30, s.label


def test_the_rejections_are_recorded_with_their_reason():
    """The trap: talenthero.io scores 90% full text and 100% 'on-topic' and publishes astrology.
    The numeric bars cannot catch that, so the reason a source was kept out has to be written down."""
    why = dict(voices_sources.REJECTED)
    assert any("talenthero" in k for k in why)
    assert all(v.strip() for v in why.values())


def test_every_source_declares_who_it_is_for():
    assert {s.audience for s in voices_sources.SOURCES} <= {"job_seeker", "hiring_team", "both"}


# ── chapters: what is, and is not, a chapter ─────────────────────────────────────────────────────

def test_a_timestamp_must_open_its_line_so_prose_is_not_a_chapter():
    """"At 08:30 we start standup" is a sentence. A looser pattern turned sentences into chapters."""
    prose = "We talked about hiring. At 08:30 we start standup, and at 09:15 we review resumes.\n"
    assert voices_doc.chapters(prose) == []


def test_offsets_only_move_forward_a_backward_one_ends_the_list():
    notes = ("00:01:00 Opening thoughts on screening\n"
             "00:12:00 What a good scorecard contains\n"
             "00:26:00 Where structured interviews fail\n"
             "00:02:00 we raised at 2am and shipped it\n")
    got = [c["stamp"] for c in voices_doc.chapters(notes)]
    assert got == ["00:01:00", "00:12:00", "00:26:00"]


def test_fewer_than_three_timestamps_is_a_stray_time_not_a_chapter_list():
    assert voices_doc.chapters("00:01:00 Opening thoughts\n00:12:00 Second thing\n") == []


def test_a_chapter_pointer_is_not_evidence_at_any_level():
    p = TechAuthorityPolicy()
    for key in ("show_notes", "youtube_chapters"):
        kind = classify(key, {"source_kind": "chapter_pointer"})
        assert kind == "pointer"
        assert p.is_evidence(kind) is False and p.is_controlling(kind) is False


def test_practitioner_advice_is_evidence_of_an_opinion_and_never_controlling():
    p = TechAuthorityPolicy()
    kind = classify("practitioner_essay", {"source_kind": "essay"})
    assert kind == "practitioner_advice"
    assert p.is_evidence(kind) is True and p.is_controlling(kind) is False
    # and it must not outrank a named expert's analysis of a field
    assert p.outranks("expert_analysis", kind) is True


# ── the chapter-only contract, enforced at discovery on every path ────────────────────────────────

_WITH = {"guid": "g1", "title": "How fake candidates get through, with Jane Roe",
         "link": "https://show.test/ep1", "published": "Mon, 01 Sep 2026 07:00:00 +0000",
         "audience": "hiring_team", "voice_role": "recruiter",
         "summary": ("00:01:30 How fake candidates get through the first screen\n"
                     "00:12:05 What a live screen should catch\n"
                     "00:30:00 Where the process breaks down\n")}
_WITHOUT = {"guid": "g2", "title": "A chat about hiring", "link": "https://show.test/ep2",
            "published": "Tue, 02 Sep 2026 07:00:00 +0000", "summary": "We talked about hiring."}


def test_an_episode_with_no_chapter_list_never_becomes_an_entity():
    """Enforced at DISCOVERY, structurally — not by filtering later, and not only on the live path."""
    c = ShowNotesConnector(episodes=[_WITH, _WITHOUT])
    ents = _run(c.discover_entities({}))
    assert [e.native_id for e in ents] == ["g1"]


def test_a_video_with_no_chapter_list_never_becomes_an_entity_either():
    v_with = {**_WITH, "guid": "youtube:v1", "link": "https://www.youtube.com/watch?v=v1"}
    v_without = {**_WITHOUT, "guid": "youtube:v2"}
    c = YoutubeChaptersConnector(videos=[v_with, v_without])
    assert [e.native_id for e in _run(c.discover_entities({}))] == ["youtube:v1"]


def test_the_document_carries_the_pointer_kind_and_the_source_card():
    c = ShowNotesConnector(episodes=[_WITH])
    ent = _run(c.discover_entities({}))[0]
    docs = _run(c.list_documents(ent))
    f = docs[0].facets
    assert f["source_kind"] == "chapter_pointer"          # the tier is decided by the data
    assert f["audience"] == "hiring_team" and f["voice_role"] == "recruiter"
    assert f["published_at"] == "2026-09-01"
    body = _run(c.fetch_artifact(docs[0])).decode()
    assert "[00:12:05]" in body and "What a live screen should catch" in body


def test_a_guest_is_read_from_the_title_and_never_from_the_body():
    """A name in the notes is a MENTION; treating it as the guest lands a moment on the wrong person."""
    assert voices_doc.guest_from_title("How fake candidates get through, with Jane Roe") == "Jane Roe"
    mentioned = {**_WITH, "title": "A conversation about screening",
                 "summary": _WITH["summary"] + "\nWe discussed what Jane Roe wrote last week."}
    assert voices_doc.facets(mentioned).get("guest", "") == ""


def test_the_whole_voices_corpus_is_sealed_off_from_the_research_path():
    """Advice must never answer a question of fact. Both halves carry the seal: the chapter pointers
    because they are not speech, the essays because they are opinion about how to work, not evidence
    about a person or a company."""
    from roster_vertical.manifest import build_manifest
    ne = build_manifest().non_evidence_facets
    assert ne["source_kind"] == ("chapter_pointer",) and ne["voices"] == ("1",)
    # and every voices document actually carries the seal the manifest names
    assert voices_doc.facets(_WITH)["voices"] == "1"
    from roster_vertical.connectors.practitioner_essay import PractitionerEssayConnector
    f = PractitionerEssayConnector._facets({"title": "On screening", "link": "https://x.test/p"},
                                           "Hung Lee", "recruiter", "hiring_team", "Recruiting Brainfood")
    assert f["voices"] == "1" and f["audience"] == "hiring_team" and f["voice_role"] == "recruiter"
