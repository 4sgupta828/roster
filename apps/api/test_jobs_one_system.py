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


# ---------------------------------------------------------------- the search profile the AI reads once

def test_the_pitch_carries_the_ais_judgment_not_just_its_prose():
    """The recruiter brief's JUDGMENT — which roles to pitch, at what level, on which skills, in which
    field — is what "let AI figure out the best roles for me" means. Embedding only `search_text` threw
    it away: target_roles and seniority reached nothing. The pitch is what the contract compiler reads,
    so every one of those becomes a facet term the rail can show."""
    from api.people_population import candidate_pitch
    brief = {"target_roles": ["Staff Software Engineer", "Principal Engineer"], "seniority": "staff_plus",
             "fields": ["software"], "technical_skills": ["go", "kubernetes"],
             "search_text": "Builds large distributed payment systems."}
    p = candidate_pitch(brief, {"city": "Austin", "region": "TX"})
    for must_appear in ("Staff Software Engineer", "Principal Engineer", "staff plus", "software",
                        "go", "kubernetes", "Austin", "distributed payment systems"):
        assert must_appear in p, f"{must_appear!r} missing from the pitch: {p!r}"


def test_an_empty_brief_makes_no_pitch_and_so_costs_no_compile():
    from api.people_population import candidate_pitch
    assert candidate_pitch({}, {}).strip() == ""
    assert candidate_pitch(None, None).strip() == ""


def test_a_saved_search_profile_is_held_to_the_vocabulary():
    """A hand-edited profile is validated exactly as a compiled one is: illegal keys and values are
    dropped, never stored. A typo must not become a preference that quietly matches nothing."""
    from roster_kernel.facets import Contract
    from api.app import validate_job_contract

    c = Contract(kind="job",
                 prefer={"level": ["senior", "not_a_level"], "made_up_key": ["x"],
                         "work_mode": ["remote", "teleport"], "skill": ["Go", "Kubernetes"]},
                 center={"key": "level", "value": "senior", "span": 1})
    d = validate_job_contract(c, FACET_SCHEMA)
    assert d["prefer"]["level"] == ["senior"]              # the invented level is gone
    assert "made_up_key" not in d["prefer"]                # the invented key is gone
    assert d["prefer"]["work_mode"] == ["remote"]          # 'teleport' is not in the vocabulary
    assert d["prefer"]["skill"] == ["go", "kubernetes"]    # open sets are kept, lowercased
    assert d["center"] == {"key": "level", "value": "senior", "span": 1}


def test_an_invented_level_centre_is_dropped_rather_than_stored():
    from roster_kernel.facets import Contract
    from api.app import validate_job_contract
    d = validate_job_contract(Contract(kind="job", center={"key": "level", "value": "wizard", "span": 1}),
                              FACET_SCHEMA)
    assert d["center"] is None


# ---------------------------------------------------------------- the fixes this line of work needed

def test_the_resume_toggle_never_kills_a_typed_search(monkeypatch):
    """🎯 Use my résumé is remembered in the browser and outlived a sign-out, so a seeker could reach a
    state where EVERY jobs search came back empty under a note telling them to type what they were
    looking for — which is what they had just done. With no résumé to shape it, the opt-in is ignored
    and a typed search is a typed search."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    c = _client()
    plain = _jobs(c, question="backend engineer").json()
    opted = _jobs(c, question="backend engineer", use_resume=True).json()
    assert plain["count"] == len(ROWS)
    assert opted["count"] == plain["count"], "the opt-in emptied a search it had nothing to shape"
    assert not opted.get("needs_profile")


def test_asking_for_the_resume_with_nothing_typed_still_says_how_to_get_one(monkeypatch):
    """The other half: with no query AND no résumé there is nothing to search on, and that is the one
    case that should ask for a résumé."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    d = _jobs(_client(), question="", use_resume=True).json()
    assert d["count"] == 0 and d["needs_profile"] is True


