"""Offline API test — /research returns a grounded, cited answer.

Injects a ResearchService wired with consistent fake providers (scripted LLM +
FakeEmbedder + the kernel's domain-neutral in-memory source), so the full
HTTP → agent → citations path is exercised without credits or a vertical package.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.react import AgentStep, AnswerResult, ClaimOut
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.runtime.research import ResearchService

from api.app import create_app


class _LLM:
    def __init__(self, steps): self._s = list(steps)
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._s.pop(0), output_tokens=5)


def _source() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="acme",
        text="Metformin is recommended as first-line therapy for type 2 diabetes.",
        locator=Locator("block_span", "d1", {"block_id": "b1"}), source_key="corpus"))
    return src


def _service() -> ResearchService:
    emb = FakeEmbedder(dim=16)
    llm = _LLM([
        AgentStep(action="search", query="first-line therapy type 2 diabetes"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="metformin is first-line for type 2 diabetes", atom_id="a1",
                     quote="first-line therapy for type 2 diabetes")]),
    ])
    return ResearchService(llm=llm, embedder=emb, sources={"corpus": _source()})


def test_health() -> None:
    client = TestClient(create_app(_service()))
    assert client.get("/health").json() == {"status": "ok"}


def test_research_returns_grounded_answer() -> None:
    client = TestClient(create_app(_service()))
    resp = client.post("/research", json={
        "question": "what is first-line therapy for type 2 diabetes?",
        "tenant_id": "acme", "sources": ["corpus"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["grounded"] is True
    assert data["rejected"] == 0
    assert len(data["claims"]) == 1
    assert data["claims"][0]["quote"] == "first-line therapy for type 2 diabetes"


def test_research_returns_people_profiles(monkeypatch) -> None:
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    profile = {"name": "Jane Roe", "url": "https://github.com/janeroe", "host": "github.com"}

    class _PeopleService:
        ui = None

        async def ask(self, **kwargs):
            return AnswerResult(composed_answer="", people_profiles=[profile])

    client = TestClient(create_app(_PeopleService()))
    resp = client.post("/research", json={
        "question": "tell me about Jane Roe",
        "tenant_id": "acme", "sources": ["corpus"]})
    assert resp.status_code == 200
    assert resp.json()["people"] == [profile]


def test_research_stream_final_returns_people_profiles(monkeypatch) -> None:
    monkeypatch.setenv("ROSTER_STREAM", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)
    profile = {"name": "Jane Roe", "url": "https://github.com/janeroe", "host": "github.com"}

    class _PeopleService:
        ui = None

        async def ask(self, **kwargs):
            return AnswerResult(composed_answer="", people_profiles=[profile])

    client = TestClient(create_app(_PeopleService()))
    with client.stream("POST", "/research/stream", json={
        "question": "tell me about Jane Roe",
        "tenant_id": "acme", "sources": ["corpus"]}) as resp:
        assert resp.status_code == 200
        events = [line.removeprefix("data: ") for line in resp.iter_lines()
                  if line.startswith("data: ")]
    final = [json.loads(e) for e in events if json.loads(e).get("type") == "final"][0]
    assert final["result"]["people"] == [profile]


def test_research_stream_people_population_returns_rows(monkeypatch) -> None:
    """Held-out regression (Rule 4) for the recurring 'still prose' bug: with
    ROSTER_PEOPLE_POPULATION on, /research/stream — the UI's REAL endpoint — MUST return grounded
    people_rows in its final event, NEVER web prose. The earlier fix only patched /research; the bug
    survived because the UI streams. This pins the streaming path that both endpoints now share
    (people routing lives in _do_research). The injected service would emit PROSE if the people
    route were ever bypassed, so a regression fails loudly here."""
    monkeypatch.setenv("ROSTER_STREAM", "1")
    monkeypatch.setenv("ROSTER_PEOPLE_POPULATION", "1")
    monkeypatch.delenv("ROSTER_REASONED_DEFAULT", raising=False)

    import api.app as appmod
    from api.people_population import _FacetParse

    class _FacetLLM:   # the query-COMPILER: free text → normalized facet filter
        async def complete(self, *, system, messages, response_format, max_tokens=2048,
                           temperature=None):
            return LLMResult(parsed=_FacetParse(role=["software_engineer"], function=["payment"]),
                             output_tokens=5)

    class _FakeStore:
        async def people_index_stats(self, *, tenant_id):
            return {"persons_indexed": 3, "source_documents": 2, "facet_coverage": {}}

        async def enumerate_by_facets(self, facets, *, tenant_id, cap):
            return [{"entity_id": "github:se1", "name": "Ada Byte", "facets": [
                {"facet_key": "role", "facet_value_norm": "software_engineer",
                 "display_value": "Software Engineer", "document_id": "gh1", "block_id": "profile"},
                {"facet_key": "link_github", "facet_value_norm": "https://github.com/adabyte",
                 "display_value": "https://github.com/adabyte", "document_id": "gh1",
                 "block_id": "profile"}]}]

    monkeypatch.setattr(appmod, "build_llm", lambda *a, **k: _FacetLLM())

    class _ProseService:   # would win ONLY if the people route were bypassed (the bug)
        ui = None

        async def ask(self, **kwargs):
            return AnswerResult(composed_answer="PROSE ABOUT PAYMENTS", people_profiles=[])

    app = create_app(_ProseService())
    app.state.claim_store = _FakeStore()          # the slot _claim_store_cached() reads/caches
    client = TestClient(app)
    with client.stream("POST", "/research/stream", json={
            "question": "Software engineers in payment industry", "tenant_id": "demo"}) as resp:
        assert resp.status_code == 200
        events = [line.removeprefix("data: ") for line in resp.iter_lines()
                  if line.startswith("data: ")]
    final = [json.loads(e) for e in events if json.loads(e).get("type") == "final"][0]
    res = final["result"]
    assert len(res["people_rows"]) == 1                       # grounded rows, not prose
    assert res["people_rows"][0]["name"] == "Ada Byte"
    assert "PROSE ABOUT PAYMENTS" not in (res.get("answer") or "")   # the prose path never ran


def test_tenant_isolation_via_api() -> None:
    # A different tenant sees no evidence → not grounded (no leak).
    client = TestClient(create_app(_service()))
    resp = client.post("/research", json={
        "question": "what is first-line therapy for type 2 diabetes?",
        "tenant_id": "intruder", "sources": ["corpus"]})
    assert resp.status_code == 200
    assert resp.json()["grounded"] is False


def test_evidence_identity_flag_reads_env(monkeypatch) -> None:
    # Evidence Contract stage 1: the flag is wired from ROSTER_EVIDENCE_IDENTITY (default OFF).
    from api.app import evidence_identity_enabled
    monkeypatch.delenv("ROSTER_EVIDENCE_IDENTITY", raising=False)
    assert evidence_identity_enabled() is False
    monkeypatch.setenv("ROSTER_EVIDENCE_IDENTITY", "1")
    assert evidence_identity_enabled() is True
    monkeypatch.setenv("ROSTER_EVIDENCE_IDENTITY", "false")
    assert evidence_identity_enabled() is False


def test_claim_congruence_flag_reads_env(monkeypatch) -> None:
    # Evidence Contract stage 2: the flag is wired from ROSTER_CLAIM_CONGRUENCE (default OFF).
    from api.app import claim_congruence_enabled
    monkeypatch.delenv("ROSTER_CLAIM_CONGRUENCE", raising=False)
    assert claim_congruence_enabled() is False
    monkeypatch.setenv("ROSTER_CLAIM_CONGRUENCE", "1")
    assert claim_congruence_enabled() is True
    monkeypatch.setenv("ROSTER_CLAIM_CONGRUENCE", "false")
    assert claim_congruence_enabled() is False


def test_question_contract_mode_reads_env(monkeypatch) -> None:
    # Evidence Contract stage 3: ROSTER_QUESTION_CONTRACT is a MODE string (mirrors
    # ROSTER_GRAPH_EXPAND): "" off (default), "shadow", "steer"; anything else → off.
    from api.app import question_contract_mode
    monkeypatch.delenv("ROSTER_QUESTION_CONTRACT", raising=False)
    assert question_contract_mode() == ""
    monkeypatch.setenv("ROSTER_QUESTION_CONTRACT", "shadow")
    assert question_contract_mode() == "shadow"
    monkeypatch.setenv("ROSTER_QUESTION_CONTRACT", "STEER")
    assert question_contract_mode() == "steer"
    monkeypatch.setenv("ROSTER_QUESTION_CONTRACT", "1")     # bare truthy is NOT a mode
    assert question_contract_mode() == ""


def test_answer_mode_routing_flag_reads_env(monkeypatch) -> None:
    # Evidence Contract stage 4: the flag is wired from ROSTER_ANSWER_MODE_ROUTING (default OFF).
    from api.app import answer_mode_routing_enabled
    monkeypatch.delenv("ROSTER_ANSWER_MODE_ROUTING", raising=False)
    assert answer_mode_routing_enabled() is False
    monkeypatch.setenv("ROSTER_ANSWER_MODE_ROUTING", "1")
    assert answer_mode_routing_enabled() is True
    monkeypatch.setenv("ROSTER_ANSWER_MODE_ROUTING", "no")
    assert answer_mode_routing_enabled() is False


# ---------------------------------------------------------------- Guided Intake v2


def test_intake_v2_flag_reads_env(monkeypatch) -> None:
    # Guided Intake v2: the flag is wired from ROSTER_INTAKE_V2 (default OFF → v1 byte-identical).
    from api.app import intake_v2_enabled
    monkeypatch.delenv("ROSTER_INTAKE_V2", raising=False)
    assert intake_v2_enabled() is False
    monkeypatch.setenv("ROSTER_INTAKE_V2", "1")
    assert intake_v2_enabled() is True
    monkeypatch.setenv("ROSTER_INTAKE_V2", "false")
    assert intake_v2_enabled() is False


def test_triage_ask_cap_per_register() -> None:
    # v1 → always TRIAGE_MAX_ASK; v2 → fact keeps it, case (and an absent/unknown echo) gets the
    # case backstop. Defaults: TRIAGE_MAX_ASK=2, TRIAGE_MAX_ASK_CASE=8.
    from api.app import TRIAGE_MAX_ASK, TRIAGE_MAX_ASK_CASE, triage_ask_cap
    assert triage_ask_cap(False, "fact") == TRIAGE_MAX_ASK
    assert triage_ask_cap(False, "case") == TRIAGE_MAX_ASK
    assert triage_ask_cap(False, "") == TRIAGE_MAX_ASK
    assert triage_ask_cap(True, "fact") == TRIAGE_MAX_ASK
    assert triage_ask_cap(True, "case") == TRIAGE_MAX_ASK_CASE
    assert triage_ask_cap(True, "") == TRIAGE_MAX_ASK_CASE          # lost echo → fail-open
    assert triage_ask_cap(True, "weird") == TRIAGE_MAX_ASK_CASE


class _RecordingLLM:
    """Returns scripted parsed turns and records every call for schema/prompt assertions."""

    def __init__(self, turns):
        self._t = list(turns)
        self.calls = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.calls.append({"system": system, "messages": messages,
                           "response_format": response_format})
        return LLMResult(parsed=self._t.pop(0), model="fake")


def _triage_service(llm) -> ResearchService:
    return ResearchService(llm=llm, embedder=FakeEmbedder(dim=16), sources={},
                           triage_prompt="V1-DIRECTIVE", triage_prompt_v2="V2-DIRECTIVE")


def test_triage_wrap_up_forces_ready(monkeypatch) -> None:
    # wrap_up:true → force_ready plumbing (v1 mode): the model wanted to keep asking, but the
    # user's wrap-up coerces a route this turn (and the force instruction reached the LLM).
    from roster_kernel.research.triage import TriageTurn
    monkeypatch.setenv("ROSTER_TRIAGE", "1")
    monkeypatch.delenv("ROSTER_INTAKE_V2", raising=False)
    monkeypatch.delenv("ROSTER_CORPUS_DSN", raising=False)   # env-only flag resolution (no DB)
    llm = _RecordingLLM([TriageTurn(status="ask", message="one more?",
                                    understood_problem="knee pain after running")])
    client = TestClient(create_app(_triage_service(llm)))
    resp = client.post("/triage/step", json={
        "transcript": [{"role": "user", "text": "my knee hurts after running"}], "wrap_up": True})
    assert resp.status_code == 200
    d = resp.json()
    assert d["status"] == "ready"                                 # coerced despite the model's "ask"
    assert d["refined_question"] == "knee pain after running"
    assert llm.calls[0]["response_format"] is TriageTurn          # v1 schema without the v2 flag
    assert llm.calls[0]["system"] == "V1-DIRECTIVE"
    assert "do NOT ask another question" in llm.calls[0]["messages"][-1]["content"]


def test_triage_v2_uses_v2_prompt_schema_and_case_cap(monkeypatch) -> None:
    # Under ROSTER_INTAKE_V2: the v2 directive + TriageTurnV2 schema are selected, the new fields
    # come back in the response, and the ask cap is per-register (2 assistant asks: fact → forced,
    # absent register → case cap → NOT forced).
    from roster_kernel.research.triage import TriageTurnV2
    monkeypatch.setenv("ROSTER_TRIAGE", "1")
    monkeypatch.setenv("ROSTER_INTAKE_V2", "1")
    monkeypatch.delenv("ROSTER_CORPUS_DSN", raising=False)   # env-only flag resolution (no DB)
    turns = [TriageTurnV2(status="ask", message="So I can search the right evidence — when did it start?",
                          register="case",
                          case_facts=[{"category": "core-issue", "text": "knee pain"}],
                          retrieval_terms=["knee pain"]),
             TriageTurnV2(status="ask", message="q", register="fact")]
    llm = _RecordingLLM(turns)
    client = TestClient(create_app(_triage_service(llm)))
    two_asks = [{"role": "user", "text": "my knee hurts"},
                {"role": "assistant", "text": "q1"}, {"role": "user", "text": "a1"},
                {"role": "assistant", "text": "q2"}, {"role": "user", "text": "a2"}]
    # absent register under v2 → case cap (8) → 2 asks do NOT force a route
    d = client.post("/triage/step", json={"transcript": two_asks}).json()
    assert d["status"] == "ask"
    assert d["register"] == "case"                                # new fields echoed in the response
    assert d["case_facts"] == [{"category": "core-issue", "text": "knee pain"}]
    assert d["retrieval_terms"] == ["knee pain"]
    assert llm.calls[0]["response_format"] is TriageTurnV2
    assert llm.calls[0]["system"] == "V2-DIRECTIVE"
    assert all("do NOT ask another question" not in m["content"] for m in llm.calls[0]["messages"])
    # fact register echoed back → v1 cap (2) → the same 2 asks DO force a route
    d = client.post("/triage/step", json={"transcript": two_asks, "register": "fact"}).json()
    assert d["status"] == "ready"
    assert "do NOT ask another question" in llm.calls[1]["messages"][-1]["content"]


# ---------------------------------------------------------------- Specialist Panel upgrade (P1–P3)


def test_panel_upgrade_flags_read_env(monkeypatch) -> None:
    # Panel upgrade: P2 dedup and P3+P1 shared-contract flags are wired from
    # ROSTER_PANEL_DEDUP / ROSTER_PANEL_CONTRACT (both default OFF).
    from api.app import panel_contract_enabled, panel_dedup_enabled
    monkeypatch.delenv("ROSTER_PANEL_DEDUP", raising=False)
    monkeypatch.delenv("ROSTER_PANEL_CONTRACT", raising=False)
    assert panel_dedup_enabled() is False and panel_contract_enabled() is False
    monkeypatch.setenv("ROSTER_PANEL_DEDUP", "1")
    monkeypatch.setenv("ROSTER_PANEL_CONTRACT", "true")
    assert panel_dedup_enabled() is True and panel_contract_enabled() is True
    monkeypatch.setenv("ROSTER_PANEL_DEDUP", "no")
    monkeypatch.setenv("ROSTER_PANEL_CONTRACT", "0")
    assert panel_dedup_enabled() is False and panel_contract_enabled() is False


def test_panel_payload_carries_coverage_gaps(monkeypatch) -> None:
    # P3c plumbing: PanelResult.coverage_gaps → the /panel/ask payload (the same payload feeds the
    # session turn), so the UI/session can show the panel-level gaps.
    from roster_kernel.research.panel import PanelResult
    monkeypatch.setenv("ROSTER_ASK_PANEL", "1")
    monkeypatch.delenv("ROSTER_CORPUS_DSN", raising=False)   # env-only flags, no session store
    svc = _service()

    async def fake_ask_panel(**kw):
        return PanelResult(question=kw["question"], synthesis="Panel answer [1].",
                           claims=[{"text": "t", "quote": "q", "atom_id": "a1", "source": "corpus",
                                    "title": "", "document_id": "d1", "evidence_kind": ""}],
                           n_specialists=2,
                           coverage_gaps=["No specialist retrieved evidence for beta-drug"])

    svc.ask_panel = fake_ask_panel
    client = TestClient(create_app(svc))
    resp = client.post("/panel/ask", json={"question": "q", "tenant_id": "acme"})
    assert resp.status_code == 200
    d = resp.json()
    assert d["coverage_gaps"] == ["No specialist retrieved evidence for beta-drug"]
    assert d["synthesis"] == "Panel answer [1]."


# ---- ✦ term glossary (ROSTER_TERM_GLOSSARY) ----

def _terms_service(llm) -> ResearchService:
    svc = _service()
    svc.llm = llm
    svc.terms_prompt = "explain medical terms"
    return svc


def test_terms_endpoints_404_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("ROSTER_TERM_GLOSSARY", raising=False)
    client = TestClient(create_app(_service()))
    assert client.post("/terms/explain", json={"answer": "eGFR matters"}).status_code == 404
    assert client.post("/glossary/lookup", json={"term": "eGFR"}).status_code == 404
    assert client.get("/glossary").status_code == 404


def test_terms_explain_returns_terms_and_config_echo(monkeypatch) -> None:
    from roster_kernel.research.terms import TermExplanation, TermExplanations
    monkeypatch.setenv("ROSTER_TERM_GLOSSARY", "1")
    monkeypatch.delenv("ROSTER_CORPUS_DSN", raising=False)
    llm = _LLM([TermExplanations(terms=[
        TermExplanation(term="eGFR", category="measure", plain="Kidney filtration estimate.",
                        purpose="Tracks kidney function.", application="Guides renal dosing.",
                        related=["creatinine", "CKD"])])])
    client = TestClient(create_app(_terms_service(llm)))
    assert client.get("/config").json()["term_glossary_enabled"] is True
    resp = client.post("/terms/explain", json={"question": "q", "answer": "eGFR guides dosing"})
    assert resp.status_code == 200
    d = resp.json()
    assert d["terms"][0]["term"] == "eGFR"
    assert d["terms"][0]["related"] == ["creatinine", "CKD"]
    assert d["glossary_total"] is None     # no DSN → accumulation unavailable, explanations still served


def test_terms_explain_404_without_vertical_prompt(monkeypatch) -> None:
    monkeypatch.setenv("ROSTER_TERM_GLOSSARY", "1")
    client = TestClient(create_app(_service()))   # no terms_prompt on the service
    assert client.post("/terms/explain", json={"answer": "x"}).status_code == 404
    assert client.get("/config").json()["term_glossary_enabled"] is False


def test_glossary_lookup_explains_fresh_term(monkeypatch) -> None:
    from roster_kernel.research.terms import TermExplanation
    monkeypatch.setenv("ROSTER_TERM_GLOSSARY", "1")
    monkeypatch.delenv("ROSTER_CORPUS_DSN", raising=False)
    llm = _LLM([TermExplanation(term="Creatinine", category="measure",
                                plain="A muscle waste product measured in blood.",
                                related=["eGFR"])])
    client = TestClient(create_app(_terms_service(llm)))
    resp = client.post("/glossary/lookup", json={"term": "creatinine", "context": "eGFR"})
    assert resp.status_code == 200
    d = resp.json()
    assert d["fresh"] is True
    assert d["entry"]["term"] == "Creatinine"
    assert d["entry"]["related"] == [{"term": "eGFR", "known": False}]


def test_glossary_list_empty_without_store(monkeypatch) -> None:
    monkeypatch.setenv("ROSTER_TERM_GLOSSARY", "1")
    monkeypatch.delenv("ROSTER_CORPUS_DSN", raising=False)
    client = TestClient(create_app(_service()))
    assert client.get("/glossary").json() == {"terms": [], "total": 0, "letters": {}}


def test_voice_intake_flag_echo(monkeypatch) -> None:
    monkeypatch.setenv("ROSTER_TRIAGE", "1")
    monkeypatch.setenv("ROSTER_VOICE_INTAKE", "1")
    svc = _service(); svc.triage_prompt = "intake"
    assert TestClient(create_app(svc)).get("/config").json()["voice_intake_enabled"] is True
    monkeypatch.delenv("ROSTER_VOICE_INTAKE", raising=False)
    svc2 = _service(); svc2.triage_prompt = "intake"
    assert TestClient(create_app(svc2)).get("/config").json()["voice_intake_enabled"] is False


def test_voice_tts_gating(monkeypatch) -> None:
    monkeypatch.delenv("ROSTER_VOICE_INTAKE", raising=False)
    client = TestClient(create_app(_service()))
    assert client.post("/voice/tts", json={"text": "hello"}).status_code == 404   # flag off
    monkeypatch.setenv("ROSTER_VOICE_INTAKE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(_service()))
    assert client.post("/voice/tts", json={"text": "hello"}).status_code == 404   # no key → not configured
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert client.post("/voice/tts", json={"text": "  "}).status_code == 400      # empty text


# ---- ◫ add-visuals (ROSTER_VISUAL_AUGMENT) ----

def _visuals_service(parsed):
    from roster_kernel.research.visuals import VisualSet
    class _LLM2:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            return LLMResult(parsed=parsed, output_tokens=5)
    svc = _service(); svc.llm = _LLM2(); svc.visuals_prompt = "make visuals"
    return svc


def test_visuals_404_when_flag_off(monkeypatch) -> None:
    monkeypatch.delenv("ROSTER_VISUAL_AUGMENT", raising=False)
    client = TestClient(create_app(_service()))
    assert client.post("/visuals/augment", json={"answer": "x"}).status_code == 404
    assert client.get("/config").json()["visual_augment_enabled"] is False


def test_visuals_augment_returns_grounded_flow(monkeypatch) -> None:
    from roster_kernel.research.visuals import Visual, VNode, VEdge, VisualSet
    monkeypatch.setenv("ROSTER_VISUAL_AUGMENT", "1")
    monkeypatch.delenv("ROSTER_CORPUS_DSN", raising=False)
    ans = "Metformin lowers hepatic glucose production, which reduces blood glucose."
    good = Visual(kind="flow", title="MoA",
        nodes=[VNode(id="a", label="Metformin", quote="Metformin lowers hepatic glucose production"),
               VNode(id="b", label="Glucose down", quote="which reduces blood glucose")],
        edges=[VEdge(src="a", dst="b", quote="Metformin lowers hepatic glucose production, which reduces blood glucose")])
    client = TestClient(create_app(_visuals_service(VisualSet(visuals=[good]))))
    assert client.get("/config").json()["visual_augment_enabled"] is True
    r = client.post("/visuals/augment", json={"question": "how does metformin work?", "answer": ans})
    assert r.status_code == 200
    vs = r.json()["visuals"]
    assert len(vs) == 1 and vs[0]["kind"] == "flow" and len(vs[0]["nodes"]) == 2


def test_visuals_augment_404_without_vertical_prompt(monkeypatch) -> None:
    monkeypatch.setenv("ROSTER_VISUAL_AUGMENT", "1")
    client = TestClient(create_app(_service()))   # no visuals_prompt on the service
    assert client.post("/visuals/augment", json={"answer": "x"}).status_code == 404
    assert client.get("/config").json()["visual_augment_enabled"] is False
