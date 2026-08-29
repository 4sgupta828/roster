from __future__ import annotations

import asyncio

from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.web import WebRetrievalSource

from .test_deep_company import RecordingWeb


class PersonRoutingLLM:
    def __init__(self, subject_kind: str = "person") -> None:
        self.subject_kind = subject_kind
        self._steps = [
            AgentStep(action="search", query="who is Jane Roe"),
            AgentStep(action="answer", claims=[ClaimOut(text="x", atom_id="nope", quote="nope")]),
        ]

    async def complete(
        self, *, system, messages, response_format, max_tokens=2048, temperature=None
    ):
        if getattr(response_format, "__name__", "") == "_ContractOut":
            return LLMResult(
                parsed=response_format(
                    mode="exploratory", subject_kind=self.subject_kind, entities=["Jane Roe"]),
                output_tokens=3,
            )
        if getattr(response_format, "__name__", "") == "_Verdicts":
            return LLMResult(
                parsed=response_format(verdicts=[{"index": 0, "keep": True}]),
                output_tokens=3,
            )
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3)


def test_retrieve_deep_person_returns_hits_and_profiles() -> None:
    from roster_kernel.providers.websearch import WebResult
    from roster_kernel.research.deep_person import retrieve_deep_person

    class PersonWeb(RecordingWeb):
        async def search(self, query: str, **kwargs) -> list[WebResult]:
            self.calls.append({"query": query, **kwargs})
            if "profile" in query:
                return [
                    WebResult(
                        url="https://github.com/janeroe", title="Jane Roe", snippet="",
                        body="Jane Roe"),
                    WebResult(
                        url="https://www.linkedin.com/in/jane-roe", title="Jane Roe", snippet="",
                        body="Jane Roe"),
                ]
            return [WebResult(
                url="https://news.example/jane-roe", title="Jane Roe profile", snippet="",
                body=("Jane Roe founded Acme and works on retrieval systems. " * 80))]

    source = WebRetrievalSource(PersonWeb(), max_results=1, max_chunks_per_page=5)

    hits, profiles = asyncio.run(retrieve_deep_person(
        person="Jane Roe",
        templates={
            "profile_query_template": "{person} profile",
            "profile_preference": ("linkedin.com", "github.com"),
            "external": {"bio": "{person} founder biography"},
        },
        source=source, tenant_id="t"))

    assert hits
    assert all("web:deep_person" in h.legs for h in hits)
    assert [p["host"] for p in profiles] == ["linkedin.com", "github.com"]


def test_retrieve_deep_person_is_fail_safe() -> None:
    from roster_kernel.research.deep_person import retrieve_deep_person

    class Boom:
        async def search(self, *args, **kwargs):
            raise RuntimeError("down")

    source = WebRetrievalSource(Boom())
    assert asyncio.run(retrieve_deep_person(
        person="Jane Roe", templates={"external": {"bio": "{person} bio"}},
        source=source, tenant_id="t")) == ([], [])


def test_run_react_deep_person_fires_only_when_enabled_and_surfaces_profiles(monkeypatch) -> None:
    calls: list[str] = []
    profile = {"name": "Jane Roe", "url": "https://github.com/janeroe", "host": "github.com"}

    async def fake_deep(**kwargs):
        calls.append(kwargs["person"])
        return [], [profile]

    async def keep_urls(hits):
        return hits

    import roster_kernel.research.react as react

    monkeypatch.setattr(react, "retrieve_deep_person", fake_deep)
    monkeypatch.setattr(react, "drop_dead_urls", keep_urls)
    aux = WebRetrievalSource(RecordingWeb())

    off_res = asyncio.run(run_react(
        question="Tell me all about Jane Roe",
        llm=PersonRoutingLLM(), embedder=FakeEmbedder(dim=8), source=aux,
        aux_source=aux, tenant_id="t", budget=BudgetState(max_calls=30),
        question_contract="shadow", contract_prompt="derive",
        deep_person=False, person_reader={"external": {"bio": "{person} bio"}},
        web_quality_prompt="judge", max_steps=3))
    assert calls == []
    assert off_res.people_profiles == []

    on_res = asyncio.run(run_react(
        question="Tell me all about Jane Roe",
        llm=PersonRoutingLLM(), embedder=FakeEmbedder(dim=8), source=aux,
        aux_source=aux, tenant_id="t", budget=BudgetState(max_calls=30),
        question_contract="shadow", contract_prompt="derive",
        deep_person=True, person_reader={"external": {"bio": "{person} bio"}},
        web_quality_prompt="judge", max_steps=3))
    assert calls == ["Jane Roe"]
    assert on_res.people_profiles == [profile]


def test_run_react_deep_person_does_not_fire_without_person_subject_kind(monkeypatch) -> None:
    # Regression guard for the follow-up-bomb fix (commit 28f8600): the deep-person reader must fire
    # ONLY on subject_kind=="person" — the old "subject_kind=='' AND single entity" fallback mis-fired it
    # on ROLES/CATEGORIES (e.g. "expand on the AI SRE startup founders") and scattered the answer. With
    # subject_kind="" the reader must NOT fire, even when the contract carries a single entity.
    calls: list[str] = []

    async def fake_deep(**kwargs):
        calls.append(kwargs["person"])
        return [], []

    import roster_kernel.research.react as react

    monkeypatch.setattr(react, "retrieve_deep_person", fake_deep)
    aux = WebRetrievalSource(RecordingWeb())

    asyncio.run(run_react(
        question="Tell me all about Jane Roe",
        llm=PersonRoutingLLM(subject_kind=""), embedder=FakeEmbedder(dim=8), source=aux,
        aux_source=aux, tenant_id="t", budget=BudgetState(max_calls=30),
        question_contract="shadow", contract_prompt="derive",
        deep_person=True, person_reader={"external": {"bio": "{person} bio"}},
        web_quality_prompt="judge", max_steps=3))

    assert calls == []
