"""Engine + adapter tests (docs/specs/facet-contract-evaluator.md §9 — app): normalization is code-owned and
explicit, extraction validates against the schema, structured beats text, compile can only narrow legally,
projection is idempotent, and the SQL adapter builds one must clause per key."""
from __future__ import annotations

import asyncio

from roster_kernel.facets import UNKNOWN, Contract, validate_contract
from roster_vertical.facet_schema import FACET_SCHEMA, SCHEMA_VERSION

from api.facet_store import FacetSQLStore, legacy_job_envelope, structural_job_facets, via_target_key
from api.facets_engine import (compile_contract, extract_envelopes, job_extraction_item, normalize_comp, pay_figures_present)


def test_comp_normalization_is_explicit_and_never_estimates():
    y = normalize_comp({"min": 150000, "max": 200000, "currency": "USD", "period": "year"})
    assert y["number"] == 175000 and not y["undisclosed"] and "150,000" in y["display"]
    h = normalize_comp({"min": 25, "max": 25, "currency": "USD", "period": "hour"})
    assert h["number"] == 25 * 2080 and h["assumption"] == "hourly × 2080"
    m = normalize_comp({"min": 10000, "currency": "USD", "period": "month"})
    assert m["number"] == 120000 and m["assumption"] == "monthly × 12"
    assert normalize_comp({"min": 50, "currency": "USD", "period": "hour"}, employment_type="contract")["undisclosed"]   # never annualized
    assert normalize_comp({"min": 100000, "max": 120000, "currency": "EUR", "period": "year"})["undisclosed"]          # not USD → no band
    assert normalize_comp("competitive")["undisclosed"] and normalize_comp("")is None and normalize_comp(None) is None
    assert normalize_comp({"display": "DOE"})["undisclosed"]
    assert FACET_SCHEMA.band_of("comp", y["number"]) == "150k_200k"


def test_extraction_validates_values_and_structured_fields_win():
    calls = []

    def fake_llm(system, user):
        calls.append(system)
        return {"items": [{"i": 0, "field": "Mechanical_Civil_Electrical", "function": "engineering", "level": "leadership", "work_type": "executive",
                           "work_mode": "onsite", "employment_type": "full_time", "specialty": ["turbomachinery", "Turbine Design"],
                           "comp": {"min": 180000, "max": 220000, "currency": "USD", "period": "year"}, "skill": ["ansys", "cad"], "metro": "seattle", "country": "us"},
                          {"i": 1, "field": "pharmacology", "level": "principal", "work_mode": "remote", "comp": "competitive"}]}
    items = [{"title": "Director of Turbomachinery", "company": "endurance", "head": "...", "tail": "Base pay $180,000–$220,000", "structured": {"work_mode": "hybrid"}},
             {"title": "Scientist", "company": "kiniksa", "head": "...", "tail": ""}]
    env = extract_envelopes("job", items, FACET_SCHEMA, fake_llm)
    assert len(env) == 2 and env[0]["schema_version"] == SCHEMA_VERSION
    f0 = env[0]["facets"]
    assert f0["field"][0]["value"] == "mechanical_civil_electrical" and f0["level"][0]["value"] == "leadership"
    assert f0["work_mode"] == [{"value": "hybrid", "confidence": 1.0, "provenance": "ats_field"}]       # structured beat the model's 'onsite'
    assert f0["comp"][0]["number"] == 200000 and f0["specialty"][0]["value"] == "turbomachinery" and f0["specialty"][1]["value"] == "turbine design"
    f1 = env[1]["facets"]
    assert "field" not in f1 and "level" not in f1                     # off-vocabulary → absent → counted as unknown
    assert f1["work_mode"][0]["value"] == "remote" and f1["comp"][0]["value"] == UNKNOWN and f1["comp"][0]["display"] == "competitive"
    assert "intern < junior < mid < senior < staff_plus < leadership" in calls[0] and "company_type" not in calls[0]
    assert extract_envelopes("job", items, FACET_SCHEMA, lambda s, u: (_ for _ in ()).throw(RuntimeError("down"))) == []