def test_the_resume_fingerprint_is_the_resume_and_nothing_else():
    """The brief and the search profile are cached against this. It used to hash `work_history` too —
    a field that is both parsed FROM the résumé and saved BY HAND on the Apply-Profile form — so the
    parser (parsed only) and the app (parsed under saved) computed different keys for the same résumé:
    the once-per-résumé model spend was thrown away, the account page said "not read yet" about a
    résumé that had been read, and editing the form orphaned a hand-tuned search profile."""
    from api.app import _resume_fingerprint
    text = "x" * 400
    parsed = {"_resume_text": text, "work_history": [{"title": "SWE", "company": "acme"}]}
    edited = {"_resume_text": text, "work_history": [{"title": "Senior SWE", "company": "acme"}]}
    assert _resume_fingerprint(parsed) == _resume_fingerprint(edited) != ""
    assert _resume_fingerprint({"_resume_text": text + "!"}) != _resume_fingerprint(parsed)
    assert _resume_fingerprint({}) == ""


# ---------------------------------------------------------------- the lexicon (spec: query-intent-decoding §4)

# A slice big enough that the evaluator's smart relaxation does NOT fire. `relax_to_enough` widens a
# contract whose slice is tiny, so a three-row fixture would demote the very must under test and the
# assertion would be about relaxation rather than about the lexicon.
def _lex_rows() -> list[dict]:
    rows = []
    for i in range(240):
        mode = ("remote", "hybrid", "onsite")[i % 3]      # every mode has a healthy slice, or a must
        staff = i % 5 == 0                                  # on one would simply be relaxed away
        metro = "new_york" if i % 2 == 0 else "austin"
        rows.append({"id": f"x{i}", "kind": "job", "sim": 0.62 - i * 0.0004, "company": "acme",
                     "title": "Backend Engineer", "url": f"https://a/x{i}", "source": "ashby",
                     "facets": {"work_mode": [mode],
                                "level": ["staff_plus" if staff else "senior"],
                                "field": ["software"], "country": ["us"], "metro": [metro]}})
    # the junk row prod actually returned for `remote`, and the one it returned for `austin`
    rows.append({"id": "junk_remote", "kind": "job", "sim": 0.70, "company": "global_elite",
                 "title": "Remote Opportunity - Take Back Control of Your Time", "url": "https://a/j1",
                 "source": "lever", "facets": {"work_mode": ["onsite"], "level": ["junior"],
                                               "field": ["other"], "country": ["us"], "metro": ["austin"]}})
    # the shape of the prod failure: the TITLE names the place, the JOB is somewhere else
    rows.append({"id": "junk_ny", "kind": "job", "sim": 0.69, "company": "us_ghost_adventures",
                 "title": "New York Tour Guide", "url": "https://a/j2", "source": "lever",
                 "facets": {"work_mode": ["onsite"], "level": ["junior"], "field": ["other"],
                            "country": ["us"], "metro": ["austin"]}})
    return rows


LEX_ROWS = _lex_rows()


def _lex_client():
    app = create_app()
    app.state.facet_store = InMemoryFacetStore(LEX_ROWS, FACET_SCHEMA)
    app.state.claim_store = _FakeClaimStore()
    app.state._co_sites = None
    return TestClient(app)


def _stub_compiler(monkeypatch, out: dict):
    """Pin what the MODEL returns. `api.model_json.llm_json` is the one call `compile_contract` makes
    (via `_llm_json`), so stubbing it keeps the whole real compile path — validation included — while
    spending nothing and touching no network."""
    monkeypatch.setattr("api.model_json.llm_json", lambda system, user, **kw: dict(out))


def _no_model(monkeypatch):
    """The compiler returns NOTHING — exactly what prod does for these queries. The point of the
    lexicon is that the search still works when the model reads a bare word as stating nothing."""
    _stub_compiler(monkeypatch, {})
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_INTENT_LEXICON", "1")


def test_a_bare_facet_value_becomes_a_filter_instead_of_a_one_word_embedding(monkeypatch):
    """Measured on prod 2026-09-09: `remote` compiled to `country=us` and nothing else, and returned
    "Remote Opportunity — Take Back Control of Your Time". `remote` is a literal member of WORK_MODES."""
    _no_model(monkeypatch)
    d = _jobs(_lex_client(), question="remote").json()
    assert d["contract"]["must"].get("work_mode") == ["remote"], d["contract"]
    assert d["contract"]["text"] == "", "the query was all filters — the must-slice should BE the pool"
    ids = [j["id"] for j in d["jobs"]]
    assert "junk_remote" not in ids, "the row whose TITLE says Remote is not a remote role"
    assert ids and all(j["facets"]["work_mode"] == ["remote"] for j in d["jobs"])


