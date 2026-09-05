"""The intake VOCABULARY (vertical): directions, artifacts, completeness checklists, required / optional keys,
question words. Every key it names must exist in the schema for the direction's search kind."""
from roster_vertical.facet_schema import FACET_SCHEMA
from roster_vertical.intake import (ARTIFACT_FOR, CHECKLIST, DIRECTIONS, OPTIONAL_KEYS, QUESTION_WORDS, REQUIRED_KEYS, SEARCH_KIND,
                                    completeness_prompt, option_label, question_for, required_items, turn_prompt, understood_words)


def test_directions_artifacts_and_kinds_line_up():
    assert DIRECTIONS == ("job", "candidate")
    assert ARTIFACT_FOR == {"job": "profile", "candidate": "jd"} and SEARCH_KIND == {"job": "job", "candidate": "person"}


def test_every_checklist_key_and_required_key_exists_in_the_schema_for_the_kind():
    for direction in DIRECTIONS:
        kind = SEARCH_KIND[direction]
        for item in CHECKLIST[ARTIFACT_FOR[direction]]:
            if item.key:
                k = FACET_SCHEMA.key(item.key)
                assert k is not None and kind in k.kinds, (direction, item.name, item.key)
                assert item.mode in ("must", "prefer")
        for key in REQUIRED_KEYS[direction] + OPTIONAL_KEYS[direction]:
            k = FACET_SCHEMA.key(key)
            assert k is not None and kind in k.kinds and k.navigable, (direction, key)
        assert not set(REQUIRED_KEYS[direction]) & set(OPTIONAL_KEYS[direction])


def test_required_items_cover_the_owners_list():
    prof = required_items("profile")
    assert {"current_role", "level", "years", "field", "skills", "location", "comp"} <= set(prof)
    jd = required_items("jd")
    assert {"title", "level", "location", "must_skills", "responsibilities", "comp"} <= set(jd)
    assert "team" not in jd and "authorization" not in prof            # optional items are not required


def test_every_item_and_key_has_question_words_and_options_are_labeled():
    for art, items in CHECKLIST.items():
        for it in items:
            q = question_for("item", it.name, art)
            assert q and q.endswith("?"), (art, it.name)
    for direction in DIRECTIONS:
        for key in REQUIRED_KEYS[direction] + OPTIONAL_KEYS[direction]:
            assert question_for("key", key, direction).endswith("?"), (direction, key)
    assert option_label("level", "staff_plus") == "staff+" and option_label("comp", "150k_200k") == "$150k–200k"
    assert option_label("metro", "bay_area") == "bay area"
    assert QUESTION_WORDS["comp"]["decline"] == "Prefer not to say"


def test_prompts_name_only_schema_keys_and_checklist_items():
    p = completeness_prompt("profile")
    for it in CHECKLIST["profile"]:
        assert it.name in p
    assert "present" in p and "missing" in p and "weak" in p
    t = turn_prompt("job")
    assert "answers" in t and "free_text" in t and "direction" in t
    for key in REQUIRED_KEYS["job"]:
        assert key in t


def test_understood_words_read_the_contract_in_plain_words():
    c = {"kind": "job", "text": "ml infra roles", "must": {"work_mode": ["remote"], "country": ["us"]}, "prefer": {"level": ["staff_plus"], "field": ["data_ml"]},
         "center": {"key": "level", "value": "senior", "span": 1}, "avoid": {}, "rank_by": "match", "scope": {}, "exclude_ids": [], "limit": 60, "angles": []}
    w = understood_words("job", c, {"comp": "150k_200k", "years": "6"})
    assert "remote" in w and "US" in w and "staff+" in w and "data / ML" in w and "senior" in w and "$150k–200k" in w
    assert w.endswith(".")