def test_compile_only_narrows_legally():
    def fake_llm(system, user):
        return {"must": {"level": ["leadership"], "work_mode": ["remote", "moon"], "nope": ["x"]}, "prefer": {"company_type": ["startup"], "comp": {"min": 200000}},
                "avoid": {"field": ["sales"]}, "center": {"key": "level", "value": "leadership", "span": 1}, "angles": ["founding cto", "head of engineering"], "intent": "..."}
    c = compile_contract("job", "Founder CTO at an early startup, remote, $200k+", FACET_SCHEMA, fake_llm)
    assert c.must == {"level": ["leadership"], "work_mode": ["remote"]} and "nope" not in c.must
    assert c.prefer == {"company_type": ["startup"], "comp": {"min": 200000.0}} and c.avoid == {"field": ["sales"]}
    assert c.center == {"key": "level", "value": "leadership", "span": 1} and c.angles == ["founding cto", "head of engineering"]
    assert validate_contract(c, FACET_SCHEMA) == []
    bad = compile_contract("person", "x", FACET_SCHEMA, lambda s, u: {"center": {"key": "field", "value": "software"}, "must": {"work_mode": ["remote"]}})
    assert bad.center is None and bad.must == {}
    down = compile_contract("job", "senior backend", FACET_SCHEMA, lambda s, u: (_ for _ in ()).throw(RuntimeError()))
    assert down.text == "senior backend" and down.must == {}


def test_legacy_and_structural_envelopes_and_extraction_item():
    e = legacy_job_envelope({"field": "software", "level": "unknown", "role_family": "software engineer"}, schema_version="v")
    assert "level" not in e["facets"] and e["facets"]["field"][0]["value"] == "software" and e["facets"]["role_family"][0]["provenance"] == "posting"
    s = structural_job_facets({"company": "Scale AI", "skills": ["python", "kubernetes"]}, schema_version="v", now_days=3)
    assert s["facets"]["company"][0]["value"] == "scale_ai" and s["facets"]["posted"][0]["number"] == 3.0 and len(s["facets"]["skill"]) == 2
    it = job_extraction_item({"title": "T", "body": "A" * 1000 + "Base pay $200,000", "work_mode": "remote"})
    assert it["tail"].endswith("$200,000") and it["structured"] == {"work_mode": "remote"} and len(it["head"]) == 400
    assert pay_figures_present("Base pay: $180,000 - $220,000") and pay_figures_present("$150k") and not pay_figures_present("competitive pay")
    assert via_target_key("company_type", "company") == "type" and via_target_key("company_stage", "company") == "stage"


def test_sql_adapter_builds_one_must_clause_per_key_and_via_joins():
    store = FacetSQLStore(lambda: None, FACET_SCHEMA)
    args: list = []
    cl = store._must_sql("('job:' || j.id::text)", {"level": ["senior", "staff_plus"], "comp": {"min": 200000}, "company_type": ["startup"], "skill": ["kubernetes"]}, args)
    assert len(cl) == 4 and all("EXISTS" in c for c in cl)
    assert any("numeric_value >=" in c for c in cl) and any("c.entity_kind = 'company'" in c for c in cl)
    assert ["senior", "staff_plus"] in args and 200000.0 in args and "type" in args
    assert store._must_sql("e.entity_id", {"nope": ["x"]}, []) == ["FALSE"]


def test_in_memory_and_sql_semantics_agree_on_the_reference_rows():
    """The kernel's matches_must is the law; the SQL adapter is checked against the same rows on prod by
    scripts/facets_parity.py — here we pin that the reference store handles the roster schema."""
    from roster_kernel.facets import InMemoryFacetStore, evaluate, FacetWeights
    rows = [{"id": "j1", "kind": "job", "sim": 0.6, "facets": {"level": ["leadership"], "field": ["software"], "company": ["acme"], "company_type": ["startup"]}},
            {"id": "j2", "kind": "job", "sim": 0.59, "facets": {"level": ["leadership"], "field": ["sales"], "company": ["big"]}},
            {"id": "j3", "kind": "job", "sim": 0.58, "facets": {"level": ["senior"], "field": ["software"], "comp": ["200k_300k"]}, "numeric": {"comp": 250000.0}}]
    c = Contract(kind="job", text="founder cto", must={"level": ["leadership"]}, avoid={"field": ["sales"]}, prefer={"company_type": ["startup"]})
    out = asyncio.new_event_loop().run_until_complete(evaluate(c, InMemoryFacetStore(rows, FACET_SCHEMA), FACET_SCHEMA, FacetWeights(), noise_floor=0.4))
    assert [r["id"] for r in out["rows"]] == ["j1", "j2"] and out["counts"]["field"] == {"software": 1, "sales": 1}
    assert out["counts"]["company_type"] == {"startup": 1, UNKNOWN: 1} and out["counts"]["comp"] == {UNKNOWN: 2}