def test_a_bare_place_is_canonicalised_the_way_the_index_stores_it(monkeypatch):
    """`jobs in new york` named a place explicitly and still compiled no metro.

    And it must canonicalise toward the token the INDEX holds. Measured on prod: metro `new_york`
    has 21,942 rows and `nyc` 3,126, because the facet extractor follows the schema's guidance while
    `METRO_ALIAS` — which serves geo-scope resolution — rewrites the other way. Aliasing to the scope
    resolver's token pointed queries at the smaller bucket, which is worse than not aliasing at all."""
    _no_model(monkeypatch)
    d = _jobs(_lex_client(), question="jobs in new york").json()
    assert d["contract"]["must"].get("metro") == ["new_york"], d["contract"]
    ids = [j["id"] for j in d["jobs"]]
    assert "junk_ny" not in ids, "a row TITLED 'New York Tour Guide' is not a New York job"
    assert ids and all(j["facets"]["metro"] == ["new_york"] for j in d["jobs"])


def test_a_bare_seniority_word_becomes_the_level(monkeypatch):
    _no_model(monkeypatch)
    d = _jobs(_lex_client(), question="staff").json()
    assert d["contract"]["must"].get("level") == ["staff_plus"]
    assert d["jobs"] and all(j["facets"]["level"] == ["staff_plus"] for j in d["jobs"])


def test_the_lexicon_never_overrides_what_the_model_already_said(monkeypatch):
    """The compiler saw the whole sentence; the lexicon saw words. On any key the model spoke about,
    the model wins."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_INTENT_LEXICON", "1")
    _stub_compiler(monkeypatch, {"must": {"work_mode": ["hybrid"]}})
    d = _jobs(_lex_client(), question="remote").json()
    assert d["contract"]["must"].get("work_mode") == ["hybrid"]


def test_the_lexicon_is_off_by_default(monkeypatch):
    """Rule 20: flag off → byte-identical to today, junk results included."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.delenv("ROSTER_INTENT_LEXICON", raising=False)
    _stub_compiler(monkeypatch, {})
    d = _jobs(_lex_client(), question="remote").json()
    assert not d["contract"]["must"].get("work_mode")


def test_the_same_query_compiles_once(monkeypatch):
    """The one model call a typed search makes was uncached on this path — only /search/compile cached.
    Short queries are the ones that repeat, and a steering loop asks again every turn."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    calls = []

    def _one(system, user, **kw):
        calls.append(user)
        return {"prefer": {"field": ["software"]}}

    monkeypatch.setattr("api.model_json.llm_json", _one)
    c = _lex_client()
    a = _jobs(c, question="backend engineer").json()
    b = _jobs(c, question="backend engineer").json()
    assert len(calls) == 1, "the second identical search must not pay for the compile again"
    assert a["contract"]["prefer"] == b["contract"]["prefer"]
    _jobs(c, question="data engineer")
    assert len(calls) == 2, "a different query is a different compile"


def test_a_cached_compile_is_a_copy_not_the_same_object(monkeypatch):
    """The routes mutate the contract they get back — scope musts, toggles, the lexicon. A cache that
    handed out one shared object would accumulate every previous search's edits."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {"prefer": {"field": ["software"]}})
    c = _lex_client()
    first = _jobs(c, question="backend engineer", job_must=["remote"]).json()
    second = _jobs(c, question="backend engineer").json()
    assert "work_mode" in (first["contract"]["must"] or {})
    assert "work_mode" not in (second["contract"]["must"] or {}), "the toggle leaked through the cache"


# ------------------------------------------- the rail's Apply, and what a steering loop re-runs through

