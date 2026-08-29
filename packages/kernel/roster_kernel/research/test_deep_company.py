from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.providers.websearch import WebResult
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.web import WebRetrievalSource


class RecordingWeb:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def search(
        self,
        query: str,
        *,
        max_results: int = 8,
        open_web: bool = False,
        recency_days: int | None = None,
        max_chars: int | None = None,
    ) -> list[WebResult]:
        self.calls.append({
            "query": query,
            "max_results": max_results,
            "open_web": open_web,
            "max_chars": max_chars,
        })
        if query == "Acme official website":
            return [WebResult(url="https://acme.com", title="Acme", snippet="", body="Acme home")]
        if "site:acme.com" in query:
            return [WebResult(
                url="https://acme.com/product", title="Product", snippet="",
                body=("Acme product line uses retrieval and model routing. " * 80))]
        return [WebResult(
            url="https://news.example/acme-funding", title="Funding", snippet="",
            body=("Acme raised funding from Example Ventures. " * 80))]


def test_retrieve_deep_company_uses_same_web_source_and_stamps_facets() -> None:
    from roster_kernel.research.deep_company import retrieve_deep_company

    client = RecordingWeb()
    source = WebRetrievalSource(client, max_results=1, max_chunks_per_page=5)
    cfg = {
        "domain_query_template": "{company} official website",
        "internal": {"product": "{company} product"},
        "external": {"funding": "{company} funding investors"},
        "max_chars": 12000,
        "max_results_per_query": 2,
        "max_chunks_per_page": 5,
    }

    hits = asyncio.run(retrieve_deep_company(
        company="Acme", templates=cfg, source=source,
        tenant_id="t", workspace_id=None))

    assert hits
    assert any(h.facets.get("web_role") == "official" for h in hits)
    assert any(h.facets.get("source_kind") == "news" for h in hits)
    assert all(h.facets.get("deep_facet") in {"product", "funding"} for h in hits)
    assert source.make_block_loader("t")(
        hits[0].document_id, hits[0].block_id) == hits[0].text
    facet_calls = [c for c in client.calls if c["query"] != "Acme official website"]
    assert facet_calls
    assert all(c["max_results"] == 2 and c["max_chars"] == 12000 for c in facet_calls)


def test_retrieve_deep_company_is_fail_safe() -> None:
    from roster_kernel.research.deep_company import retrieve_deep_company

    class Boom:
        async def search(self, *args, **kwargs):
            raise RuntimeError("down")

    source = WebRetrievalSource(Boom())
    assert asyncio.run(retrieve_deep_company(
        company="Acme", templates={"internal": {"x": "{company} x"}},
        source=source, tenant_id="t")) == []


def test_web_retrieval_allows_per_request_chunk_cap() -> None:
    client = RecordingWeb()
    source = WebRetrievalSource(client, max_results=1, max_chunks_per_page=3)
    req = RetrievalRequest(
        query="site:acme.com Acme product", tenant_id="t", k=20,
        web_max_chunks_per_page=5, web_max_results=2, web_max_chars=12000)

    hits = asyncio.run(source.search(req))

    assert len(hits) == 5
    assert client.calls[-1]["max_results"] == 2
    assert client.calls[-1]["max_chars"] == 12000


class RoutingLLM:
    def __init__(self) -> None:
        self._steps = [
            AgentStep(action="search", query="what is Acme"),
            AgentStep(action="answer", claims=[ClaimOut(text="x", atom_id="nope", quote="nope")]),
        ]

    async def complete(
        self, *, system, messages, response_format, max_tokens=2048, temperature=None
    ):
        if getattr(response_format, "__name__", "") == "_ContractOut":
            return LLMResult(
                parsed=response_format(
                    mode="exploratory", subject_kind="specific_entity", entities=["Acme"]),
                output_tokens=3,
            )
        if getattr(response_format, "__name__", "") == "_Verdicts":
            return LLMResult(
                parsed=response_format(verdicts=[{"index": 0, "keep": True}]),
                output_tokens=3,
            )
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3)


def test_run_react_deep_company_fires_only_when_enabled(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_deep(**kwargs):
        calls.append(kwargs["company"])
        return []

    import roster_kernel.research.react as react

    monkeypatch.setattr(react, "retrieve_deep_company", fake_deep)
    aux = WebRetrievalSource(RecordingWeb())

    asyncio.run(run_react(
        question="Tell me all about Acme",
        llm=RoutingLLM(), embedder=FakeEmbedder(dim=8), source=aux,
        aux_source=aux, tenant_id="t", budget=BudgetState(max_calls=30),
        question_contract="shadow", contract_prompt="derive",
        deep_company=False, company_reader={"internal": {"product": "{company} product"}},
        web_quality_prompt="judge", max_steps=3))
    assert calls == []

    asyncio.run(run_react(
        question="Tell me all about Acme",
        llm=RoutingLLM(), embedder=FakeEmbedder(dim=8), source=aux,
        aux_source=aux, tenant_id="t", budget=BudgetState(max_calls=30),
        question_contract="shadow", contract_prompt="derive",
        deep_company=True, company_reader={"internal": {"product": "{company} product"}},
        web_quality_prompt="judge", max_steps=3))
    assert calls == ["Acme"]
