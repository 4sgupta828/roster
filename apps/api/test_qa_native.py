"""Phase-0 contract tests for the NATIVE Q&A router (flag ROSTER_QA_ROUTER) — the amended design's
acceptance surface (docs/qa_improvements_amended_design.md §Rollout Phase 0):

- /qa is native (no Eigen call), returns source="roster", persists kind="qa";
- /qa/stream uses the native SSE runner and its final payload matches /qa's shape;
- not_people_query falls through to native research even with ROSTER_PEOPLE_POPULATION on;
- a named person becomes a grounded DOSSIER run (research fires) with profile links attached as
  secondary material — never only the static card;
- zero-match faceted people discovery stays an honest index coverage gap (research does NOT fire);
- a connection question answers from the claim graph with per-hop citations when a path exists;
- router failure fails SAFE into general research;
- flag OFF keeps the legacy behavior byte-identical (people intercept, Eigen 404).
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from roster_kernel.research.react import AnswerResult

import api.app as appmod
from api.app import create_app
from api.qa_router import QaRoute


class _RouteLLM:
    """The router's LLM — returns a scripted _QaRouteParse-shaped object."""
    def __init__(self, parsed):
        self._parsed = parsed

    async def complete(self, *, system, messages, response_format, max_tokens=2048,
                       temperature=None):
        from roster_kernel.providers.llm import LLMResult
        if isinstance(self._parsed, Exception):
            raise self._parsed
        return LLMResult(parsed=response_format(**self._parsed), output_tokens=5)


class _AskSpy:
    """A ResearchService stand-in that records whether/what native research ran."""
    ui = None

    def __init__(self, answer="GROUNDED PROSE"):
        self.calls: list[dict] = []
        self._answer = answer

    async def ask(self, **kwargs):
        self.calls.append(kwargs)
        return AnswerResult(composed_answer=self._answer)


def _client(monkeypatch, service, route_parsed, **env):
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    monkeypatch.delenv("ROSTER_EIGEN_QA", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _RouteLLM(route_parsed))
    return TestClient(create_app(service))


def test_qa_native_returns_roster_source_no_eigen(monkeypatch):
    svc = _AskSpy()
    client = _client(monkeypatch, svc, {"route": "general_professional_qa", "confidence": "high"})
    # Any outbound Eigen call would explode loudly: kill httpx for the app module's scope.
    monkeypatch.setattr("httpx.AsyncClient", None, raising=False)
    r = client.post("/qa", json={"question": "explain the AI infra recruiting market",
                                 "tenant_id": "demo"})
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "roster"
    assert data["answer"] == "GROUNDED PROSE"
    assert data["qa_route"]["route"] == "general_professional_qa"
    assert len(svc.calls) == 1                      # native research ran


def test_qa_stream_native_final_matches_qa_shape(monkeypatch):
    svc = _AskSpy()
    client = _client(monkeypatch, svc, {"route": "general_professional_qa", "confidence": "high"})
    with client.stream("POST", "/qa/stream", json={"question": "what does Roster do",
                                                   "tenant_id": "demo"}) as resp:
        assert resp.status_code == 200
        events = [json.loads(l.removeprefix("data: ")) for l in resp.iter_lines()
                  if l.startswith("data: ")]
    assert events[0]["type"] == "run"               # run event first (resumable)
    final = [e for e in events if e["type"] == "final"][0]["result"]
    assert final["source"] == "roster"
    assert final["answer"] == "GROUNDED PROSE"
    # router progress event streamed
    assert any(e.get("type") == "route" for e in events)


def test_not_people_query_falls_through_to_research(monkeypatch):
    """With people-population ON, a non-people question must reach ResearchService.ask (the
    over-interception bug the design fixes)."""
    svc = _AskSpy()
    client = _client(monkeypatch, svc,
                     {"route": "general_professional_qa", "confidence": "high"},
                     ROSTER_PEOPLE_POPULATION="1")
    r = client.post("/research", json={"question": "how do vector databases work",
                                       "tenant_id": "demo"})
    assert r.status_code == 200
    assert r.json()["answer"] == "GROUNDED PROSE"
    assert len(svc.calls) == 1


def test_named_person_routes_to_dossier_with_secondary_links(monkeypatch):
    """A named-person question runs GROUNDED research (dossier contract) and attaches the profile
    search links as SECONDARY material — never only the static card."""
    svc = _AskSpy(answer="Jane Roe is a systems engineer…")
    client = _client(monkeypatch, svc,
                     {"route": "person_dossier", "subject_kind": "person",
                      "entities": ["Jane Roe"], "confidence": "high"})
    r = client.post("/qa", json={"question": "who is Jane Roe?", "tenant_id": "demo"})
    assert r.status_code == 200
    data = r.json()
    assert data["answer"].startswith("Jane Roe is")
    assert len(svc.calls) == 1
    # the dossier answer contract was applied
    assert "PERSON DOSSIER" in (svc.calls[0].get("answer_format_override") or "")
    hosts = {p["host"] for p in data["people"]}
    assert "github.com" in hosts and "linkedin.com" in hosts


