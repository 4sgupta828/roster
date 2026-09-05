"""/search/evaluate and /search/compile over the kernel's in-memory reference store (no DB, no model)."""
from __future__ import annotations

from fastapi.testclient import TestClient
from roster_kernel.facets import UNKNOWN, InMemoryFacetStore
from roster_vertical.facet_schema import FACET_SCHEMA

from api.app import create_app

ROWS = [
    {"id": "j1", "kind": "job", "sim": 0.62, "company": "acme", "title": "Founding CTO", "url": "https://a/1", "source": "ashby",
     "facets": {"level": ["leadership"], "field": ["software"], "function": ["executive"], "work_type": ["founder"], "company": ["acme"], "company_type": ["startup"], "work_mode": ["remote"]}},
    {"id": "j2", "kind": "job", "sim": 0.60, "company": "sonar", "title": "Field CTO", "url": "https://a/2", "source": "lever",
     "facets": {"level": ["leadership"], "field": ["sales"], "function": ["sales"], "work_type": ["ic"], "company": ["sonar"], "work_mode": ["remote"]}},
    {"id": "j3", "kind": "job", "sim": 0.58, "company": "endurance", "title": "Director of Turbomachinery", "url": "https://a/3", "source": "ashby",
     "facets": {"level": ["leadership"], "field": ["mechanical_civil_electrical"], "function": ["engineering"], "work_type": ["executive"], "company": ["endurance"], "comp": ["200k_300k"]}, "numeric": {"comp": 250000.0}},
    {"id": "j4", "kind": "job", "sim": 0.57, "company": "sierra", "title": "Software Engineer", "url": "https://a/4", "source": "ashby",
     "facets": {"field": ["software"], "function": ["engineering"], "work_type": ["ic"], "company": ["sierra"], "work_mode": ["hybrid"]}},
]


def _client():
    app = create_app()
    app.state.facet_store = InMemoryFacetStore(ROWS, FACET_SCHEMA)
    return TestClient(app)


def test_evaluate_returns_rows_counts_and_coverage_and_honours_musts():
    c = _client()
    r = c.post("/search/evaluate", json={"contract": {"kind": "job", "text": "founder cto", "must": {"level": ["leadership"]},
                                                      "avoid": {"field": ["sales", "mechanical_civil_electrical"]}, "prefer": {"company_type": ["startup"]}}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [x["id"] for x in d["rows"]] == ["j1", "j2", "j3"]                          # j4 (unknown level) excluded by the must
    assert d["rows"][0]["reasons"] and "startup" in " ".join(d["rows"][0]["reasons"])
    assert d["counts"]["field"] == {"software": 1, "sales": 1, "mechanical_civil_electrical": 1}
    assert d["counts"]["work_mode"] == {"remote": 2, UNKNOWN: 1} and d["counts"]["comp"] == {"200k_300k": 1, UNKNOWN: 2}
    assert d["coverage"]["pool"] == 3 and d["contract"]["must"] == {"level": ["leadership"]}


def test_evaluate_rejects_an_illegal_contract_with_a_clear_message():
    c = _client()
    r = c.post("/search/evaluate", json={"contract": {"kind": "job", "must": {"level": ["principal"]}}})
    assert r.status_code == 400 and "principal" in r.json()["detail"]


def test_rank_by_comp_puts_unknown_last_and_numeric_must_uses_the_number():
    c = _client()
    d = c.post("/search/evaluate", json={"contract": {"kind": "job", "text": "engineer", "rank_by": "comp"}}).json()
    assert d["rows"][0]["id"] == "j3"
    d2 = c.post("/search/evaluate", json={"contract": {"kind": "job", "text": "engineer", "must": {"comp": {"min": 200000}}}}).json()
    assert [x["id"] for x in d2["rows"]] == ["j3"]


def test_compile_endpoint_uses_the_model_and_validates(monkeypatch):
    import api.app as appmod
    app = create_app()
    app.state.facet_store = InMemoryFacetStore(ROWS, FACET_SCHEMA)
    # the engine's model call is injected through the app's _llm_json; patch the module-level fallback path via monkeypatching urllib
    import api.facets_engine as eng
    monkeypatch.setattr(eng, "compile_contract", lambda kind, text, schema, llm, **kw: eng.Contract(kind=kind, text=text, must={"level": ["leadership"]}, limit=kw.get("limit", 60)))
    c = TestClient(app)
    r = c.post("/search/compile", json={"kind": "job", "text": "founder cto"})
    assert r.status_code == 200 and r.json()["contract"]["must"] == {"level": ["leadership"]}
    r2 = c.post("/search/compile", json={"kind": "job", "text": "founder cto"})            # cached → same contract
    assert r2.json() == r.json()


PEOPLE = [
    {"id": "p1", "kind": "person", "sim": 0.70, "name": "Ada", "facets": {"level": ["leadership"], "work_type": ["executive"], "field": ["software"], "metro": ["bay_area"], "country": ["us"]}},
    {"id": "p2", "kind": "person", "sim": 0.66, "name": "Grace", "facets": {"level": ["senior"], "work_type": ["ic"], "field": ["software"], "metro": ["nyc"], "country": ["us"]}},
    {"id": "p3", "kind": "person", "sim": 0.55, "name": "Linus", "facets": {"level": ["leadership"], "work_type": ["founder"], "field": ["software"], "country": ["fi"]}},
]


def test_people_evaluate_returns_card_shaped_rows_the_talent_surface_renders():
    """kind=person rows come back in the people-card shape (entity_id, name, attributes, links) with the
    evaluator's facets / match / reasons riding along — even with no people store to hydrate from."""
    app = create_app()
    app.state.facet_store = InMemoryFacetStore(ROWS + PEOPLE, FACET_SCHEMA)
    c = TestClient(app)
    r = c.post("/search/evaluate", json={"contract": {"kind": "person", "text": "cto", "must": {"level": ["leadership"]}, "prefer": {"country": ["us"]}}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [x["entity_id"] for x in d["rows"]] == ["p1", "p3"]
    row = d["rows"][0]
    assert row["name"] == "Ada" and row["match_pct"] and row["facets"]["level"] == ["leadership"]
    assert isinstance(row.get("attributes"), list) and isinstance(row.get("links"), list)
    assert d["counts"]["level"] == {"leadership": 2} and d["counts"]["work_type"] == {"executive": 1, "founder": 1}
    assert d["labels"]["order"][0] == "field" and "years" in d["labels"]["keys"]
    assert d["labels"]["keys"]["evidence"] == "Public evidence"


def test_legacy_people_projection_endpoint_needs_the_admin_token(monkeypatch):
    monkeypatch.setenv("ROSTER_ADMIN_TOKEN", "secret")
    c = _client()
    assert c.post("/admin/facets/project-people").status_code == 401
