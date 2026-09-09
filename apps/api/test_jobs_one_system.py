"""ONE JOB-MATCHING SYSTEM (owner, 2026-09-09).

"Match jobs to my résumé" used to be a SECOND engine behind its own endpoint, its own preference form
and its own ranking. It returned no facet contract and no counts, so its results carried no rail — and
a job map saved from it stored `contract: null`, a saved map that could never show which filters
produced it (the bug that started this).

Now a jobs search takes a QUERY, a PROFILE, or both. Every profile source — an attached CV, a pasted
CV, a self-description, a pasted LinkedIn URL, or the résumé on file (opt-in) — runs the SAME
contract → evaluator path as a typed search, so all of them carry contract + counts + labels and the
rail is the one set of controls.

Over the kernel's in-memory reference store: no DB, no model, no spend.
"""
from __future__ import annotations

import base64

from fastapi.testclient import TestClient
from roster_kernel.facets import InMemoryFacetStore
from roster_vertical.facet_schema import FACET_SCHEMA

from api.app import ResearchIn, create_app, profile_slate

ROWS = [
    {"id": "j1", "kind": "job", "sim": 0.62, "company": "acme", "title": "Senior Backend Engineer", "url": "https://a/1", "source": "ashby",
     "facets": {"level": ["senior"], "field": ["software"], "function": ["engineering"], "work_type": ["ic"],
                "company": ["acme"], "work_mode": ["remote"], "country": ["us"], "skill": ["go", "kubernetes"]}},
    {"id": "j2", "kind": "job", "sim": 0.60, "company": "acme", "title": "Backend Engineer", "url": "https://a/2", "source": "ashby",
     "facets": {"level": ["senior"], "field": ["software"], "function": ["engineering"], "work_type": ["ic"],
                "company": ["acme"], "work_mode": ["remote"], "country": ["us"], "skill": ["go"]}},
    {"id": "j3", "kind": "job", "sim": 0.58, "company": "sierra", "title": "Platform Engineer", "url": "https://a/3", "source": "lever",
     "facets": {"level": ["senior"], "field": ["software"], "function": ["engineering"], "work_type": ["ic"],
                "company": ["sierra"], "work_mode": ["hybrid"], "country": ["us"], "skill": ["kubernetes"]}},
]

CV = ("Jane Doe. Backend engineer with 8 years of experience building distributed systems. "
      "Skills: go, kubernetes, postgres. Worked at Acme on payments infrastructure.")


class _FakeClaimStore:
    """Just enough of the claim store for the jobs route: the header stats, and no company-site table.
    The rows themselves come from the kernel's in-memory facet store."""
    async def jobs_stats(self):
        return {"jobs": len(ROWS), "companies": len({r["company"] for r in ROWS})}

    async def _get_pool(self):
        raise RuntimeError("no pool in tests")


def _client(**env):
    app = create_app()
    app.state.facet_store = InMemoryFacetStore(ROWS, FACET_SCHEMA)
    app.state.claim_store = _FakeClaimStore()
    app.state._co_sites = None
    return TestClient(app)


def _jobs(c, **body):
    return c.post("/jobs", json={"tenant_id": "demo", **body})


# ---------------------------------------------------------------- the second engine is gone

def test_the_second_engine_is_gone(monkeypatch):
    """No /me/match-jobs search endpoint, and no stored preference config feeding it. ✨ Fine tune
    survives — it builds the recruiter brief the ONE path reads."""
    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/me/match-jobs" not in paths          # the second search
    assert "/me/match-jobs/config" not in paths   # the form's remembered preferences
    assert "/me/match-jobs/fine-tune" in paths    # the brief builder stays
    assert "/jobs" in paths


def test_using_the_resume_is_opt_in_and_off_by_default():
    """The résumé used to reshape every signed-in seeker's search with nothing on screen saying so and
    no way to turn it off. It is now a request the caller makes."""
    assert ResearchIn(question="x", tenant_id="demo").use_resume is False
    assert ResearchIn(question="x", tenant_id="demo", use_resume=True).use_resume is True


# ---------------------------------------------------------------- every profile source gets the rail

def _assert_has_a_rail(d, why):
    assert d.get("contract"), f"{why}: no contract — the rail cannot draw and a saved map has none"
    assert d.get("counts"), f"{why}: no counts — the rail's chips have nothing to count"
    assert d.get("labels"), f"{why}: no labels — the rail cannot name its keys"
    assert d.get("matched_on"), f"{why}: the result does not say what it matched on"


