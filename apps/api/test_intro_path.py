"""Intro path (job card): the user's own LinkedIn export + a PUBLIC hiring-manager map per company."""
import asyncio


def _run(coro):
    """Run a coroutine on a fresh loop that STAYS current (asyncio.run() would leave later tests loop-less)."""
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)
import importlib.util
import pathlib


def _facet(key, val, disp=None):
    return {"facet_key": key, "facet_value_norm": val, "value_norm": val, "display_value": disp or val.replace("_", " ").title(),
            "document_id": "d", "block_id": "b"}


def test_hiring_managers_rank_discipline_match_then_evidence():
    from api.people_population import hiring_managers_at

    class _Store:
        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            assert facets == {"company": ["stripe"]}
            return [
                {"entity_id": "a", "name": "Ann", "facets": [_facet("company", "stripe"), _facet("seniority", "engineering_manager"), _facet("function", "infrastructure")]},
                {"entity_id": "b", "name": "Bo", "facets": [_facet("company", "stripe"), _facet("seniority", "director"), _facet("function", "marketing")]},
                {"entity_id": "c", "name": "Cy", "facets": [_facet("company", "stripe"), _facet("seniority", "senior"), _facet("role", "software_engineer")]},
                {"entity_id": "d", "name": "Di", "facets": [_facet("company", "stripe"), _facet("title", "Head of Platform Engineering", "Head of Platform Engineering")]},
            ]

    res = _run(hiring_managers_at(_Store(), tenant_id="demo", company="Stripe", title="Senior Backend Engineer", department="Infrastructure"))
    names = [m["name"] for m in res["managers"]]
    assert "Cy" not in names                                   # an IC is not a hiring manager
    assert names[:2] and set(names[:2]) == {"Ann", "Di"}      # infra / platform leads first
    assert "Bo" not in names                                  # a marketing director does not hire for an infra opening
    assert res["n_at_company"] == 4 and "backend / infra" in res["discipline"]
    assert any("leads backend / infra" in w for w in res["managers"][0]["why"])


def test_company_normalizer_for_connections():
    from api.accounts import _norm_company
    assert _norm_company("Stripe, Inc.") == "stripe" == _norm_company("STRIPE") == _norm_company("Stripe Inc")
    assert _norm_company("The Trade Desk") == "trade_desk"


def test_ingest_body_helpers():
    spec = importlib.util.spec_from_file_location("ingest_jobs", pathlib.Path(__file__).resolve().parents[2] / "scripts" / "ingest_jobs.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    html = "<div><h2>About</h2><p>We use &amp; love <b>Kubernetes</b>, Java and Postgres.</p><ul><li>5+ years</li></ul><script>x()</script></div>"
    txt = mod._html_text(html)
    assert "Kubernetes" in txt and "x()" not in txt and "&amp;" not in txt
    assert mod.body_skills(txt) == ["java", "kubernetes", "postgres"]
    b = mod.blurb({"title": "Backend Engineer", "location": "Austin", "department": "Infra", "body": txt}, "Acme")
    assert b.startswith("Backend Engineer at Acme. Austin. Infra") and "Kubernetes" in b
    assert mod.blurb({"title": "T", "location": "", "department": ""}, "Acme") == "T at Acme. . "
