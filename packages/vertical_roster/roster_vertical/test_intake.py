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


def test_jd_draft_vocabulary_and_role_key():
    from roster_vertical.intake import JD_PEER_SHARE, JD_PEERS, draft_prompt, group_prompt, improve_prompt, role_key
    assert 0.2 <= JD_PEER_SHARE <= 0.5 and JD_PEERS == 10
    assert "g<id>" in draft_prompt() and "you" in draft_prompt() and "never" in draft_prompt().lower()
    assert "[p<n>]" in group_prompt() and "peers" in group_prompt()
    assert "ORIGINAL" in improve_prompt("profile") and "ANSWERS" in improve_prompt("jd") and "Never add" in improve_prompt("jd")
    assert role_key("Backend Engineer, Payments", {"field": ["software"], "function": ["engineering"], "level": ["senior"]}) == "software/engineering/senior/backend-engineer-payments"
    assert role_key("", {}) == "_/_/_/role"
    assert role_key("ML Platform Lead", {"field": ["data_ml"], "level": ["leadership"]}) == "data_ml/_/leadership/ml-platform-lead"


def test_direction_prompt_treats_a_bare_title_list_as_ambiguous():
    from roster_vertical.intake import direction_prompt
    p = direction_prompt()
    assert "ambiguous" in p and "Never guess" in p and "hiring" in p and "looking" in p


def test_a_stated_leadership_tier_rules_out_ic_peers_but_an_unmarked_tier_keeps_every_work_type():
    from roster_vertical.intake import peer_work_types
    assert peer_work_types(["leadership"], ["ic"]) == []
    assert peer_work_types(["leadership"], ["ic", "executive"]) == ["executive"]
    assert peer_work_types(["senior"], ["ic"]) == ["ic"]
    assert peer_work_types([], ["ic", "manager"]) == ["ic", "manager"]