def test_zero_match_discovery_stays_coverage_gap(monkeypatch):
    """True faceted people discovery with zero index matches remains an HONEST coverage gap —
    research/web never fires to invent a population."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse

    class _TwoStageLLM:
        """Call 1 = router (QaRoute), call 2 = the people facet compiler."""
        def __init__(self):
            self.n = 0

        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            self.n += 1
            if response_format is _FacetParse:
                return LLMResult(parsed=_FacetParse(role=["underwater_basket_weaver"]),
                                 output_tokens=5)
            return LLMResult(parsed=response_format(route="indexed_people_discovery",
                                                    subject_kind="person", confidence="high"),
                             output_tokens=5)

    class _EmptyStore:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 10, "source_documents": 5, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            return []

    svc = _AskSpy(answer="SHOULD NEVER APPEAR")
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_POPULATION", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_PEOPLE_SEMANTIC_FIRST", raising=False)
    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _TwoStageLLM())
    app = create_app(svc)
    app.state.claim_store = _EmptyStore()
    client = TestClient(app)
    r = client.post("/qa", json={"question": "find underwater basket weavers in Reno",
                                 "tenant_id": "demo"})
    assert r.status_code == 200
    data = r.json()
    assert data["people_rows"] == []
    assert "SHOULD NEVER APPEAR" not in (data["answer"] or "")
    assert svc.calls == []                          # web research never ran
    assert data["coverage_basis"] is not None       # honest coverage accounting


def test_connection_question_answers_from_graph(monkeypatch):
    """'how is X connected to Y' with both endpoints + a grounded edge → a cited path answer,
    no research run."""
    class _GraphStore:
        async def find_entity(self, ref, *, tenant_id):
            ids = {"ada byte": {"entity_id": "github:ada", "name": "Ada Byte", "kind": "person"},
                   "acme": {"entity_id": "co:acme", "name": "Acme", "kind": "company"},
                   "github:ada": {"entity_id": "github:ada", "name": "Ada Byte", "kind": "person"},
                   "co:acme": {"entity_id": "co:acme", "name": "Acme", "kind": "company"}}
            return ids.get(ref.lower())

        async def neighbors(self, eid, *, tenant_id, relations=None, cap=400):
            edge = {"subject_id": "github:ada", "predicate": "worked_at", "object_id": "co:acme",
                    "claim_id": "c1", "citation": {"document_id": "d1", "block_id": "b1",
                                                   "quote": "Ada Byte was a staff engineer at Acme",
                                                   "authority_tier": 3}}
            return [edge] if eid in ("github:ada", "co:acme") else []

    svc = _AskSpy(answer="SHOULD NOT RUN")
    client = _client(monkeypatch, svc,
                     {"route": "connection_path", "subject_kind": "relationship",
                      "entities": ["Ada Byte", "Acme"], "confidence": "high"})
    client.app.state.claim_store = _GraphStore()
    r = client.post("/qa", json={"question": "how is Ada Byte connected to Acme?",
                                 "tenant_id": "demo"})
    assert r.status_code == 200
    data = r.json()
    assert data["grounded"] is True
    assert "worked at" in data["answer"]
    assert data["claims"][0]["quote"] == "Ada Byte was a staff engineer at Acme"
    assert svc.calls == []                          # graph answered; research not needed


def test_connection_no_path_falls_to_research_with_gap(monkeypatch):
    """Endpoints resolve but no grounded path → native research runs and the answer carries the
    graph-coverage gap (coverage honesty, not a universal no-relationship claim)."""
    class _NoPathStore:
        async def find_entity(self, ref, *, tenant_id):
            m = {"ada byte": {"entity_id": "github:ada", "name": "Ada Byte", "kind": "person"},
                 "acme": {"entity_id": "co:acme", "name": "Acme", "kind": "company"}}
            return m.get(ref.lower())

        async def neighbors(self, eid, *, tenant_id, relations=None, cap=400):
            return []

    svc = _AskSpy(answer="research prose about both")
    client = _client(monkeypatch, svc,
                     {"route": "connection_path", "subject_kind": "relationship",
                      "entities": ["Ada Byte", "Acme"], "confidence": "high"})
    client.app.state.claim_store = _NoPathStore()
    r = client.post("/qa", json={"question": "how is Ada Byte connected to Acme?",
                                 "tenant_id": "demo"})
    data = r.json()
    assert len(svc.calls) == 1                      # fell through to research
    assert any("graph-coverage gap" in g for g in data["coverage_gaps"])
    assert "CONNECTED" in (svc.calls[0].get("answer_format_override") or "")


def test_router_failure_fails_safe_to_research(monkeypatch):
    svc = _AskSpy()
    client = _client(monkeypatch, svc, RuntimeError("router LLM down"))
    r = client.post("/qa", json={"question": "anything at all", "tenant_id": "demo"})
    assert r.status_code == 200
    assert r.json()["answer"] == "GROUNDED PROSE"   # answered anyway
    assert r.json()["qa_route"]["route"] == "general_professional_qa"


def test_clarify_route_returns_clarification(monkeypatch):
    svc = _AskSpy()
    client = _client(monkeypatch, svc,
                     {"route": "clarify", "confidence": "high",
                      "clarification": "Do you mean the company Figma or a person named Figma?"})
    r = client.post("/qa", json={"question": "tell me about figma", "tenant_id": "demo"})
    data = r.json()
    assert data["clarification"].startswith("Do you mean")
    assert svc.calls == []


def test_jd_analysis_materializes_pasted_jd_as_document(monkeypatch):
    jd = ("Analyze this JD\n\nAbout the role\nSenior platform engineer.\n\nRequirements\n" +
          "- 5+ years Python\n- Kafka\n" + "filler line\n" * 40)
    svc = _AskSpy(answer="JD analysis")
    client = _client(monkeypatch, svc,
                     {"route": "jd_analysis", "subject_kind": "job", "confidence": "high"})
    r = client.post("/qa", json={"question": jd, "tenant_id": "demo"})
    assert r.status_code == 200
    call = svc.calls[0]
    docs = call.get("documents") or []
    assert docs and "Requirements" in docs[0]["text"]        # the JD rides as citable evidence
    assert "JOB DESCRIPTION" in (call.get("answer_format_override") or "")


def test_flag_off_is_legacy(monkeypatch):
    """ROSTER_QA_ROUTER off: /qa 404s without Eigen; people-population intercept still owns
    every question (byte-identical legacy)."""
    monkeypatch.delenv("ROSTER_QA_ROUTER", raising=False)
    monkeypatch.delenv("ROSTER_EIGEN_QA", raising=False)
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    svc = _AskSpy()
    client = TestClient(create_app(svc))
    assert client.post("/qa", json={"question": "x", "tenant_id": "demo"}).status_code == 404


def test_insights_route_dispatches_to_insights_engine(monkeypatch):
    svc = _AskSpy(answer="SHOULD NOT RUN")

    async def _fake_insights(**kw):
        return {"grounded": True, "abstain": False, "answer": "1. Google — 120 people",
                "rows": [{"value": "google", "display": "Google", "n": 120}],
                "coverage_basis": {"population_statement": "over the index"}}

    import api.people_population as pp
    monkeypatch.setattr(pp, "answer_roster_insights", _fake_insights)
    client = _client(monkeypatch, svc,
                     {"route": "insights", "confidence": "high"},
                     ROSTER_INSIGHTS_QA="1")
    client.app.state.claim_store = object()   # non-None: the engine itself is stubbed
    r = client.post("/qa", json={"question": "top companies by people in the index",
                                 "tenant_id": "demo"})
    data = r.json()
    assert data["grounded"] is True
    assert "Google" in data["answer"]
    assert svc.calls == []


def test_discovery_without_engine_stays_closed_world(monkeypatch):
    """Panel fix: an indexed-discovery ask on a deployment WITHOUT the people engine must return an
    honest 'not available' — never open-web people enumeration."""
    svc = _AskSpy(answer="WEB PEOPLE LIST — MUST NOT APPEAR")
    client = _client(monkeypatch, svc,
                     {"route": "indexed_people_discovery", "confidence": "high"})
    # ROSTER_PEOPLE_POPULATION deliberately unset
    r = client.post("/qa", json={"question": "find ML engineers in Berlin", "tenant_id": "demo"})
    data = r.json()
    assert svc.calls == []
    assert "isn't enabled" in data["answer"]
    assert data["grounded"] is False


def test_connection_path_drops_quoteless_hops(monkeypatch):
    """Panel fix: a path containing a hop WITHOUT a verbatim quote must not be answered as grounded
    — the graph route falls through to research instead of laundering the claim."""
    class _QuotelessStore:
        async def find_entity(self, ref, *, tenant_id):
            m = {"ada byte": {"entity_id": "github:ada", "name": "Ada Byte", "kind": "person"},
                 "acme": {"entity_id": "co:acme", "name": "Acme", "kind": "company"}}
            return m.get(ref.lower())

        async def neighbors(self, eid, *, tenant_id, relations=None, cap=400):
            edge = {"subject_id": "github:ada", "predicate": "worked_at", "object_id": "co:acme",
                    "claim_id": "c1", "citation": {"document_id": "d1", "block_id": "b1",
                                                   "quote": "", "authority_tier": 3}}
            return [edge] if eid in ("github:ada", "co:acme") else []

    svc = _AskSpy(answer="research prose")
    client = _client(monkeypatch, svc,
                     {"route": "connection_path", "subject_kind": "relationship",
                      "entities": ["Ada Byte", "Acme"], "confidence": "high"})
    client.app.state.claim_store = _QuotelessStore()
    r = client.post("/qa", json={"question": "how is Ada Byte connected to Acme?",
                                 "tenant_id": "demo"})
    data = r.json()
    assert len(svc.calls) == 1                       # fell through to research
    assert data["claims"] == [] or all(c.get("quote") for c in data["claims"])


def test_dossier_draws_person_profile_from_index(monkeypatch):
    """Index draw: a dossier run receives the person's grounded Roster-index profile as a citable
    per-request document."""
    class _PeopleStore:
        async def find_entity(self, ref, *, tenant_id):
            return {"entity_id": "github:jroe", "name": "Jane Roe", "kind": "person"}

        async def people_by_ids(self, ids, *, tenant_id):
            return [{"entity_id": "github:jroe", "name": "Jane Roe", "facets": [
                {"facet_key": "role", "facet_value_norm": "software_engineer",
                 "display_value": "Software Engineer", "document_id": "gh1", "block_id": "p"},
                {"facet_key": "company", "facet_value_norm": "acme",
                 "display_value": "Acme", "document_id": "gh1", "block_id": "p"}]}]

    svc = _AskSpy(answer="dossier prose")
    client = _client(monkeypatch, svc,
                     {"route": "person_dossier", "subject_kind": "person",
                      "entities": ["Jane Roe"], "confidence": "high"})
    client.app.state.claim_store = _PeopleStore()
    r = client.post("/qa", json={"question": "who is Jane Roe?", "tenant_id": "demo"})
    assert r.status_code == 200
    docs = svc.calls[0].get("documents") or []
    assert docs and "Roster people-index profile" in docs[0]["text"]
    assert "Software Engineer" in docs[0]["text"]
    assert "IDENTITY CAUTION" in docs[0]["text"]     # name-match honesty rides with the facts


def test_company_hiring_draws_open_roles_from_index(monkeypatch):
    class _JobsStore:
        async def search_jobs(self, *, terms=None, company=None, location=None, cap=60):
            assert company == ["Acme"]
            return [{"company": "Acme", "title": "Staff Engineer", "location": "NYC",
                     "url": "https://x/apply", "source": "greenhouse", "updated_at": "2026-09-01"}]

        async def jobs_stats(self):
            return {"jobs": 100, "companies": 10}

    svc = _AskSpy(answer="hiring prose")
    client = _client(monkeypatch, svc,
                     {"route": "company_hiring", "subject_kind": "company",
                      "entities": ["Acme"], "confidence": "high"})
    client.app.state.claim_store = _JobsStore()
    r = client.post("/qa", json={"question": "what is Acme like as an employer?",
                                 "tenant_id": "demo"})
    assert r.status_code == 200
    docs = svc.calls[0].get("documents") or []
    assert docs and "roster-index open roles: Acme" == docs[0]["name"]
    assert "Staff Engineer" in docs[0]["text"] and "as of 2026-09-01" in docs[0]["text"]


def test_people_surface_never_answers_questions(monkeypatch):
    """People tab is a SEARCH surface: a question-shaped input gets a no-cost redirect to Q&A —
    research never runs, no router LLM call is made."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse

    class _FacetOnlyLLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            assert response_format is _FacetParse, "router must NOT be consulted on the people surface"
            return LLMResult(parsed=_FacetParse(), output_tokens=5)   # {} facets, no person

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 3, "source_documents": 2, "facet_coverage": {}}

    svc = _AskSpy(answer="MUST NOT RUN")
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_POPULATION", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _FacetOnlyLLM())
    app = create_app(svc)
    app.state.claim_store = _Store()
    client = TestClient(app)
    r = client.post("/research", json={"question": "what are the ideal skills of a director of "
                                       "engineering?", "tenant_id": "demo", "surface": "people"})
    data = r.json()
    assert data["redirect_to_qa"] is True
    assert svc.calls == []


