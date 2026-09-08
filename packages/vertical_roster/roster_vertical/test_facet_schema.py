"""The roster facet schema is complete, shared across kinds, and reaches the kernel through the manifest."""
from roster_kernel.facets import UNKNOWN, Contract, FacetType, validate_contract

from roster_vertical.facet_schema import (COMPANY_TYPES, FACET_SCHEMA, FACET_WEIGHTS, LEVELS, SCHEMA_VERSION, VALUE_LABELS)


def test_every_extracted_key_has_a_label_kinds_and_guidance():
    for k in FACET_SCHEMA.keys:
        assert k.label and k.kinds, k.key
        assert k.guidance, f"{k.key} needs one line of guidance for the extractor"
        if k.type in (FacetType.categorical, FacetType.ordinal):
            assert k.values and all(v == v.lower() and " " not in v for v in k.values), k.key
        if k.type is FacetType.numeric:
            assert k.bands and k.unit, k.key


def test_shared_keys_share_one_vocabulary_across_person_and_job():
    for key in ("field", "function", "level", "work_type"):
        k = FACET_SCHEMA.key(key)
        assert set(k.kinds) >= {"person", "job"}
    assert FACET_SCHEMA.key("level").values == LEVELS
    assert FACET_SCHEMA.ordinal_distance("level", "intern", "leadership") == 5
    assert FACET_SCHEMA.key("company_type").via == "company" and FACET_SCHEMA.key("type").values == COMPANY_TYPES


def test_numeric_bands_cover_the_line_and_derived_keys_are_marked():
    s = FACET_SCHEMA
    assert s.band_of("comp", 95_000) == "under_100k" and s.band_of("comp", 150_000) == "150k_200k" and s.band_of("comp", 1_000_000) == "300k_plus"
    assert s.band_of("comp", None) == UNKNOWN
    assert s.band_of("posted", 0) == "week" and s.band_of("posted", 7) == "month" and s.band_of("posted", 45) == "older"
    assert s.band_of("years", 2) == "0_2" and s.band_of("years", 16) == "16_plus"
    assert "not extracted" in s.key("posted").guidance and "not extracted" in s.key("evidence").guidance


def test_prompt_block_shows_vocabularies_and_hides_via_keys():
    job = FACET_SCHEMA.to_prompt_block("job")
    assert "intern < junior < mid < senior < staff_plus < leadership" in job
    assert "comp (numeric" in job and "never estimate" in job
    assert "company_type" not in job and "company_stage" not in job         # read through the company, not extracted
    person = FACET_SCHEMA.to_prompt_block("person")
    assert "years (numeric" in person and "work_mode" not in person


def test_weights_cover_rankable_keys_and_version_is_stable():
    assert FACET_WEIGHTS.avoid["field"] == 0.30 and FACET_WEIGHTS.prefer["field"] == 0.25 and FACET_WEIGHTS.prefer["skill"] == 0.06   # field is the strongest signal both ways
    assert SCHEMA_VERSION == FACET_SCHEMA.version() and len(SCHEMA_VERSION) == 12
    assert VALUE_LABELS["staff_plus"] == "staff+"


def test_a_realistic_contract_validates_and_a_wrong_one_is_named():
    c = Contract(kind="job", text="founder cto", must={"level": ["leadership"], "work_mode": ["remote", "hybrid"], "comp": {"min": 200000}},
                 prefer={"company_type": ["startup"]}, avoid={"field": ["sales"]}, center={"key": "level", "value": "leadership", "span": 1}, rank_by="comp")
    assert validate_contract(c, FACET_SCHEMA) == []
    bad = Contract(kind="person", must={"work_mode": ["remote"], "level": ["principal"]})
    errs = validate_contract(bad, FACET_SCHEMA)
    assert any("work_mode" in e for e in errs) and any("principal" in e for e in errs)


def test_manifest_exposes_the_schema():
    from roster_vertical.manifest import build_manifest
    m = build_manifest()
    assert m.extraction_schema is FACET_SCHEMA


# ── place names ───────────────────────────────────────────────────────────────────────────────────

def test_country_and_state_labels_are_namespaced_because_the_codes_collide():
    """"in" is India AND Indiana; "de" is Germany AND Delaware. One shared table would rename one of
    them wrongly wherever it appeared, so the labels are keyed by the facet they belong to."""
    from roster_vertical.facet_schema import KEYED_VALUE_LABELS
    assert KEYED_VALUE_LABELS["country"]["in"] == "India"
    assert KEYED_VALUE_LABELS["state"]["in"] == "Indiana"
    assert KEYED_VALUE_LABELS["country"]["de"] == "Germany"
    assert KEYED_VALUE_LABELS["state"]["de"] == "Delaware"


def test_both_spellings_of_the_united_kingdom_read_the_same():
    """The index carries `uk` and `gb`; side by side on a rail they look like a bug, not two codes."""
    from roster_vertical.facet_schema import KEYED_VALUE_LABELS
    c = KEYED_VALUE_LABELS["country"]
    assert c["uk"] == c["gb"] == "United Kingdom"


def test_every_us_state_the_index_ranks_by_has_a_name():
    from roster_vertical.facet_schema import KEYED_VALUE_LABELS
    st = KEYED_VALUE_LABELS["state"]
    for code in ("ca", "ny", "tx", "wa", "ma", "il", "fl", "va", "oh", "az", "nc", "ga", "dc", "pr"):
        assert st.get(code), code
    assert len(st) >= 51                                   # 50 states + DC
