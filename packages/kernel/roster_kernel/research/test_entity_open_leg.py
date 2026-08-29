"""Tests for the additive, quality-screened open-web leg in the ReAct loop
(ROSTER_WEB_ENTITY_OPEN / T3).

Drives the REAL `run_react` with fakes:
- a RoutingLLM that dispatches on the requested `response_format` — it answers the
  contract derivation (`_ContractOut`, with a chosen `subject_kind`), the open-web
  quality screen (`_Verdicts`), and the planner steps (`AgentStep`);
- a RecordingAux web source that records the `web_open` value of every request it
  receives and returns distinct hits for the whitelisted vs open probe.

Assertions:
- ON + subject_kind="specific_entity": an OPEN (`web_open=True`) request IS issued on
  step 0, and its hits are quality-screened (raw vs kept recorded under diagnostics).
- OFF (or subject_kind != "specific_entity"): NO `web_open=True` request is EVER issued
  — the leg set is byte-identical to today (only the whitelisted `web_open=False` leg).
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import BlockHit, Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


class RoutingLLM:
    """Dispatches by the requested `response_format` so ONE fake serves every call
    site in run_react (contract derivation, the open-web screen, and the planner)."""

    def __init__(self, steps, *, subject_kind="specific_entity", keep=True):
        self._steps = list(steps)
        self._subject_kind = subject_kind
        self._keep = keep
        self.verdict_calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        name = getattr(response_format, "__name__", "")
        if name == "_ContractOut":
            return LLMResult(
                parsed=response_format(mode="exploratory", subject_kind=self._subject_kind),
                output_tokens=3, model="fake")
        if name == "_Verdicts":
            self.verdict_calls += 1
            # keep/drop verdicts for far more indices than exist — screen_open_web_hits
            # filters to `0 <= index < len(candidates)`, so the surplus is ignored.
            return LLMResult(
                parsed=response_format(verdicts=[{"index": i, "keep": self._keep} for i in range(50)]),
                output_tokens=3, model="fake")
        # anything else (AgentStep planner step, ComposedAnswer) → next scripted step.
        # ComposedAnswer pops past the end → IndexError, caught by compose's own retry
        # loop (the answer still surfaces from verified claims / a fail note).
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3, model="fake")


class RecordingAux:
    """Fake web source: records the `web_open` of each request and returns distinct
    hits for the whitelisted (web_open=False) vs open (web_open=True) probe."""

    key = "web"

    def __init__(self) -> None:
        self.web_open_values: list[bool] = []
        self._texts: dict[str, str] = {}

    def make_block_loader(self, tenant_id, workspace_id=None):
        def _load(document_id, block_id):
            return self._texts.get(block_id)
        return _load

    def _hit(self, doc: str, blk: str, text: str) -> BlockHit:
        self._texts[blk] = text
        return BlockHit(document_id=doc, block_id=blk, text=text,
                        locator=Locator("block_span", doc, {"block_id": blk}),
                        document_title=f"{doc} title")

    async def search(self, req):
        wo = bool(getattr(req, "web_open", False))
        self.web_open_values.append(wo)
        if wo:
            # the OPEN probe returns three open-web candidates to screen
            return [self._hit(f"open{i}", f"ob{i}", f"open web body {i} about the entity")
                    for i in range(3)]
        # the whitelisted leg returns one trusted-domain hit
        return [self._hit("wl0", "wb0", "whitelisted trusted-domain body")]


def _corpus() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="c1", document_id="cd", tenant_id="A",
                         text="corpus body about the entity and its technology",
                         locator=Locator("block_span", "cd", {"block_id": "c1"})))
    return src


def _drive(*, entity_open_web, subject_kind, keep=True):
    """Run run_react through step0 (search) + step1 (answer); return (result, aux, llm)."""
    llm = RoutingLLM(
        [AgentStep(action="search", query="what is the entity"),
         # a claim that resolves to no known atom → REJECTED (not empty), which
         # finalizes the loop without triggering extract-recovery re-asks. We assert
         # on the legs/screen, not on grounded output.
         AgentStep(action="answer",
                   claims=[ClaimOut(text="x", atom_id="no_such_atom", quote="whatever")])],
        subject_kind=subject_kind, keep=keep)
    aux = RecordingAux()
    res = asyncio.run(run_react(
        question="what is Blazel and how does its technology work?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=_corpus(),
        aux_source=aux, tenant_id="A", budget=BudgetState(max_calls=30),
        max_steps=4,
        # trigger contract derivation (needs a non-empty prompt + a contract mode)
        question_contract="shadow", contract_prompt="derive a contract",
        entity_open_web=entity_open_web, web_quality_prompt="judge these pages",
        collect_diagnostics=True))
    return res, aux, llm


def test_entity_open_fires_and_screens_on_specific_entity() -> None:
    res, aux, llm = _drive(entity_open_web=True, subject_kind="specific_entity", keep=True)
    # an OPEN probe (web_open=True) WAS issued...
    assert True in aux.web_open_values, aux.web_open_values
    # ...exactly once (step 0 only — not per step)
    assert aux.web_open_values.count(True) == 1, aux.web_open_values
    # the whitelisted leg still fired (web_open=False present)
    assert False in aux.web_open_values, aux.web_open_values
    # the quality screen was invoked on the open leg's hits
    assert llm.verdict_calls == 1
    diag = res.diagnostics or {}
    weo = diag.get("web_entity_open")
    assert weo and weo[0]["step"] == 1 and weo[0]["raw"] == 3 and weo[0]["kept"] == 3, weo


def test_entity_open_screen_drops_junk() -> None:
    # same path, but the judge rejects everything → kept == 0 (fail-closed screening path)
    res, aux, llm = _drive(entity_open_web=True, subject_kind="specific_entity", keep=False)
    assert aux.web_open_values.count(True) == 1
    assert llm.verdict_calls == 1
    weo = (res.diagnostics or {}).get("web_entity_open")
    assert weo and weo[0]["raw"] == 3 and weo[0]["kept"] == 0, weo


def test_off_never_issues_open_request() -> None:
    # OFF-byte-identical guard: flag off → no open probe, no screen, leg set unchanged.
    res, aux, llm = _drive(entity_open_web=False, subject_kind="specific_entity")
    assert True not in aux.web_open_values, aux.web_open_values
    assert aux.web_open_values and set(aux.web_open_values) == {False}
    assert llm.verdict_calls == 0
    assert "web_entity_open" not in (res.diagnostics or {})


def test_general_subject_never_issues_open_request() -> None:
    # flag ON but the LLM judged the subject NOT a specific entity → no open probe.
    res, aux, llm = _drive(entity_open_web=True, subject_kind="general")
    assert True not in aux.web_open_values, aux.web_open_values
    assert set(aux.web_open_values) == {False}
    assert llm.verdict_calls == 0
    assert "web_entity_open" not in (res.diagnostics or {})