def test_candidates_for_jd_matches_and_analyzes(monkeypatch):
    jd = ("Who fits this role?\n\nAbout the role\nStaff payments engineer.\n\nRequirements\n"
          "- Distributed systems\n- Payments domain\n" + "filler requirement line\n" * 30)
    card = {"entity_id": "github:ada", "name": "Ada Byte", "match_pct": 87,
            "blurb": "Staff engineer, payments infra",
            "attributes": [{"key": "role", "display": "Staff Engineer"}], "links": []}

    async def _fake_match(store, jd_text, prefs):
        assert "Distributed systems" in jd_text
        return {"people_rows": [card]}

    import api.people_population as pp
    monkeypatch.setattr(pp, "match_jd_people", _fake_match)
    svc = _AskSpy(answer="critical fit analysis")
    client = _client(monkeypatch, svc,
                     {"route": "candidates_for_jd", "subject_kind": "job", "confidence": "high"})
    client.app.state.claim_store = object()
    r = client.post("/qa", json={"question": jd, "tenant_id": "demo"})
    data = r.json()
    assert data["answer"] == "critical fit analysis"
    assert data["people_rows"][0]["name"] == "Ada Byte"          # cards ride beside the analysis
    docs = svc.calls[0].get("documents") or []
    assert any("candidate matches" in d["name"] for d in docs)   # matches are citable evidence
    assert any("job-description" in d["name"] for d in docs)
    assert "recruiter-analyst" in (svc.calls[0].get("answer_format_override") or "")


