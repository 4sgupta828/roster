"""The Voices mode's gates. Every test here is a way the mode could lie.

The load-bearing one is the pointer/quote split: a chapter title is written by a producer, so showing
it as a quotation would manufacture a statement nobody made.
"""
from api.voices.search import (VOICE_SOURCE_KEYS, build_query, dedupe, is_quotable, kind_of, moment,
                               register_of, terms, tsqueries)


# ── the query ────────────────────────────────────────────────────────────────────────────────────

def test_stopwords_and_domain_generics_are_dropped_so_the_real_words_rank():
    # "interview" is in a third of this corpus; OR-ed into the query it drowns "system"/"design"
    assert terms("how to prepare for a system design interview") == ["prepare", "system", "design"]


def test_a_question_made_only_of_generic_words_still_searches_for_something():
    """Dropping every word would leave nothing — the generics are all we have, so they are kept."""
    assert terms("hiring process") == ["hiring", "process"]
    assert terms("what should candidates do") == ["candidates"]
    # and when something specific survives, the generic word goes: nearly every block says "recruiters"
    assert terms("how do recruiters read a resume") == ["read", "resume"]


def test_the_ladder_asks_for_all_words_before_any_word():
    assert tsqueries(["fake", "resume", "screen"])[0] == ("all words", "fake & resume & screen")
    assert tsqueries(["fake", "resume", "screen"])[-1][0] == "any word"
    assert tsqueries([]) == []


# ── the SQL ──────────────────────────────────────────────────────────────────────────────────────

def test_only_the_voice_sources_are_searched():
    _, params = build_query(q="resume")
    assert params[0] == list(VOICE_SOURCE_KEYS) and "podcast" not in params[0]


def test_an_unrecognised_kind_narrows_to_nothing_never_widens_to_everything():
    """A typo'd filter that silently returns the whole corpus is a lie about what the filter did."""
    _, params = build_query(q="resume", kinds=("essayy",))
    assert params[0] == []


def test_the_audience_filter_keeps_material_written_for_both_readers():
    where, params = build_query(q="resume", audience="job_seeker")[0], build_query(q="resume", audience="job_seeker")[1]
    assert "'both'" in where and "job_seeker" in params
    # an unknown audience is ignored rather than emptying the page
    assert "audience" not in build_query(q="resume", audience="martians")[0]


def test_rank_is_length_normalised_so_a_short_chapter_can_beat_a_long_essay():
    """Without normalisation a 4,000-character essay mentioning a word twice outranks a chapter
    titled exactly that word, and the moments this mode exists to surface never appear."""
    sql, _ = build_query(q="layoffs")
    assert "ts_rank(tsv, to_tsquery('english', $2), 1)" in sql


def test_boilerplate_and_document_furniture_never_reach_a_card():
    sql, _ = build_query(q="resume")
    assert "NOT (facets ? 'boilerplate')" in sql
    assert "text NOT LIKE 'URL: %'" in sql
    assert "source_key NOT IN ('show_notes','youtube_chapters') OR text LIKE '[%'" in sql


def test_a_browse_orders_by_the_pieces_own_date_not_by_when_we_ingested_it():
    sql, _ = build_query(q="")
    assert sql.index("facets->>'published_at' DESC") < sql.index("created_at DESC")


# ── the cards, and the gate that matters ─────────────────────────────────────────────────────────

def _chapter_row(text="[00:12:05] What a screen should catch", **facets):
    f = {"source_kind": "chapter_pointer", "episode_url": "https://show.test/ep1",
         "publication": "Recruiting is No Joke", "guest": "Jane Roe", "published_at": "2026-09-01"}
    f.update(facets)
    return {"document_id": "d1", "block_id": "b1", "text": text, "document_title": "Ep 12",
            "source_key": "show_notes", "facets": f}


def test_a_chapter_is_never_quotable_and_says_so_in_its_register():
    m = moment(_chapter_row())
    assert m["quotable"] is False
    assert "written by the publisher" in m["register"] and "listen from this point" in m["register"]
    assert m["kind"] == "podcast"


def test_a_chapter_deep_links_to_its_own_moment():
    m = moment(_chapter_row())
    assert m["t_start"] == 725 and m["url"] == "https://show.test/ep1?t=725"
    assert m["text"] == "What a screen should catch"        # the timestamp is not part of the words


def test_a_video_chapter_is_a_video_and_embeds_at_the_second():
    m = moment({**_chapter_row(video_id="abc123"), "source_key": "youtube_chapters"})
    assert m["kind"] == "video" and m["media"] == {"kind": "youtube", "id": "abc123", "t": 725}
    assert "watch from this point" in m["register"] and m["quotable"] is False


def test_an_essay_is_quotable_but_its_register_asserts_only_what_the_author_argues():
    """The whole discipline of the mode: this corpus is opinion, and the card has to say so."""
    m = moment({"document_id": "d2", "block_id": "b2", "source_key": "practitioner_essay",
                "text": "Most screens measure recall, not judgement.", "document_title": "On screening",
                "facets": {"source_kind": "essay", "author": "Hung Lee", "voice_role": "recruiter",
                           "url": "https://x.test/p", "audience": "hiring_team"}})
    assert m["quotable"] is True and m["kind"] == "essay"
    assert m["register"] == "Advice from a recruiter — what they argue, not a verified outcome"
    assert "verified" in m["register"] and "recommend" not in m["register"].lower()


def test_the_register_never_claims_a_practitioner_is_right():
    for role in ("recruiter", "coach", "engineer", "vendor", "", "anything-else"):
        assert register_of("essay", role).endswith("what they argue, not a verified outcome")