def test_re_evaluating_a_contract_returns_the_grouping_menu_for_the_new_rows(monkeypatch):
    """The rail's Apply posts a mutated contract to /search/evaluate, which returned no `group_options`
    because only /jobs computed them. The browser guards with `if(out.group_options)`, so it kept the
    menu built for the PREVIOUS rows — the grouping offered described results that were no longer on
    screen. It is also what the convergence loop re-runs a contract through, so it has to carry the
    whole jobs payload."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    c = _lex_client()
    r = c.post("/search/evaluate", json={"contract": {"kind": "job", "must": {"work_mode": ["remote"]}, "limit": 20}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rows"], "the contract matches rows"
    assert d.get("group_options"), "an Apply must bring back the menu for the rows it just returned"
    assert all(row["facets"]["work_mode"] == ["remote"] for row in d["rows"])


def test_a_follow_up_inherits_the_turn_before_it(monkeypatch):
    """Measured in the code: `refine_query` is read only past the evaluator's own return, so with the
    evaluator on — which is prod — every jobs follow-up recompiled from nothing. A conversation
    narrows: "remote roles", then "in austin", keeps remote."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {"must": {"metro": ["austin"]}})
    c = _lex_client()
    prior = {"kind": "job", "must": {"work_mode": ["remote"]}, "prefer": {"field": ["software"]}}
    d = _jobs(c, question="in austin", prior_contract=prior).json()
    got = d["contract"]["must"]
    assert got.get("metro") == ["austin"], got
    assert got.get("work_mode") == ["remote"], "the previous turn's filter was dropped"
    assert d["contract"]["prefer"].get("field") == ["software"]


def test_the_new_turn_wins_over_the_old_one_on_any_key_it_speaks_about(monkeypatch):
    """Carrying forward must never overrule the reader. Saying "hybrid" after "remote" is a change of
    mind, not a contradiction to be merged."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {"must": {"work_mode": ["hybrid"]}})
    c = _lex_client()
    d = _jobs(c, question="hybrid instead", prior_contract={"kind": "job", "must": {"work_mode": ["remote"]}}).json()
    assert d["contract"]["must"].get("work_mode") == ["hybrid"]


def test_a_carried_preference_never_hardens_into_a_filter(monkeypatch):
    """A refinement must not quietly promote something the previous turn only ranked on."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {})
    c = _lex_client()
    d = _jobs(c, question="anything", prior_contract={"kind": "job", "prefer": {"level": ["staff_plus"]}}).json()
    assert d["contract"]["prefer"].get("level") == ["staff_plus"]
    assert "level" not in (d["contract"]["must"] or {})


def test_the_scope_is_never_inherited_from_a_previous_turn(monkeypatch):
    """`country` belongs to the Where selector, which is applied separately every turn. Carrying it
    would let a stale worldwide search silently outlive the control that set it."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {})
    c = _lex_client()
    d = _jobs(c, question="anything", country="us",
              prior_contract={"kind": "job", "must": {"country": ["de"]}}).json()
    assert d["contract"]["must"].get("country") == ["us"]


# -------------------------------------- the intent debugger (spec: intent-convergence-loop §3, rev 2)
#
# It replaced a row of facet chips. The owner's verdict on those was exact: "I can do the same from
# existing chips — what am I getting more?" Nothing: a chip filters what came back, which the rail
# already does key by key with counts. Narrowing belongs to the rail; this asks whether the QUESTION
# was understood, which is a reading and not a count.

def _stub_intent(monkeypatch, payload):
    """Pin the debugger's model call. It goes through the same `llm_json` as the compiler, so the stub
    answers whichever of the two is asking by looking at the system prompt."""
    def _route(system, user, **kw):
        return dict(payload) if "recruiting consultant" in (system or "").lower() else {}
    monkeypatch.setattr("api.model_json.llm_json", _route)


READINGS = {
    "understanding": ["infrastructure, not developer tooling", "staff level"],
    "noticed": "Two thirds of these are at big platform vendors.",
    "believed": "Right now I'm showing infrastructure and reliability roles.",
    "question": "Do you mean the teams that run the infrastructure, or the ones building internal tooling?",
    "readings": [
        {"label": "Infrastructure platform", "says": "Kubernetes, cloud and reliability work.",
         "text": "kubernetes cloud reliability infrastructure", "prefer": {"field": ["software"]}},
        {"label": "Developer tooling", "says": "Internal build systems, CI and developer experience.",
         "text": "developer experience build systems CI"},
    ],
    "ask": "Or tell me the stack you want to be in.",
}


def test_a_short_query_gets_readings_in_prose_not_chips(monkeypatch):
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_DIRECTIONS", "1")
    _stub_intent(monkeypatch, READINGS)
    d = _jobs(_lex_client(), question="platform engineer").json()
    ic = d.get("intent_check") or {}
    assert ic.get("believed", "").startswith("Right now"), ic
    assert ic.get("understanding") == ["infrastructure, not developer tooling", "staff level"]
    assert ic.get("noticed"), "an analyst says what the results are made of"
    assert ic.get("question")
    assert [r["label"] for r in ic.get("readings") or []] == ["Infrastructure platform", "Developer tooling"]
    assert ic["readings"][0]["text"], "a reading carries the search it would run"
    assert ic.get("ask")
    assert "directions" not in d, "the chip surface is gone"


def test_a_long_specific_query_is_not_interrupted(monkeypatch):
    """Every ask costs a model call and a reader's attention. A specific question that matched well is
    left alone — and the call is never made."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_DIRECTIONS", "1")
    calls = []

    def _count(system, user, **kw):
        calls.append(system)
        return dict(READINGS) if "recruiting consultant" in (system or "").lower() else {}

    monkeypatch.setattr("api.model_json.llm_json", _count)
    q = "staff backend engineer at stripe working on payments infrastructure in new york"
    d = _jobs(_lex_client(), question=q).json()
    assert (d.get("intent_check") or {}).get("readings") == []
    assert not any("recruiting consultant" in (c or "").lower() for c in calls), "no call for a specific query"