def test_jobs_for_profile_matches_and_analyzes(monkeypatch):
    cv = ("Best roles for this resume?\n\nSummary: infra engineer.\n\nWork Experience\n"
          "Built Kafka pipelines at Acme.\n\nEducation\nBS CS\n\nSkills: Kafka, Go\n" + "resume detail line\n" * 30)

    async def _fake_match(store, profile, prefs):
        assert "Kafka" in profile["_resume_text"]
        return {"jobs": [{"title": "Staff Infra Engineer", "company": "Stripe", "location": "SF",
                          "url": "https://x/apply", "match_pct": 91, "reasons": ["role match"]}]}

    import api.people_population as pp
    monkeypatch.setattr(pp, "match_resume_jobs", _fake_match)
    svc = _AskSpy(answer="gems + ideal roles")
    client = _client(monkeypatch, svc,
                     {"route": "jobs_for_profile", "subject_kind": "person", "confidence": "high"})
    client.app.state.claim_store = object()
    r = client.post("/qa", json={"question": cv, "tenant_id": "demo"})
    assert r.json()["answer"] == "gems + ideal roles"
    docs = svc.calls[0].get("documents") or []
    assert any("job matches" in d["name"] for d in docs)
    assert any("resume" in d["name"] for d in docs)
    assert "career-analyst" in (svc.calls[0].get("answer_format_override") or "")


