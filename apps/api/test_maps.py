"""Map review security + hygiene — pure (no DB)."""
from api.maps import sanitize_text, sign_reviewer, verify_reviewer


def test_reviewer_tokens_are_server_signed_and_bound_to_map_key_and_name():
    t = sign_reviewer("map1", "key1", "Hiring Manager")
    assert verify_reviewer("map1", "key1", "Hiring Manager", t)
    assert not verify_reviewer("map2", "key1", "Hiring Manager", t)        # another map
    assert not verify_reviewer("map1", "key2", "Hiring Manager", t)        # a spoofed key
    assert not verify_reviewer("map1", "key1", "Someone Else", t)          # a renamed reviewer
    assert not verify_reviewer("map1", "key1", "Hiring Manager", "")       # no token


def test_reviewer_text_is_sanitized_at_the_boundary():
    assert sanitize_text("  Hiring <b>Manager</b>\x00\n  ", 80) == "Hiring bManager/b"
    assert sanitize_text("x" * 100, 10) == "x" * 10
    assert sanitize_text(None, 10) == ""


def test_review_state_is_derived_from_the_feedback_chips():
    from api.maps import derive_state
    assert derive_state(["more_like_this", "wrong_location"]) == "shortlist"
    assert derive_state(["less_like_this"]) == "not relevant"
    assert derive_state(["evidence_too_weak"]) == "needs more evidence"
    assert derive_state(["wrong_seniority"]) == "maybe"
    assert derive_state([]) == "unreviewed"