def test_a_reading_that_names_an_illegal_value_is_cleaned_not_shown(monkeypatch):
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_DIRECTIONS", "1")
    _stub_intent(monkeypatch, {**READINGS, "readings": [
        {"label": "Infra", "says": "x", "text": "kubernetes", "prefer": {"level": ["wizard"], "nope": ["y"]}}]})
    d = _jobs(_lex_client(), question="platform engineer").json()
    r = (d.get("intent_check") or {})["readings"][0]
    assert r["prefer"] == {}, "an invented value never reaches a search"
    assert r["text"] == "kubernetes"


def test_the_debugger_is_off_by_default(monkeypatch):
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.delenv("ROSTER_DIRECTIONS", raising=False)
    _stub_intent(monkeypatch, READINGS)
    d = _jobs(_lex_client(), question="platform engineer").json()
    assert "intent_check" not in d


def test_a_pasted_job_link_is_read_as_a_profile_not_embedded_as_a_url(monkeypatch):
    """Measured on prod: a greenhouse URL was embedded as a string, so the search returned the nearest
    neighbours of a URL. What the reader means is "more roles like this one"."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_JD_URL", "1")
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {})
    jd = ("Senior Backend Engineer at Acme. You will build distributed payment systems in Go and "
          "Kubernetes on Postgres. We are looking for someone with deep experience in reliability, "
          "on-call ownership and API design. This role is remote within the United States. ") * 3
    c = _lex_client()
    c.app.state.jd_reader = lambda url: jd
    d = _jobs(c, question="https://boards.greenhouse.io/acme/jobs/123").json()
    assert d.get("matched_on") == "job_link", d.get("note")
    assert d["jobs"], "a linked posting should return its neighbours"
    assert "posting you linked" in (d.get("note") or "")
    assert jd[:40].lower().split()[0] in (d["contract"]["text"] or "").lower()


def test_a_job_link_that_cannot_be_read_stays_an_ordinary_search(monkeypatch):
    """Fail-safe: an unreachable or empty posting must not silently become an empty profile."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_JD_URL", "1")
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {})
    c = _lex_client()
    c.app.state.jd_reader = lambda url: ""
    d = _jobs(c, question="https://boards.greenhouse.io/acme/jobs/123").json()
    assert d.get("matched_on") != "job_link"
    assert "jobs" in d


def test_the_job_link_route_is_off_by_default(monkeypatch):
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.delenv("ROSTER_JD_URL", raising=False)
    monkeypatch.setattr("api.model_json.llm_json", lambda s, u, **kw: {})
    d = _jobs(_lex_client(), question="https://boards.greenhouse.io/acme/jobs/123").json()
    assert d.get("matched_on") != "job_link"