def test_people_refinement_follows_user_lead(monkeypatch):
    """Turn 2 in the People tab REFINES the running filter via the LLM refinement compile — the
    model returns the FULL updated filter (here: expansion of metro + kept role)."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse

    seen_filters = []

    class _RefineLLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            assert response_format is _FacetParse
            assert "REFINEMENT MODE" in messages[0]["content"]     # the refine prompt was used
            assert '"role": ["ml_engineer"]' in messages[0]["content"]
            return LLMResult(parsed=_FacetParse(role=["ml_engineer"], metro=["berlin", "munich"]),
                             output_tokens=5)

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 5, "source_documents": 2, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            seen_filters.append(facets)
            return [{"entity_id": "github:a", "name": "Ada", "facets": [
                {"facet_key": "role", "facet_value_norm": "ml_engineer",
                 "display_value": "ML Engineer", "document_id": "d", "block_id": "b"}]}]

    svc = _AskSpy(answer="MUST NOT RUN")
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_POPULATION", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_PEOPLE_SEMANTIC_FIRST", raising=False)
    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _RefineLLM())
    app = create_app(svc)
    app.state.claim_store = _Store()
    client = TestClient(app)
    r = client.post("/research", json={
        "question": "also include Munich", "tenant_id": "demo", "surface": "people",
        "refine_facets": {"role": ["ml_engineer"], "metro": ["berlin"]}})
    data = r.json()
    assert data["people_rows"], data
    assert seen_filters and seen_filters[0].get("metro") == ["berlin", "munich"]
    assert svc.calls == []


def test_identity_facets_gate_semantic_first(monkeypatch):
    """'people who worked at Apple' must HARD-filter worked_at=apple (hybrid path) even in
    semantic-first mode — never rank the whole index where academics dominate (the researcher-flood
    session 2a0302d3)."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse, answer_people_population

    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            return LLMResult(parsed=_FacetParse(worked_at=["apple"], seniority=["senior"]),
                             output_tokens=5)

    calls = {"scored": 0, "enum": 0}

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 10, "source_documents": 5, "facet_coverage": {}}

        async def match_people_scored(self, qvec, cap=500):
            calls["scored"] += 1          # whole-index semantic ranking — must NOT run
            return []

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            calls["enum"] += 1
            # identity facet gates: either the worked_at variant or the sparse-tag union's
            # company variant — never an ungated whole-index scan
            assert facets.get("worked_at") == ["apple"] or facets.get("company") == ["apple"]
            if facets.get("company"):
                return []                                   # union variant: no current-apple rows
            return [{"entity_id": "github:x", "name": "Xa", "facets": [
                {"facet_key": "worked_at", "facet_value_norm": "apple",
                 "display_value": "Apple", "document_id": "d", "block_id": "b"}]}]

        async def semantic_people(self, qvec, candidate_ids=None, cap=200):
            assert candidate_ids == ["github:x"]            # rank WITHIN the gated pool
            return ["github:x"]

        async def people_by_ids(self, ids, *, tenant_id):
            return [{"entity_id": "github:x", "name": "Xa", "facets": [
                {"facet_key": "worked_at", "facet_value_norm": "apple", "value_norm": "apple",
                 "display_value": "Apple", "document_id": "d", "block_id": "b"}]}]

    import api.people_population as pp
    monkeypatch.setenv("ROSTER_PEOPLE_SEMANTIC_FIRST", "1")
    monkeypatch.setattr(pp, "embed_query", lambda text: "[0.1,0.2]")
    import asyncio
    res = asyncio.get_event_loop().run_until_complete(answer_people_population(
        question="people who worked at Apple for more than 5 years",
        tenant_id="demo", store=_Store(), llm=_LLM()))
    assert calls["scored"] == 0 and calls["enum"] >= 1
    assert res["people_rows"] and res["people_rows"][0]["name"] == "Xa"


def test_primary_role_leads_the_ranking(monkeypatch):
    """'Data scientists at Google': people whose PRIMARY role is data_scientist must lead — a
    multi-tagged SWE who merely carries the tag follows (session 0cb80174)."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse, answer_people_population

    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            return LLMResult(parsed=_FacetParse(role=["data_scientist"], company=["google"]),
                             output_tokens=5)

    def _person(eid, name, roles):
        return {"entity_id": eid, "name": name, "facets": [
            {"facet_key": "role", "facet_value_norm": r, "value_norm": r,
             "display_value": r.replace("_", " ").title(), "document_id": "d", "block_id": "b"}
            for r in roles]}

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 9, "source_documents": 3, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            # store order simulates semantic/prominence order that buried the pure data scientist
            return [_person("gh:swe", "Sam SWE", ["software_engineer", "data_scientist"]),
                    _person("gh:ml", "Mia ML", ["ml_engineer", "data_scientist"]),
                    _person("gh:ds", "Dana DS", ["data_scientist"])]

    import asyncio
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_PEOPLE_SEMANTIC_FIRST", raising=False)
    res = asyncio.get_event_loop().run_until_complete(answer_people_population(
        question="Data scientists at Google", tenant_id="demo", store=_Store(), llm=_LLM()))
    names = [p["name"] for p in res["people_rows"]]
    assert names[0] == "Dana DS", names          # primary data scientist leads
    assert set(names[1:]) == {"Sam SWE", "Mia ML"}


def test_geo_scope_keeps_unknown_country_people(monkeypatch):
    """The injected geo-scope country must not require the country FACET: an Apple alum with no
    country tag stays in the results; a known-foreign one drops (session b5353056)."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse, answer_people_population

    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            return LLMResult(parsed=_FacetParse(worked_at=["apple"]), output_tokens=5)

    def _p(eid, name, country=None):
        fs = [{"facet_key": "worked_at", "facet_value_norm": "apple", "value_norm": "apple",
               "display_value": "Apple", "document_id": "d", "block_id": "b"}]
        if country:
            fs.append({"facet_key": "country", "facet_value_norm": country, "value_norm": country,
                       "display_value": country.upper(), "document_id": "d", "block_id": "b"})
        return {"entity_id": eid, "name": name, "facets": fs}

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 9, "source_documents": 3, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            assert "country" not in facets          # country never hard-gates the enumeration
            return [_p("a", "US Alum", "us"), _p("b", "Untagged Alum"), _p("c", "DE Alum", "de")]

    import asyncio
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_PEOPLE_SEMANTIC_FIRST", raising=False)
    res = asyncio.get_event_loop().run_until_complete(answer_people_population(
        question="people who worked at Apple", tenant_id="demo", store=_Store(), llm=_LLM(),
        scope_country="us"))
    names = {p["name"] for p in res["people_rows"]}
    assert names == {"US Alum", "Untagged Alum"}    # unknown kept, known-foreign dropped


