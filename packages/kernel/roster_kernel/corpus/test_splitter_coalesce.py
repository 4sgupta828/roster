"""Splitter target_chars coalescing: full-text docs otherwise fragment into hundreds of tiny
paragraph blocks that scatter context. With target_chars>0, consecutive SAME-section paragraphs merge
into ~target-sized blocks (never crossing a heading, never exceeding max_chars). target_chars=0 is
byte-identical to the per-paragraph default."""
from __future__ import annotations

from roster_kernel.corpus.splitter import MAX_BLOCK_CHARS, split

_DOC = ("# Introduction\n\n" + ("A short intro sentence about the method. " * 3 + "\n\n") * 12
        + "# Results\n\n" + ("A results paragraph with numbers 12.3 and 45.6. " * 3 + "\n\n") * 12)


def test_default_is_per_paragraph_unchanged():
    b = split("d", _DOC, min_chars=1)                       # no target_chars
    assert len(b) == 24                                     # 12 + 12 paragraphs, one block each


def test_coalesce_merges_within_section_up_to_target():
    b = split("d", _DOC, min_chars=1, target_chars=1800)
    assert len(b) < 24                                      # fragments merged
    assert all(len(x.text) <= MAX_BLOCK_CHARS for x in b)   # never exceed the hard cap
    # every merged block stays within ONE section (no heading crossed)
    secs = [x.section_path for x in b]
    assert ("Introduction",) in secs and ("Results",) in secs
    for x in b:
        assert x.section_path in (("Introduction",), ("Results",))
    # blocks are near the target, not tiny fragments
    assert max(len(x.text) for x in b) > 700


def test_section_boundary_forces_a_flush():
    # even if an intro block is under target, the Results heading starts a NEW block (topic coherence)
    b = split("d", _DOC, min_chars=1, target_chars=100000)  # target huge → only section boundary flushes
    assert len({x.section_path for x in b}) == 2            # exactly one block per section
    assert len(b) == 2


def test_coalesced_block_text_contains_its_paragraphs():
    # provenance: a quote from any merged paragraph must be a substring of its block (span gate holds)
    b = split("d", _DOC, min_chars=1, target_chars=1800)
    joined = "\n".join(x.text for x in b)
    assert "results paragraph with numbers 12.3 and 45.6" in joined
