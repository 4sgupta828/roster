from roster_vertical.consultant import (FIELDS, FIELD_KEYS, JD_INTERVIEW, REQUIRED, consultant_prompt, contract_mapping, document_reader_prompt, fields_for,
                                        jd_assemble_prompt)
from roster_vertical.facet_schema import FACET_SCHEMA


def test_every_mapped_field_names_a_real_contract_key_and_a_legal_mode():
    for f in FIELDS:
        if f.contract:
            key, mode = f.contract
            assert FACET_SCHEMA.key(key) is not None, f.key
            assert mode in ("must", "prefer", "avoid", "center")
            if mode == "center":
                assert FACET_SCHEMA.key(key).type.value == "ordinal"
    for d in ("job", "candidate"):
        m = contract_mapping(d)
        assert "role_family" in m and "level" in m and all(FACET_SCHEMA.key(k) is not None for k, _ in m.values())
        assert all(r in FIELD_KEYS for r in REQUIRED[d])
    assert "evidence" in contract_mapping("candidate") and "evidence" not in contract_mapping("job")


def test_the_consultants_first_questions_are_mission_and_posture_and_the_prompts_carry_the_rules():
    hiring, seeker = consultant_prompt("candidate"), consultant_prompt("job")
    assert "mission" in hiring and "what fails if they are not hired" in hiring and "never title, years or a skills list first" in hiring
    assert "posture" in seeker and "step up, lateral" in seeker and "current salary" in seeker and "ONE question at most" in seeker
    assert [f.key for f in fields_for("candidate")][:2] == ["direction", "mission"] and [f.key for f in fields_for("job")][1] == "posture"
    assert JD_INTERVIEW[0] == "mission"
    assert "career_arc" in document_reader_prompt("resume", "job") and "span" in document_reader_prompt("jd", "candidate")
    assert "What you will own" in jd_assemble_prompt()