def test_a_search_never_runs_with_neither_a_filter_nor_a_query(monkeypatch):
    """THE INVARIANT BEHIND THE REGRESSION THAT REACHED PROD. The lexicon blanked the semantic text on
    the strength of a must that `downgrade_uncovered_musts` then demoted; what ran had no filter and no
    words, so the pool became an arbitrary page of the whole scope.

    The exemption for reader-named keys means that particular sequence can no longer happen — but the
    property is what matters, not the mechanism that threatened it, so it is asserted directly over
    every shape: a bare facet value, a partly-understood query, and one the lexicon cannot read at all.
    `country` never counts as narrowing; it is on every search."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_INTENT_LEXICON", "1")
    _stub_compiler(monkeypatch, {})

    for coverage in ({"work_mode": 0.20, "level": 0.2, "metro": 0.2},      # sparse: demotions likely
                     {"work_mode": 0.95, "level": 0.95, "metro": 0.95}):   # dense: musts stand
        for q in ("remote", "staff backend engineer in austin", "cuda kernels", "jobs in new york"):
            app = create_app()
            app.state.facet_store = InMemoryFacetStore(LEX_ROWS, FACET_SCHEMA)
            app.state.claim_store = _FakeClaimStore(); app.state._co_sites = None
            app.state._facet_cov = {"job": (1e18, dict(coverage))}
            c = TestClient(app).post("/jobs", json={"tenant_id": "demo", "question": q}).json()["contract"]
            narrowing = [k for k in (c["must"] or {}) if k != "country"]
            assert c["text"] or narrowing, f"{q!r} at {coverage}: no filter and no query — the pool is arbitrary"


def test_the_text_is_blanked_when_a_must_does_survive(monkeypatch):
    """The other side: when the promoted must is on a well-covered key it survives, and blanking is
    then the whole point — the must-slice becomes the pool instead of one word's neighbourhood."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_INTENT_LEXICON", "1")
    _stub_compiler(monkeypatch, {})

    app = create_app()
    app.state.facet_store = InMemoryFacetStore(LEX_ROWS, FACET_SCHEMA)
    app.state.claim_store = _FakeClaimStore(); app.state._co_sites = None
    app.state._facet_cov = {"job": (1e18, {"work_mode": 0.95, "level": 0.95, "metro": 0.95})}
    d = TestClient(app).post("/jobs", json={"tenant_id": "demo", "question": "remote"}).json()
    c = d["contract"]
    assert c["must"].get("work_mode") == ["remote"]
    assert c["text"] == ""


def test_a_facet_the_reader_typed_survives_the_coverage_guard(monkeypatch):
    """THE REASON THE FEATURE DID NOTHING IN PROD. `work_mode` is known on 38 % of jobs, so the guard
    demoted the lexicon's `must: remote` to a preference — and with the semantic text kept, the same
    one-word embedding chose the pool and `remote` still returned "Remote Opportunity — Take Back
    Control of Your Time".

    The guard is right about the MODEL and wrong about the READER: it exists to stop a compiler
    inventing a hard filter the corpus cannot support. Someone who types exactly `remote` has not
    inferred anything, they have asked. So a key the reader named is exempt, exactly as the rail's own
    toggles already are, and the must survives — which is what makes the slice the pool."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_INTENT_LEXICON", "1")
    _stub_compiler(monkeypatch, {})

    app = create_app()
    app.state.facet_store = InMemoryFacetStore(LEX_ROWS, FACET_SCHEMA)
    app.state.claim_store = _FakeClaimStore(); app.state._co_sites = None
    app.state._facet_cov = {"job": (1e18, {"work_mode": 0.38, "level": 0.42, "metro": 0.41})}
    d = TestClient(app).post("/jobs", json={"tenant_id": "demo", "question": "remote"}).json()

    c = d["contract"]
    assert c["must"].get("work_mode") == ["remote"], "the reader asked for it; it must not be demoted"
    assert c["text"] == "", "with the must standing, the slice is the pool"
    assert d["jobs"] and all(j["facets"]["work_mode"] == ["remote"] for j in d["jobs"])


def test_a_facet_only_the_model_guessed_is_still_demoted(monkeypatch):
    """The other half: the guard must keep doing its job for a compiled must on a sparse key, or a
    model's guess silently filters out most of the index by absence."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.delenv("ROSTER_INTENT_LEXICON", raising=False)
    _stub_compiler(monkeypatch, {"must": {"work_mode": ["remote"]}})

    app = create_app()
    app.state.facet_store = InMemoryFacetStore(LEX_ROWS, FACET_SCHEMA)
    app.state.claim_store = _FakeClaimStore(); app.state._co_sites = None
    app.state._facet_cov = {"job": (1e18, {"work_mode": 0.38})}
    d = TestClient(app).post("/jobs", json={"tenant_id": "demo", "question": "somewhere flexible"}).json()
    c = d["contract"]
    assert "work_mode" not in (c["must"] or {}), "a model's guess on a 38 %-covered key still demotes"
    assert c["prefer"].get("work_mode") == ["remote"]


