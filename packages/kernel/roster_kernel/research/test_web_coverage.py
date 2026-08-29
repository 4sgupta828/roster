from __future__ import annotations

import asyncio

from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.research.web_coverage import build_coverage_queries
from roster_kernel.retrieval.web import WebRetrievalSource

from .test_deep_company import RecordingWeb


# ---- build_coverage_queries: structural expansion (axis-only FIRST, then entity×axis) ----------

def test_build_coverage_queries_axis_first_then_entity_axis():
    qs = build_coverage_queries(["coding agents", "video AI"], ["moat", "ICP"], cap=10)
    # axis-only legs lead (the business dimensions a thin corpus misses), then entity×axis round-robin
    assert qs[:2] == ["moat", "ICP"]
    assert "coding agents moat" in qs and "video AI ICP" in qs


def test_build_coverage_queries_dedup_and_cap():
    qs = build_coverage_queries(["A"], ["x", "x", "y"], cap=3)
    assert qs == ["x", "y", "A x"]              # deduped, capped at 3


def test_build_coverage_queries_empty():
    assert build_coverage_queries([], []) == []
    assert build_coverage_queries([], ["only axis"]) == ["only axis"]


class _RoutingLLM:
    """Emits a contract (subject_kind/entities/axes) then a one-step search→answer."""
    def __init__(self, subject_kind: str, entities, axes) -> None:
        self.subject_kind, self.entities, self.axes = subject_kind, entities, axes
        self._steps = [
            AgentStep(action="search", query="landscape"),
            AgentStep(action="answer", claims=[ClaimOut(text="x", atom_id="nope", quote="nope")]),
        ]

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        if getattr(response_format, "__name__", "") == "_ContractOut":
            return LLMResult(parsed=response_format(
                mode="exploratory", subject_kind=self.subject_kind,
                entities=list(self.entities), axes=list(self.axes)), output_tokens=3)
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3)


def _run(reflection: str, subject_kind: str, monkeypatch):
    seen = {}

    async def fake_cov(**kwargs):
        seen["queries"] = list(kwargs.get("queries") or [])
        return []

    async def keep_urls(hits):
        return hits

    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "retrieve_web_coverage", fake_cov)
    monkeypatch.setattr(react, "drop_dead_urls", keep_urls)
    aux = WebRetrievalSource(RecordingWeb())
    asyncio.run(run_react(
        question="landscape of text to video startups: moat, ICP, distribution",
        llm=_RoutingLLM(subject_kind, ["segment one", "segment two"], ["moat", "ICP"]),
        embedder=FakeEmbedder(dim=8), source=aux, aux_source=aux, tenant_id="t",
        budget=BudgetState(max_calls=30), contract_prompt="derive", reflection=reflection,
        web_quality_prompt="judge", max_steps=2))
    return seen


def test_web_coverage_fires_on_steer_for_general(monkeypatch):
    seen = _run("steer", "general", monkeypatch)
    qs = seen.get("queries")
    assert qs                                        # fired
    # topic-anchored axis legs lead (the question's subject prefixes each dimension so a bare 'moat'
    # search isn't off-topic), then entity×axis legs
    assert qs[0].endswith("moat") and "video" in qs[0].lower()
    assert any("segment one" in q for q in qs)       # entity×axis legs present


def test_web_coverage_off_is_byte_identical(monkeypatch):
    assert _run("", "general", monkeypatch) == {}    # never called when flag off


def test_web_coverage_shadow_does_not_fire(monkeypatch):
    assert _run("shadow", "general", monkeypatch) == {}   # shadow logs but never retrieves


def test_web_coverage_skips_single_entity(monkeypatch):
    # specific_entity / person are already web-read by the deep readers — coverage must NOT double-fire
    assert _run("steer", "specific_entity", monkeypatch) == {}
    assert _run("steer", "person", monkeypatch) == {}
