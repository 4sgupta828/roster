"""The employer's INDUSTRY (owner, 2026-09-07: "why are fintech jobs not captured?"): a via key on the company entity,
curated lookup data, and a schema change that must not invalidate a single extracted row."""
from __future__ import annotations

import json
import pathlib
import re

from roster_vertical.facet_schema import COMPATIBLE_EXTRACTION_VERSIONS, FACET_SCHEMA, FACET_WEIGHTS, INDUSTRIES, extraction_is_current

DATA = pathlib.Path(__file__).parent / "data" / "company_industry.json"


def test_industry_is_a_via_key_on_the_company_read_by_jobs_and_people():
    k = FACET_SCHEMA.key("company_industry")
    assert k is not None and k.via == "company" and set(k.kinds) == {"person", "job"} and k.values == INDUSTRIES
    from api.facet_store import via_target_key
    assert via_target_key(k.key, k.via) == "industry"                       # reads the company's own `industry` facet
    # a SEARCH may set it; an EXTRACTION is never asked for it (nothing reads an industry off a posting or a profile)
    assert "company_industry" in FACET_SCHEMA.to_prompt_block("job", include_via=True)
    assert "company_industry" not in FACET_SCHEMA.to_prompt_block("job")
    assert FACET_WEIGHTS.prefer["company_industry"] > FACET_WEIGHTS.prefer["company_type"]     # it says more than a size class


def test_the_curated_map_only_uses_real_industries_and_never_contradicts_itself():
    d = json.loads(DATA.read_text())
    seen: dict[str, str] = {}
    for industry, slugs in d["industries"].items():
        assert industry in INDUSTRIES, industry
        for s in slugs:
            key = re.sub(r"[^a-z0-9]", "", s.lower())
            assert seen.get(key, industry) == industry, f"{s} is both {seen.get(key)} and {industry}"
            seen[key] = industry
    assert len(seen) > 400 and d.get("as_of")


def test_a_slug_matches_through_its_legal_suffix_and_an_unknown_employer_gets_nothing():
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "scripts"))
    from company_industry import core, industry_of, load_map
    table, _ = load_map()
    assert industry_of("Stripe", table) == "fintech" and industry_of("stripe", table) == "fintech"
    assert industry_of("Coupang, Inc.", table) == "ecommerce" and industry_of("HSBC HOLDINGS PLC", table) == "financial_services"
    assert industry_of("Pulse Biosciences, Inc.", table) == "medical_devices" and industry_of("anduril_industries", table) == "aerospace_defense"
    assert industry_of("a_company_nobody_curated", table) is None            # never a guess
    assert core("wpp plc") == "wpp" and core("mclane company inc") == "mclane"


def test_adding_a_via_key_does_not_invalidate_a_single_extracted_row():
    """`version()` hashes every key, so a via key changes the stamp although nothing about extraction changed. The
    corpus passes accept the listed predecessors — without this, one such change would re-read 281k people."""
    assert "77c3be380247" in COMPATIBLE_EXTRACTION_VERSIONS                  # the stamp on every row extracted so far
    assert extraction_is_current("77c3be380247") and extraction_is_current(FACET_SCHEMA.version())
    assert not extraction_is_current("deadbeef1234") and not extraction_is_current(None)


def test_every_via_key_has_a_real_key_to_read_on_the_entity_it_points_at():
    """`FacetSQLStore.project` silently drops a key the schema does not declare: `company_industry` existed while the
    company's own `industry` did not, so a lookup pass reported 1,342 matches and wrote 0 rows (prod, 2026-09-07)."""
    from api.facet_store import via_target_key
    for k in FACET_SCHEMA.keys:
        if not k.via:
            continue
        target = FACET_SCHEMA.key(via_target_key(k.key, k.via))
        assert target is not None, f"{k.key} reads {via_target_key(k.key, k.via)!r} on a {k.via}, which the schema does not declare"
        assert k.via in target.kinds, f"{target.key} is not a {k.via} key"
        assert set(target.values) == set(k.values), f"{k.key} and {target.key} must share a vocabulary"