def test_the_reader_naming_a_facet_hardens_what_the_model_only_ranked(monkeypatch):
    """Measured on prod: "jobs in new york" compiled `prefer: metro=new_york` — the place was
    recognised and then only ranked on, so results were New York-flavoured rather than in New York.
    When the lexicon covers the whole query and names the same value, that is the reader stating it."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_INTENT_LEXICON", "1")
    _stub_compiler(monkeypatch, {"prefer": {"metro": ["new_york"]}})
    d = _jobs(_lex_client(), question="jobs in new york").json()
    c = d["contract"]
    assert c["must"].get("metro") == ["new_york"], c
    assert "metro" not in (c["prefer"] or {})
    assert d["jobs"] and all(j["facets"]["metro"] == ["new_york"] for j in d["jobs"])


def test_a_disagreement_still_leaves_the_model_in_charge(monkeypatch):
    """The lexicon read words; the compiler read the sentence. Where they name DIFFERENT values, the
    one that saw the grammar wins and nothing is hardened behind the reader's back."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_INTENT_LEXICON", "1")
    _stub_compiler(monkeypatch, {"prefer": {"work_mode": ["hybrid"]}})
    c = _jobs(_lex_client(), question="remote").json()["contract"]
    assert c["prefer"].get("work_mode") == ["hybrid"], c
    assert "work_mode" not in (c["must"] or {})


def test_the_debugger_is_given_what_the_conversation_already_settled(monkeypatch):
    """A consultant who forgets the last exchange asks the same question twice — and worse, re-offers a
    reading the reader already turned down. The turns so far ride with the request, and what was
    ESTABLISHED carries forward cumulatively rather than being rebuilt each turn."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_DIRECTIONS", "1")
    seen = {}

    def _route(system, user, **kw):
        if "recruiting consultant" in (system or "").lower():
            seen["user"] = user
            return dict(READINGS)
        return {}

    monkeypatch.setattr("api.model_json.llm_json", _route)
    _jobs(_lex_client(), question="platform engineer", intent_history=[
        {"asked": "platform", "offered": ["Infra", "DevEx"], "chose": "Infra",
         "understood": ["infrastructure, not tooling"]},
        {"asked": "infrastructure", "told": "I care about reliability at scale"},
    ])
    msg = seen.get("user") or ""
    assert "THE CONVERSATION SO FAR" in msg
    assert "they chose: Infra" in msg
    assert 'they said: "I care about reliability at scale"' in msg
    assert "established: infrastructure, not tooling" in msg


def test_a_converged_conversation_keeps_its_notes_and_stops_asking(monkeypatch):
    """Converging is success, not failure. With nothing left to ask, the turn still carries what was
    established and what the results show — it just stops putting questions."""
    monkeypatch.setenv("ROSTER_JOBS", "1"); monkeypatch.setenv("ROSTER_FACET_EVALUATOR", "1")
    monkeypatch.setenv("ROSTER_DIRECTIONS", "1")
    _stub_intent(monkeypatch, {"understanding": ["staff infra, reliability at scale, remote"],
                               "noticed": "These are all platform teams at infrastructure vendors.",
                               "believed": "Right now I'm showing staff infrastructure roles.",
                               "question": "", "readings": []})
    d = _jobs(_lex_client(), question="platform engineer").json()
    ic = d.get("intent_check") or {}
    assert ic.get("readings") == [] and ic.get("understanding"), ic
    assert ic.get("noticed")
