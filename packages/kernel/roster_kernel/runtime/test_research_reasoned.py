"""Reasoned engine routing (the noesis clinical-decision mode, kernel side). One scaffold call
classifies the question: DECISION (management) → coverage-brief-steered retrieval + decision-gated
compose; LOOKUP → standard engine (no decision frame); UNDERSTANDING → causal-model compose. Plain
`ask()` (the flag-OFF path) never scaffolds and is byte-identical. Prompts are opaque sentinels here —
this tests WIRING/routing, not the tech prose (that is a held-out behavioral eval)."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.runtime.research import ResearchService

_TEXT = "Anthropic crossed 47 billion in revenue run rate in 2026."
SCAFFOLD = "SCAFFOLD::classify decision/lookup/understanding"
REASONED = "REASONED_FORMAT::answer as a decision"
UNDERSTANDING = "UNDERSTANDING_FORMAT::causal model"
UHINT = "UHINT::investigate the mechanism"


class ReasonedLLM:
    """Returns a scriptable _Scaffold verdict, then a search step, then claims, then a ComposedAnswer.
    Records the planner and compose user messages so the test can assert what was injected."""
    def __init__(self, kind="management", key_decisions=None, explicit_asks=None):
        self.kind = kind
        self.key_decisions = key_decisions or ["which layer — infra, model, or application?"]
        self.explicit_asks = explicit_asks or ["where to deploy $10M today?"]
        self.compose_user = None
        self.planner_user = None
        self.scaffold_called = False
        # search query must lexically retrieve the block so its atom (a1) exists and the claim verifies
        self._loop = [
            AgentStep(action="search", query="Anthropic revenue run rate"),
            AgentStep(action="answer", claims=[
                ClaimOut(text="Anthropic revenue run rate 47B", atom_id="a1",
                         quote="anthropic crossed 47 billion in revenue run rate")]),
        ]

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        name = getattr(response_format, "__name__", "")
        if name == "_Scaffold":                         # the reasoned scaffold classification call
            self.scaffold_called = True
            return LLMResult(parsed=response_format(
                kind=self.kind, likely_causes=["seed/Series-A application bets"],
                cant_miss=["check-size vs round-size fit"],
                key_decisions=self.key_decisions, explicit_asks=self.explicit_asks), model="c")
        if response_format is ComposedAnswer:
            self.compose_user = messages[-1]["content"]
            return LLMResult(parsed=ComposedAnswer(answer="Deploy into X [1].",
                                                   directly_addresses=True), model="c")
        if self.planner_user is None:
            self.planner_user = messages[-1]["content"]
        return LLMResult(parsed=self._loop.pop(0), output_tokens=5, model="c")


def _service(**llm_kw):
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    llm = ReasonedLLM(**llm_kw)
    svc = ResearchService(
        llm=llm, embedder=FakeEmbedder(dim=8), sources={"corpus": src},
        reasoned_scaffold_prompt=SCAFFOLD, reasoned_answer_format=REASONED,
        understanding_answer_format=UNDERSTANDING, understanding_query_hint=UHINT)
    return svc, llm


def test_decision_question_routes_reasoned():
    """A DECISION (management) question: coverage brief steers the planner AND the decision-gated
    compose directive reaches compose."""
    svc, llm = _service(kind="management")
    res = asyncio.run(svc.ask_reasoned(question="Where should I invest $10M today in AI?", tenant_id="A"))
    assert llm.scaffold_called
    assert "Coverage brief" in llm.planner_user            # decision structure injected as questions
    assert "which layer" in llm.planner_user               # a key_decision item made it in
    assert REASONED in llm.compose_user                     # decision-gated compose directive applied
    assert res.composed_answer                              # an answer still composed + span-verified
    assert res.verified_claims


def test_lookup_falls_through_to_standard():
    """A pure LOOKUP: no decision frame — the standard compose runs, reasoned directive absent."""
    svc, llm = _service(kind="lookup")
    res = asyncio.run(svc.ask_reasoned(question="What is Anthropic's revenue run rate?", tenant_id="A"))
    assert llm.scaffold_called
    assert REASONED not in (llm.compose_user or "")        # standard engine, not decision-gated
    assert "Coverage brief" not in (llm.planner_user or "")
    assert res.verified_claims


def test_understanding_routes_causal_model():
    """A WHY/HOW question: the causal-model compose contract + mechanism query hint apply."""
    svc, llm = _service(kind="understanding")
    res = asyncio.run(svc.ask_reasoned(question="How does retrieval-augmented generation work?", tenant_id="A"))
    assert llm.scaffold_called
    assert UNDERSTANDING in llm.compose_user               # causal-model directive applied
    assert REASONED not in llm.compose_user
    assert UHINT in llm.planner_user                        # mechanism-steering hint reached retrieval
    assert res.verified_claims


def test_off_path_plain_ask_is_byte_identical():
    """The flag-OFF path binds plain ask(): no scaffold call, no reasoned/understanding directive —
    byte-identical to today (Rule 20)."""
    svc, llm = _service(kind="management")
    res = asyncio.run(svc.ask(question="Where should I invest $10M today in AI?", tenant_id="A"))
    assert not llm.scaffold_called                         # ask() never scaffolds
    assert REASONED not in llm.compose_user
    assert UNDERSTANDING not in llm.compose_user
    assert "Coverage brief" not in (llm.planner_user or "")
    assert res.verified_claims


def test_reasoned_inert_without_vertical_prompts():
    """No vertical prompts supplied → ask_reasoned degrades to plain ask (the pre-port tech state)."""
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    llm = ReasonedLLM(kind="management")
    svc = ResearchService(llm=llm, embedder=FakeEmbedder(dim=8), sources={"corpus": src})  # no reasoned_*
    asyncio.run(svc.ask_reasoned(question="Where should I invest $10M?", tenant_id="A"))
    assert not llm.scaffold_called                         # short-circuits to ask() — dormant, as in prod today