def test_empty_insights_falls_through_to_research_with_gap(monkeypatch):
    """An insights-routed question the index can't support (zero rows) answers from open-world
    research with the index gap disclosed — never a dead-end 'no match' (session 34f20ddb)."""
    svc = _AskSpy(answer="AI skills in demand, from grounded research")

    async def _empty_insights(**kw):
        return {"grounded": False, "abstain": False, "rows": [], "coverage_basis": {},
                "answer": "No people in Roster's index match that yet."}

    import api.people_population as pp
    monkeypatch.setattr(pp, "answer_roster_insights", _empty_insights)
    client = _client(monkeypatch, svc, {"route": "insights", "confidence": "high"},
                     ROSTER_INSIGHTS_QA="1")
    client.app.state.claim_store = object()
    r = client.post("/qa", json={"question": "Top AI skills for engineers in demand",
                                 "tenant_id": "demo"})
    data = r.json()
    assert len(svc.calls) == 1                       # research ran
    assert data["answer"].startswith("AI skills in demand")
    assert any("couldn't support" in g for g in data["coverage_gaps"])


def test_worked_at_unions_current_company(monkeypatch):
    """'People who worked at Apple' includes people currently there: worked_at matches lead,
    company=apple people follow, and the coverage discloses the union (worked_at is sparse)."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse, answer_people_population

    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            return LLMResult(parsed=_FacetParse(worked_at=["apple"]), output_tokens=5)

    def _p(eid, name, key, val):
        return {"entity_id": eid, "name": name, "facets": [
            {"facet_key": key, "facet_value_norm": val, "value_norm": val,
             "display_value": val.title(), "document_id": "d", "block_id": "b"}]}

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 9, "source_documents": 3, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            if "worked_at" in facets:
                return [_p("gh:alum", "Ada Alum", "worked_at", "apple")]
            assert facets.get("company") == ["apple"]
            return [_p("gh:cur", "Cara Current", "company", "apple"),
                    _p("gh:alum", "Ada Alum", "worked_at", "apple")]   # dedup by entity_id

    import asyncio
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_PEOPLE_SEMANTIC_FIRST", raising=False)
    res = asyncio.get_event_loop().run_until_complete(answer_people_population(
        question="people who worked at Apple", tenant_id="demo", store=_Store(), llm=_LLM()))
    names = [p["name"] for p in res["people_rows"]]
    assert names[0] == "Ada Alum" and "Cara Current" in names and len(names) == 2
    assert "CURRENTLY at the named company" in res["coverage_basis"]["population_statement"]


def test_foreign_metro_counts_as_foreign_under_scope(monkeypatch):
    """metro=berlin with NO country facet is KNOWN-foreign under the us scope (metro→country map);
    truly-untagged people stay; confirmed-us people lead the ordering."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse, answer_people_population

    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            return LLMResult(parsed=_FacetParse(role=["ml_engineer"]), output_tokens=5)

    def _p(eid, name, extra=()):
        fs = [{"facet_key": "role", "facet_value_norm": "ml_engineer", "value_norm": "ml_engineer",
               "display_value": "Ml Engineer", "document_id": "d", "block_id": "b"}]
        for k, v in extra:
            fs.append({"facet_key": k, "facet_value_norm": v, "value_norm": v,
                       "display_value": v, "document_id": "d", "block_id": "b"})
        return {"entity_id": eid, "name": name, "facets": fs}

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 9, "source_documents": 3, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            return [_p("a", "Berlin Untagged", (("metro", "berlin"),)),
                    _p("b", "Truly Unknown"),
                    _p("c", "US Person", (("country", "us"),))]

    import asyncio
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_PEOPLE_SEMANTIC_FIRST", raising=False)
    res = asyncio.get_event_loop().run_until_complete(answer_people_population(
        question="ML engineers", tenant_id="demo", store=_Store(), llm=_LLM(),
        scope_country="us"))
    names = [p["name"] for p in res["people_rows"]]
    assert "Berlin Untagged" not in names          # metro implies the country → dropped
    assert names[0] == "US Person"                  # confirmed-scope leads
    assert "Truly Unknown" in names                 # untagged kept (recall)


