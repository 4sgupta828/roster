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
