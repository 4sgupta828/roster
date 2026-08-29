"""Answer-focus: elliptical follow-ups are condensed (so retrieval + compose inherit the subject) and
compose gets the answer-scope instruction. Off / no-history → byte-identical no-op."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.runtime.research import ResearchService

_TEXT = "The approved metric value was 9.8 percent for the term period."
SCOPE_MARK = "Directly ANSWER the specific question"   # only present under answer_focus


class FocusLLM:
    def __init__(self, condensed="RESOLVED SUBJECT QUESTION", clar="", subject="", operate=False):
        self.condensed = condensed
        self.clar = clar
        self.subject = subject
        self.operate = operate
        self.compose_user = None
        self.planner_user = None
        self.transformed = None
        self._loop = [
            AgentStep(action="search", query="metric value"),
            AgentStep(action="answer", claims=[
                ClaimOut(text="metric was 9.8 percent", atom_id="a1",
                         quote="the approved metric value was 9.8 percent")]),
        ]

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        name = getattr(response_format, "__name__", "")
        if name == "_Resolution":                      # the follow-up resolver pre-step
            return LLMResult(parsed=response_format(
                core_query=self.condensed, subject=self.subject,
                needs_clarification=bool(self.clar), clarification=self.clar,
                operate_on_prior=self.operate), model="c")
        if name == "_PlainAnswer":                     # the operate-on-prior transform
            self.transformed = messages[-1]["content"]
            return LLMResult(parsed=response_format(text="A shorter version of the prior answer."), model="c")
        if response_format is ComposedAnswer:
            self.compose_user = messages[-1]["content"]
            return LLMResult(parsed=ComposedAnswer(answer="Value is 9.8 percent [1].",
                                                   directly_addresses=True), model="c")
        if self.planner_user is None:                  # first planner (search-planning) message
            self.planner_user = messages[-1]["content"]
        return LLMResult(parsed=self._loop.pop(0), output_tokens=5, model="c")


def _service(condensed="RESOLVED SUBJECT QUESTION", clar="", subject="", operate=False):
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    llm = FocusLLM(condensed, clar, subject, operate)
    return ResearchService(llm=llm, embedder=FakeEmbedder(dim=8), sources={"corpus": src}), llm


HIST = [{"question": "What is first-line PCP prophylaxis?", "answer": "TMP-SMX is first-line."}]


def test_followup_condensed_and_reaches_compose():
    svc, llm = _service("What is the dose of TMP-SMX for PCP prophylaxis?")
    res = asyncio.run(svc.ask(question="What dose?", tenant_id="A", history=HIST, answer_focus=True))
    assert res.resolved_question == "What is the dose of TMP-SMX for PCP prophylaxis?"
    assert "What is the dose of TMP-SMX" in llm.compose_user        # compose sees the SUBJECT-bound question
    assert "What dose?" not in llm.compose_user


def test_answer_focus_adds_scoping_instruction():
    svc, llm = _service()
    asyncio.run(svc.ask(question="What dose?", tenant_id="A", history=HIST, answer_focus=True))
    assert SCOPE_MARK in llm.compose_user


def test_off_is_byte_identical_noop():
    svc, llm = _service("SHOULD NOT BE USED")
    res = asyncio.run(svc.ask(question="What dose?", tenant_id="A", history=HIST, answer_focus=False))
    assert res.resolved_question == ""                 # no condensation
    assert SCOPE_MARK not in llm.compose_user           # original compose instruction
    assert "What dose?" in llm.compose_user             # raw question used


def test_no_history_skips_condense():
    svc, llm = _service("SHOULD NOT BE USED")
    res = asyncio.run(svc.ask(question="What dose?", tenant_id="A", answer_focus=True))  # no history
    assert res.resolved_question == ""
    assert "What dose?" in llm.compose_user
    assert SCOPE_MARK in llm.compose_user               # compose-scope still applies single-turn


def test_verbatim_rewrite_is_not_flagged_as_resolved():
    svc, llm = _service("What dose?")                  # resolver returns it unchanged (self-contained)
    res = asyncio.run(svc.ask(question="What dose?", tenant_id="A", history=HIST, answer_focus=True))
    assert res.resolved_question == ""                 # unchanged → not surfaced as a rewrite
    assert "What dose?" in llm.compose_user


def test_ambiguous_followup_returns_clarification_and_skips_research():
    svc, llm = _service(condensed="best guess", clar="Which drug did you mean — A or B?")
    res = asyncio.run(svc.ask(question="What dose?", tenant_id="A", history=HIST,
                              answer_focus=True, clarify=True))
    assert res.clarification == "Which drug did you mean — A or B?"
    assert res.stopped_reason == "clarify"
    assert not res.verified_claims                     # no research ran
    assert llm.compose_user is None                    # compose never called


def test_clarify_off_ignores_needs_clarification():
    svc, llm = _service(condensed="best guess standalone", clar="Which drug?")
    res = asyncio.run(svc.ask(question="What dose?", tenant_id="A", history=HIST,
                              answer_focus=True, clarify=False))   # clarify flag OFF
    assert res.clarification == ""                      # not honored
    assert llm.compose_user is not None                # research + compose ran normally


def test_operate_on_prior_reshapes_without_research():
    svc, llm = _service(operate=True)
    hist = [{"question": "First-line PCP prophylaxis?", "answer": "TMP-SMX is first-line; the dose is 1 DS tab daily."}]
    res = asyncio.run(svc.ask(question="summarize that", tenant_id="A", history=hist, answer_focus=True))
    assert res.derived_from_prior is True
    assert res.composed_answer == "A shorter version of the prior answer."
    assert res.stopped_reason == "operate_prior"
    assert not res.verified_claims                     # NO new research/claims
    assert llm.compose_user is None                    # compose never ran
    assert "TMP-SMX is first-line" in llm.transformed  # the prior answer was the transform input


def test_episodic_prior_findings_reach_the_planner():
    svc, llm = _service()
    hist = [{"question": "First-line PCP prophylaxis?", "answer": "TMP-SMX is first-line.",
             "claims": [{"text": "TMP-SMX is the first-line agent for PCP prophylaxis"},
                        {"text": "Alternatives include dapsone and atovaquone"}]}]
    asyncio.run(svc.ask(question="What dose?", tenant_id="A", history=hist, answer_focus=True))
    assert "already established" in llm.planner_user
    assert "TMP-SMX is the first-line agent" in llm.planner_user


def test_episodic_off_without_answer_focus():
    svc, llm = _service()
    hist = [{"question": "q", "answer": "a", "claims": [{"text": "ESTABLISHED FINDING X"}]}]
    asyncio.run(svc.ask(question="follow up", tenant_id="A", history=hist, answer_focus=False))
    assert "already established" not in (llm.planner_user or "")   # gated on answer_focus


def test_operate_on_prior_falls_through_without_prior_answer():
    svc, llm = _service(operate=True)
    res = asyncio.run(svc.ask(question="summarize that", tenant_id="A",
                              history=[{"question": "hi", "answer": ""}], answer_focus=True))
    assert res.derived_from_prior is False             # no prior answer → normal research ran
    assert llm.compose_user is not None