def test_an_attached_cv_runs_the_contract_path_and_carries_the_rail(monkeypatch):
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    c = _client()
    att = [{"name": "cv.txt", "media_type": "text/plain", "data": base64.b64encode(CV.encode()).decode()}]
    d = _jobs(c, question="", attachments=att).json()
    _assert_has_a_rail(d, "attached CV")
    assert d["matched_on"] == "attachment" and d["jobs"]


def test_a_pasted_self_description_runs_the_contract_path_and_carries_the_rail(monkeypatch):
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    c = _client()
    d = _jobs(c, question="I'm a backend engineer with 8 years of go, kubernetes and postgres experience").json()
    _assert_has_a_rail(d, "self-description")
    assert d["matched_on"] == "description"


def test_the_profile_is_the_semantic_text_and_its_skills_become_preferences(monkeypatch):
    """What was typed steers, the profile shapes — both land in the contract, and the skills the
    profile NAMES become prefers rather than being thrown away."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    c = _client()
    att = [{"name": "cv.txt", "media_type": "text/plain", "data": base64.b64encode(CV.encode()).decode()}]
    con = _jobs(c, question="", attachments=att).json()["contract"]
    assert "kubernetes" in con["text"].lower(), "the profile is not in the contract's semantic text"
    assert set(con.get("prefer", {}).get("skill") or []) >= {"go", "kubernetes"}


def test_the_stated_scope_filters_a_profile_search_too(monkeypatch):
    """The résumé path used to pass scope=None and set no country must, so a US-scoped résumé search
    quietly returned postings from anywhere. It now applies the same scope the typed path does."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    c = _client()
    att = [{"name": "cv.txt", "media_type": "text/plain", "data": base64.b64encode(CV.encode()).decode()}]
    con = _jobs(c, question="", attachments=att, country="us").json()["contract"]
    assert con["must"].get("country") == ["us"]


def test_the_tabs_must_have_toggles_reach_a_profile_search(monkeypatch):
    """A profile search honours the Jobs tab's own controls. On a full index the toggle is a must; on a
    pool this small the relaxer demotes it to a preference and SAYS so in `relaxed` — either way the
    toggle reached the contract, which is what the old résumé path never did with the scope."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    c = _client()
    att = [{"name": "cv.txt", "media_type": "text/plain", "data": base64.b64encode(CV.encode()).decode()}]
    d = _jobs(c, question="", attachments=att, job_must=["remote"]).json()
    held = d["contract"]["must"].get("work_mode") == ["remote"]
    relaxed = any(n.get("key") == "work_mode" for n in (d.get("relaxed") or []))
    assert held or relaxed, f"the remote toggle never reached the contract: {d['contract']} / {d.get('relaxed')}"
    if held:
        assert all("hybrid" not in (j.get("facets", {}).get("work_mode") or []) for j in d["jobs"])


def test_a_profile_search_with_nothing_typed_spends_no_model_call(monkeypatch):
    """With no query there is nothing to parse and nothing to compile: the contract is built from the
    profile and the tab's toggles. The surface this replaced spent two LLM calls compiling an empty
    string, on every click of a button built to be clicked repeatedly."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    from api import facets_engine
    calls = []
    monkeypatch.setattr(facets_engine, "compile_contract",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("compiled an empty query")))
    c = _client()
    att = [{"name": "cv.txt", "media_type": "text/plain", "data": base64.b64encode(CV.encode()).decode()}]
    d = _jobs(c, question="", attachments=att).json()
    assert not calls and d["jobs"]


# ---------------------------------------------------------------- the slate a profile search shows

def test_profile_slate_leads_with_a_diverse_set_and_never_drops_a_row():
    """One employer's hundred open postings must not crowd out the market: at most three per company
    LEAD, the rest follow. Carried over from the résumé matcher this path replaced — the typed path's
    thin_repeats alone does not bound a single company."""
    rows = [{"id": f"a{i}", "company": "acme", "title": f"Engineer {i}"} for i in range(5)]
    rows += [{"id": "s1", "company": "sierra", "title": "Engineer"}]
    out = profile_slate(rows)
    assert len(out) == len(rows), "a row was dropped — the overflow must be appended, never removed"
    assert [r["company"] for r in out[:4]] == ["acme", "acme", "acme", "sierra"]
    assert {r["id"] for r in out} == {r["id"] for r in rows}