def test_counts_query_parameters_line_up(monkeypatch):
    """Every $n in the counts SQL must be backed by an argument (prod 2026-09-05: 'could not determine data
    type of parameter $4' — the via-key query referenced params it never passed)."""
    import re as _re

    class _Conn:
        def __init__(self): self.calls = []
        async def fetchval(self, sql, *args): self.calls.append((sql, args)); return 0
        async def fetch(self, sql, *args): self.calls.append((sql, args)); return []
        async def execute(self, sql, *args): return None

    class _Acq:
        def __init__(self, c): self.c = c
        async def __aenter__(self): return self.c
        async def __aexit__(self, *a): return False

    class _Pool:
        def __init__(self): self.c = _Conn()
        def acquire(self): return _Acq(self.c)

    pool = _Pool()

    async def getter(): return pool
    store = FacetSQLStore(getter, FACET_SCHEMA); store._ready = True
    asyncio.new_event_loop().run_until_complete(store.counts("job", {"level": ["leadership"], "company_type": ["startup"]}, FACET_SCHEMA))
    assert pool.c.calls
    for sql, args in pool.c.calls:
        refs = {int(m) for m in _re.findall(r"\$(\d+)", sql)}
        assert refs and max(refs) == len(args) and refs == set(range(1, len(args) + 1)), (sorted(refs), len(args), sql[:120])


def test_compile_never_promises_open_phrases_and_uncovered_keys_downgrade_to_prefer():
    from api.facets_engine import downgrade_uncovered_musts
    c = compile_contract("job", "founder cto", FACET_SCHEMA, lambda s, u: {"must": {"role_family": ["cto"], "work_type": ["founder"], "company": ["acme"]}, "prefer": {"level": ["leadership"]}})
    assert "role_family" not in c.must and c.prefer["role_family"] == ["cto"]          # an open phrase is a preference
    assert c.must == {"work_type": ["founder"], "company": ["acme"]}                   # exact keys may be promised
    moved = downgrade_uncovered_musts(c, {"work_type": 0.02, "company": 0.99})
    assert moved == ["work_type"] and c.must == {"company": ["acme"]} and c.prefer["work_type"] == ["founder"]
    assert validate_contract(c, FACET_SCHEMA) == []


def test_invented_pay_cannot_pass_the_verbatim_figure_gate():
    llm = lambda s, u: {"items": [{"i": 0, "field": "software", "comp": {"min": 150000, "max": 180000, "currency": "USD", "period": "year"}}]}
    no_figure = extract_envelopes("job", [{"title": "SWE", "head": "Great team, competitive pay.", "tail": ""}], FACET_SCHEMA, llm)
    assert "comp" not in no_figure[0]["facets"]                                            # the text never stated a figure
    with_figure = extract_envelopes("job", [{"title": "SWE", "head": "", "tail": "Base salary $150,000 – $180,000"}], FACET_SCHEMA, llm)
    assert with_figure[0]["facets"]["comp"][0]["number"] == 165000


def test_control_bytes_in_postings_never_reach_the_database():
    from api.facets_engine import clean_text
    assert clean_text("Base\x00 pay\x01 $1") == "Base pay $1"
    llm = lambda s, u: {"items": [{"i": 0, "specialty": ["tur\x00bomachinery"], "comp": {"min": 100000, "max": 120000, "currency": "USD", "period": "year", "display": "$100k\x00–$120k"}}]}
    env = extract_envelopes("job", [{"title": "T\x00", "head": "pay $100,000", "tail": ""}], FACET_SCHEMA, llm)
    assert env[0]["facets"]["specialty"][0]["value"] == "turbomachinery" and "\x00" not in env[0]["facets"]["comp"][0]["display"]