def test_quotability_is_decided_by_the_stamped_kind_not_by_the_source_name():
    assert is_quotable("chapter_pointer") is False
    assert is_quotable("essay") is True
    assert kind_of("chapter_pointer", "anything") == "podcast"


def test_a_pointer_with_no_link_is_still_a_card_and_never_invents_a_url():
    m = moment(_chapter_row(episode_url=""))
    assert m["url"] == "" and m["t_start"] == 725


# ── crowding ─────────────────────────────────────────────────────────────────────────────────────

def _m(i, doc, show, text):
    return {"id": f"{doc}::{i}", "text": text, "show": show, "speaker": ""}


def test_one_piece_cannot_hold_the_whole_page():
    ms = [_m(i, "d1", "A", f"passage {i}") for i in range(5)]
    assert len(dedupe(ms, per_document=2)) == 2


def test_identical_text_is_shown_once():
    ms = [_m(0, "d1", "A", "same words"), _m(0, "d2", "B", "same  WORDS ")]
    assert len(dedupe(ms)) == 1


def test_one_voice_is_held_back_but_not_thrown_away_when_the_page_would_be_thin():
    """A prolific publisher must not own the page; but it is better than an empty page."""
    ms = [_m(i, f"d{i}", "A", f"t{i}") for i in range(6)]
    out = dedupe(ms, per_source=3, limit=10)
    assert len(out) == 6 and [m["id"] for m in out[:3]] == ["d0::0", "d1::1", "d2::2"]


# ── the semantic leg and the fusion ──────────────────────────────────────────────────────────────

def test_both_legs_search_the_same_corpus_under_the_same_filters():
    """A filter honoured by one leg and not the other is a lie either way, so the WHERE is written
    once. This pins that they stay in step."""
    from api.voices.search import build_vector_query
    kw, kwp = build_query(q="resume", kinds=("essay",), audience="job_seeker", speaker="Hung")
    vec, vp = build_vector_query(qvec="[0.1]", kinds=("essay",), audience="job_seeker", speaker="Hung")
    for clause in ("NOT (facets ? 'boilerplate')", "text NOT LIKE 'URL: %'", "'both'", "ILIKE"):
        assert clause in kw and clause in vec
    assert kwp[0] == vp[0] == ["practitioner_essay"]        # same slice of the corpus
    assert "embedding IS NOT NULL" in vec and "embedding" not in kw


def test_the_semantic_leg_orders_by_distance_and_never_invents_a_vector():
    from api.voices.search import build_vector_query
    sql, params = build_vector_query(qvec="[0.1,0.2]", limit=5)
    assert "(embedding <=> $2::vector)" in sql and "ORDER BY (embedding <=> $2::vector) ASC" in sql
    assert params[1] == "[0.1,0.2]" and params[-1] == 5


def test_rrf_puts_what_both_legs_found_above_what_only_one_did():
    """Not a weighted blend: ts_rank and cosine distance live on different scales, and mixing them is
    a guess dressed up as a number. RRF reads POSITION, which both legs agree on the meaning of."""
    from api.voices.search import fuse
    kw = [{"document_id": "d", "block_id": "kw_only"}, {"document_id": "d", "block_id": "both"}]
    vec = [{"document_id": "d", "block_id": "both"}, {"document_id": "d", "block_id": "vec_only"}]
    assert [r["block_id"] for r in fuse(kw, vec)] == ["both", "kw_only", "vec_only"]


def test_fusion_keeps_the_keyword_legs_highlighted_snippet():
    """The vector leg returns the block's opening; the keyword leg returns the passage that matched.
    A fused row must keep the one a reader learns from."""
    from api.voices.search import fuse
    kw = [{"document_id": "d", "block_id": "1", "snippet": "…the «resume» screen…"}]
    vec = [{"document_id": "d", "block_id": "1", "snippet": "the opening of the piece"}]
    assert fuse(vec, kw)[0]["snippet"] == "…the «resume» screen…"


def test_one_leg_alone_still_answers():
    from api.voices.search import fuse
    only = [{"document_id": "d", "block_id": "1"}]
    assert len(fuse(only, [])) == 1 and len(fuse([], only)) == 1 and fuse([], []) == []


def test_the_generic_words_are_kept_once_meaning_is_available():
    """Stripping them compensates for having no semantics. With a vector leg the words are literal —
    which is what stops "fake candidates" matching a block that says "fake" and no candidate."""
    assert terms("fake candidates") == ["fake"]                                  # keyword-only
    assert terms("fake candidates", keep_generic=True) == ["fake", "candidates"]  # hybrid
    assert tsqueries(terms("fake candidates", keep_generic=True))[0] == ("all words", "fake & candidates")


def test_the_role_strip_has_a_floor_and_would_rather_show_less():
    """A strip is an aside on someone else's card: three weak rows are worse than one good one, so it
    abstains rather than pads. The floor is measured, not chosen — see MIN_ROLE_SCORE."""
    from api.voices.routes import MIN_ROLE_SCORE
    assert 0.3 < MIN_ROLE_SCORE < 0.5
    rows = [{"document_id": "d1", "block_id": "1", "score": 0.52, "text": "[00:12:38] Coding interview",
             "source_key": "youtube_chapters", "facets": {"source_kind": "chapter_pointer"}},
            {"document_id": "d2", "block_id": "1", "score": 0.31, "text": "[00:02:00] Economics of software",
             "source_key": "youtube_chapters", "facets": {"source_kind": "chapter_pointer"}}]
    kept = [r for r in rows if float(r.get("score") or 0) >= MIN_ROLE_SCORE]
    assert [r["document_id"] for r in kept] == ["d1"]
