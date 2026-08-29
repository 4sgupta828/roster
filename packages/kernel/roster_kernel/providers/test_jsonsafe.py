"""Tests for lenient LLM-JSON parsing — the invalid-\\escape crash repair."""
import json

import pytest

from roster_kernel.providers._jsonsafe import loads, repair_escapes


def test_valid_json_untouched():
    assert loads('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_markdown_escape_underscore_repaired():
    # the real failure class: a markdown-escaped underscore inside a string value
    bad = '{"answer": "use \\_dev\\_ tools and vertical\\_agents"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)                       # strict rejects it
    assert loads(bad)["answer"] == "use \\_dev\\_ tools and vertical\\_agents"


def test_latex_backslash_repaired():
    # \g and \D are INVALID JSON escapes (unlike \f/\t/\n which strict JSON accepts) → they crash strict.
    bad = '{"note": "scales \\gamma and \\Delta over time"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(bad)
    assert loads(bad)["note"] == "scales \\gamma and \\Delta over time"


def test_valid_escapes_preserved():
    good = '{"s": "line1\\nline2\\ttab \\"quoted\\" \\u00e9"}'
    assert loads(good)["s"] == 'line1\nline2\ttab "quoted" \u00e9'


def test_real_escaped_backslash_pair_preserved():
    # a legit escaped backslash (Windows path) must survive unchanged
    good = '{"p": "C:\\\\Users\\\\x"}'
    assert loads(good)["p"] == "C:\\Users\\x"


def test_trailing_lone_backslash_doubled():
    assert repair_escapes("path\\") == "path\\\\"   # a lone trailing backslash → doubled (literal)


def test_non_escape_syntax_error_still_raises():
    with pytest.raises(json.JSONDecodeError):
        loads('{"a": }')                       # genuine malformation is NOT hidden


def test_repair_is_idempotent_on_clean_input():
    s = '{"a":"no backslashes here"}'
    assert repair_escapes(s) == s
