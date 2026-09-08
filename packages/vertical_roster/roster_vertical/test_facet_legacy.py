"""The legacy people vocabulary → schema table (spec §8 step 4 bridge): one lookup table serves BOTH the
projection of stored rows and the translation of a compiled legacy filter, so a Talent rail's must-slice and the
old engine's filter agree by construction (the normalization-alignment invariant)."""
from roster_vertical.facet_legacy import (LEGACY_PERSON_MAP, PASS_THROUGH_KEYS, legacy_facets_to_contract,
                                          legacy_person_pairs)
from roster_vertical.facet_schema import FACET_SCHEMA


def test_every_target_in_the_table_is_a_legal_schema_value_for_people():
    for (old_key, old_value), targets in LEGACY_PERSON_MAP.items():
        assert old_key and old_value
        for new_key, new_value in targets.items():
            k = FACET_SCHEMA.key(new_key)
            assert k is not None and "person" in k.kinds, (old_key, old_value, new_key)
            assert FACET_SCHEMA.validate_value(new_key, new_value) == new_value, (old_key, old_value, new_key, new_value)
    for key in PASS_THROUGH_KEYS:
        assert FACET_SCHEMA.key(key) is not None and "person" in FACET_SCHEMA.key(key).kinds


def test_the_projection_pairs_are_flat_and_cover_the_obvious_readings():
    pairs = legacy_person_pairs()
    assert ("seniority", "c_level", "level", "leadership") in pairs
    assert ("seniority", "c_level", "work_type", "executive") in pairs
    assert ("seniority", "principal", "level", "staff_plus") in pairs
    assert ("seniority", "engineering_manager", "work_type", "manager") in pairs
    assert not any(p[:2] == ("seniority", "engineering_manager") and p[2] == "level" for p in pairs)   # a level is never invented
    assert ("role", "physician", "field", "clinical_pharma") in pairs
    assert ("role", "software_engineer", "field", "software") in pairs
    assert ("function", "machine_learning", "field", "data_ml") in pairs
    assert ("function", "chemistry", "field", "research") in pairs
    assert ("role", "founder", "work_type", "founder") in pairs
    assert ("seniority", "founder", "level", "leadership") in pairs
    assert len(pairs) == len(set(pairs))


def test_a_legacy_filter_becomes_a_sound_contract():
    # every chosen value maps to level=leadership and work_type=executive → both are musts
    c = legacy_facets_to_contract({"seniority": ["c_level", "vp", "director"], "metro": ["bay_area"], "country": ["us"]})
    assert c["must"]["level"] == ["leadership"] and c["must"]["work_type"] == ["executive"]
    assert c["must"]["metro"] == ["bay_area"] and c["must"]["country"] == ["us"]
    # an OR over values that disagree on a key: only the keys EVERY value maps to can be musts; the rest rank
    c2 = legacy_facets_to_contract({"seniority": ["c_level", "engineering_manager"]})
    assert "level" not in c2["must"]
    assert sorted(c2["must"]["work_type"]) == ["executive", "manager"]
    assert c2["prefer"].get("level") == ["leadership"]
    # unknown legacy tokens translate to nothing (never a guess); industry has no schema key yet
    c3 = legacy_facets_to_contract({"seniority": ["astronaut"], "industry": ["payments"], "worked_at": ["google"]})
    assert c3["must"] == {} and c3["prefer"] == {}
    # role + function both name a field → the musts union within the key
    c4 = legacy_facets_to_contract({"role": ["software_engineer"], "function": ["machine_learning"]})
    assert sorted(c4["must"]["field"]) == ["data_ml", "software"]
    # company / skill pass through as sets (slugs untouched)
    c5 = legacy_facets_to_contract({"company": ["openai", "Deep Mind"], "skill": ["python"]})
    assert c5["must"]["company"] == ["openai", "deep_mind"] and c5["must"]["skill"] == ["python"]


def test_the_engines_hard_soft_split_is_respected_by_the_rail():
    from roster_vertical.facet_legacy import legacy_brief_to_contract
    hard = {"country": ["us"], "metro": ["bay area"]}
    soft = {"function": ["machine learning"], "seniority": ["engineering manager"], "stage": ["startup"]}
    c = legacy_brief_to_contract(hard, soft)
    assert c["must"] == {"country": ["us"], "metro": ["bay_area"]}           # what the engine filtered
    assert c["prefer"] == {"field": ["data_ml"], "work_type": ["manager"]}   # what it only ranked; stage has no person key
    # no brief contract → the compiled filter is read as all-hard
    assert legacy_brief_to_contract({}, {}, {"seniority": ["c_level"]})["must"]["level"] == ["leadership"]