def test_jobs_endpoint_honors_country_scope(monkeypatch):
    """/jobs drops clearly-foreign locations under the selector scope; remote/unknown stays."""
    class _JobsStore:
        async def search_jobs(self, *, terms=None, company=None, location=None, cap=60):
            return [{"company": "Bosch", "title": "Engineer", "location": "Stuttgart, Germany",
                     "url": "u", "source": "smartrecruiters"},
                    {"company": "Stripe", "title": "Engineer", "location": "San Francisco, CA",
                     "url": "u", "source": "greenhouse"},
                    {"company": "Acme", "title": "Engineer", "location": "Remote",
                     "url": "u", "source": "lever"}]

        async def jobs_stats(self):
            return {"jobs": 3, "companies": 3}

    monkeypatch.setenv("ROSTER_JOBS", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_GEO_SCOPE", "1")
    monkeypatch.delenv("ROSTER_QA_ROUTER", raising=False)
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_AGENTIC_JOBS", raising=False)

    class _JobLLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            from roster_kernel.providers.llm import LLMResult
            return LLMResult(parsed=response_format(title_keywords=["engineer"]), output_tokens=5)

    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _JobLLM())
    app = create_app(_AskSpy())
    app.state.claim_store = _JobsStore()
    client = TestClient(app)
    r = client.post("/jobs", json={"question": "engineering jobs", "tenant_id": "demo",
                                   "country": "us"})
    locs = [j["location"] for j in r.json()["jobs"]]
    assert "Stuttgart, Germany" not in locs
    assert "San Francisco, CA" in locs and "Remote" in locs


