"""ResearchService.ask(audience=...) selects the compose directive — patient vs clinician — while
retrieval + gates stay identical. Verifies the byte-identical default + the patient fallback."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
from roster_kernel.runtime.research import ResearchService

_TEXT = "The approved metric value was 9.8 percent for the term period."
CLIN = "CLINICIAN_DIRECTIVE_MARKER"
PAT = "PATIENT_DIRECTIVE_MARKER"


class RecordingLLM:
    """Scripts the loop steps + compose, and records the compose call's user message."""
    def __init__(self):
        self._steps = [
            AgentStep(action="search", query="metric value"),
            AgentStep(action="answer", claims=[
                ClaimOut(text="metric was 9.8 percent", atom_id="a1",
                         quote="the approved metric value was 9.8 percent")]),
        ]
        self.compose_user = None

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        if response_format is ComposedAnswer:
            self.compose_user = messages[-1]["content"]
            return LLMResult(parsed=ComposedAnswer(answer="Value is 9.8 percent [1].",
                                                   directly_addresses=True), model="rec")
        return LLMResult(parsed=self._steps.pop(0), output_tokens=5, model="rec")


def _service():
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    llm = RecordingLLM()
    svc = ResearchService(llm=llm, embedder=FakeEmbedder(dim=8), sources={"corpus": src},
                          answer_format=CLIN, patient_answer_format=PAT)
    return svc, llm


def _ask(svc, **kw):
    return asyncio.run(svc.ask(question="what was the metric value?", tenant_id="A", **kw))


def test_clinician_is_default_and_uses_clinician_directive():
    svc, llm = _service()
    res = _ask(svc)                                 # no audience passed
    assert res.grounded and CLIN in llm.compose_user and PAT not in llm.compose_user


def test_explicit_clinician_uses_clinician_directive():
    svc, llm = _service()
    _ask(svc, audience="clinician")
    assert CLIN in llm.compose_user and PAT not in llm.compose_user


def test_patient_audience_uses_patient_directive():
    svc, llm = _service()
    _ask(svc, audience="patient")
    assert PAT in llm.compose_user and CLIN not in llm.compose_user


def test_patient_falls_back_to_clinician_when_no_patient_directive():
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    llm = RecordingLLM()
    svc = ResearchService(llm=llm, embedder=FakeEmbedder(dim=8), sources={"corpus": src},
                          answer_format=CLIN, patient_answer_format=None)  # vertical has no patient view
    _ask(svc, audience="patient")
    assert CLIN in llm.compose_user            # safe fallback → clinician directive


def test_unknown_audience_uses_clinician():
    svc, llm = _service()
    _ask(svc, audience="martian")
    assert CLIN in llm.compose_user and PAT not in llm.compose_user