def test_followup_discovery_gets_conversation_context(monkeypatch):
    """A follow-up like 'example people for these roles' carries no facets alone — the discovery
    engine's question must be enriched with the prior turns so the compiler resolves the reference
    (cards, not research prose; session 4efafe24 turn 2)."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse

    class _TwoStageLLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            if response_format is _FacetParse:
                # the compiler must SEE the conversation context, not just the bare follow-up
                assert "AI Engineers" in messages[0]["content"]
                return LLMResult(parsed=_FacetParse(role=["ml_engineer"]), output_tokens=5)
            return LLMResult(parsed=response_format(route="indexed_people_discovery",
                                                    confidence="high"), output_tokens=5)

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 5, "source_documents": 2, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            return [{"entity_id": "gh:a", "name": "Ada", "facets": [
                {"facet_key": "role", "facet_value_norm": "ml_engineer", "value_norm": "ml_engineer",
                 "display_value": "ML Engineer", "document_id": "d", "block_id": "b"}]}]

    svc = _AskSpy(answer="PROSE MUST NOT WIN")
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_POPULATION", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    monkeypatch.delenv("ROSTER_SEMANTIC", raising=False)
    monkeypatch.delenv("ROSTER_PEOPLE_SEMANTIC_FIRST", raising=False)
    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _TwoStageLLM())
    app = create_app(svc)
    app.state.claim_store = _Store()
    client = TestClient(app)
    r = client.post("/qa", json={
        "question": "Provide me with some example people profiles that fit these roles",
        "tenant_id": "demo",
        "history": [{"question": "Which companies are hiring a lot of AI Engineers that do LLM "
                                 "based coding?", "answer": "40 roles", "route": "indexed_job_search"}]})
    data = r.json()
    assert data["people_rows"] and data["people_rows"][0]["name"] == "Ada"
    assert svc.calls == []                       # cards, not prose


def test_router_asserted_discovery_falls_back_to_semantic_cards(monkeypatch):
    """A discovery-routed follow-up that compiles to NO facets ('example people for these roles')
    must serve semantic people CARDS, never research prose (busted-follow-up session 7e1123b6)."""
    from roster_kernel.providers.llm import LLMResult
    from api.people_population import _FacetParse

    class _TwoStageLLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            if response_format is _FacetParse:
                return LLMResult(parsed=_FacetParse(), output_tokens=5)   # compiler yields {}
            return LLMResult(parsed=response_format(route="indexed_people_discovery",
                                                    subject_kind="person",
                                                    axes=["AI Engineers", "LLM based coding"],
                                                    confidence="high"), output_tokens=5)

    class _Store:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 5, "source_documents": 2, "facet_coverage": {}}

        async def match_people_scored(self, qvec, cap=500):
            return [{"entity_id": "gh:a", "sim": 0.9}]

        async def people_by_ids(self, ids, *, tenant_id):
            return [{"entity_id": "gh:a", "name": "Ada", "facets": [
                {"facet_key": "role", "facet_value_norm": "ml_engineer", "value_norm": "ml_engineer",
                 "display_value": "ML Engineer", "document_id": "d", "block_id": "b"}]}]

    import api.people_population as pp
    svc = _AskSpy(answer="PROSE MUST NOT WIN")
    monkeypatch.setenv("ROSTER_QA_ROUTER", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_POPULATION", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_SEMANTIC_FIRST", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _TwoStageLLM())
    monkeypatch.setattr(pp, "embed_query", lambda text: "[0.1,0.2]")
    app = create_app(svc)
    app.state.claim_store = _Store()
    client = TestClient(app)
    r = client.post("/qa", json={
        "question": "Give me some example people profiles that suit above roles",
        "tenant_id": "demo",
        "history": [{"question": "Which companies are hiring AI Engineers?", "answer": "35 roles",
                     "route": "indexed_job_search"}]})
    data = r.json()
    assert data["people_rows"] and data["people_rows"][0]["name"] == "Ada"
    assert svc.calls == []                       # cards, never prose


def test_match_jobs_rotates_seen_and_diversifies_companies(monkeypatch):
    """Find-jobs variety: previously-shown ids are DEMOTED (fresh comparable matches lead, seen ones
    stay available), and no company floods the slate (max 3 lead rows per company)."""
    from api.people_population import match_resume_jobs
    import api.people_population as pp

    def _j(i, company, sim):
        return {"id": i, "company": company, "title": f"Engineer {i}", "location": "Remote",
                "url": "u", "source": "greenhouse", "sim": sim}

    class _Store:
        async def match_jobs_scored(self, qvec, cap=400):
            # acme floods with 5 near-identical top matches; two others trail slightly
            return ([_j(i, "acme", 0.90 - i * 0.001) for i in range(5)]
                    + [_j(10, "globex", 0.88), _j(11, "initech", 0.87)])

        async def companies_with_facet(self, keys, values=None):
            return set()

    monkeypatch.setattr(pp, "embed_query", lambda text: "[0.1]")
    import asyncio
    run = asyncio.get_event_loop().run_until_complete

    res1 = run(match_resume_jobs(_Store(), {"_resume_text": "python engineer"}, {"limit": 5}))
    ids1 = [j["id"] for j in res1["jobs"]]
    assert ids1[:4] == [0, 1, 2, 10]          # company cap: only 3 acme rows lead, globex breaks in
    assert res1["rotated"] is False

    res2 = run(match_resume_jobs(_Store(), {"_resume_text": "python engineer"},
                                 {"limit": 5, "seen_ids": ids1}))
    ids2 = [j["id"] for j in res2["jobs"]]
    assert res2["rotated"] is True
    assert ids2[0] not in ids1[:1] or ids2 != ids1   # the slate actually changed
    assert set(ids2[:2]) & {4, 11, 3}                # unseen jobs rose to the top
    assert "rotating" in res2["note"]


def test_match_jobs_dedupes_excludes_and_leads_with_named_roles(monkeypatch):
    """Variety round 2: cross-source duplicate listings collapse to one slot; excluded title words
    never appear; user-named role keywords LEAD the slate over semantic look-alikes."""
    from api.people_population import match_resume_jobs
    import api.people_population as pp

    def _j(i, company, title, sim):
        return {"id": i, "company": company, "title": title, "location": "Remote",
                "url": "u", "source": "s", "sim": sim}

    class _Store:
        async def match_jobs_scored(self, qvec, cap=400):
            return [_j(1, "Cloudflare", "Senior Software Engineer - Fintech", 0.90),
                    _j(2, "cloudflare", "Senior Software Engineer - Fintech", 0.899),  # same job, 2nd source
                    _j(3, "acme", "Sales Engineer", 0.89),                              # excluded
                    _j(4, "globex", "Staff Payments Engineer", 0.88),
                    _j(5, "initech", "Data Analyst", 0.87)]

        async def companies_with_facet(self, keys, values=None):
            return set()

    monkeypatch.setattr(pp, "embed_query", lambda text: "[0.1]")
    import asyncio
    res = asyncio.get_event_loop().run_until_complete(match_resume_jobs(
        _Store(), {"_resume_text": "payments engineer"},
        {"limit": 10, "exclude_keywords": ["sales"], "role_keywords": ["payments"]}))
    titles = [j["title"] for j in res["jobs"]]
    ids = [j["id"] for j in res["jobs"]]
    assert ids.count(1) + ids.count(2) == 1          # cross-source duplicate collapsed to one slot
    assert 3 not in ids                              # excluded word never shows
    assert titles[0] == "Staff Payments Engineer"    # user-named role LEADS over higher-sim look-alikes
    assert "excluded by your title filters" in res["note"]


def test_rotation_counts_twin_rows_as_seen(monkeypatch):
    """A twin row of an already-seen job (same company|title via another source/id) must be demoted
    too — rotating by row id alone re-served the identical posting under a fresh id."""
    from api.people_population import match_resume_jobs
    import api.people_population as pp

    def _j(i, company, title, sim):
        return {"id": i, "company": company, "title": title, "location": "Remote",
                "url": "u", "source": "s", "sim": sim}

    class _Store:
        async def match_jobs_scored(self, qvec, cap=400):
            return [_j(1, "Stripe", "Backend Engineer, Payments", 0.90),
                    _j(2, "stripe", "Backend Engineer, Payments", 0.899),   # twin, other source
                    _j(3, "globex", "Staff Payments Engineer", 0.88)]

        async def companies_with_facet(self, keys, values=None):
            return set()

    monkeypatch.setattr(pp, "embed_query", lambda text: "[0.1]")
    import asyncio
    run = asyncio.get_event_loop().run_until_complete
    r1 = run(match_resume_jobs(_Store(), {"_resume_text": "payments"}, {"limit": 1}))
    seen = [j["key"] for j in r1["jobs"]]           # FE now remembers the job KEY
    assert r1["jobs"][0]["title"] == "Backend Engineer, Payments"
    r2 = run(match_resume_jobs(_Store(), {"_resume_text": "payments"},
                               {"limit": 2, "seen_ids": seen}))
    assert r2["jobs"][0]["title"] == "Staff Payments Engineer"   # twin row did NOT lead again
