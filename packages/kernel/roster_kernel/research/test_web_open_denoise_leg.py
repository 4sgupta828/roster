"""Tests for the full-web + denoising-funnel web leg in the ReAct loop
(ROSTER_WEB_OPEN_DENOISE / T3).

Mirrors test_entity_open_leg.py's harness: the REAL `run_react` driven by a
RoutingLLM (dispatches on `response_format`) + a RecordingAux web source that
records each request's `web_open` AND `web_denoise`, and returns hits with/without
a `source_kind` authority facet so the authoritative-subset fail-safe is provable.

Assertions:
- ON: the plain `web` leg's request has web_open=True AND web_denoise=True; its hits
  are screened (verdicts applied); NO separate `web:entity_open` leg fires even when
  subject_kind=="specific_entity" (Edit 3 redundancy guard).
- ON + screen can't-judge (llm raises → None): the `web` leg falls back to
  `_authoritative_subset` — only facet-stamped hits survive.
- OFF: the `web` leg's request has web_denoise=False (and web_open == pre-existing
  `_web_open`); the leg is NOT screened. Byte-identical guard.
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
    """Dispatches by requested `response_format`: contract derivation, the open-web
    screen (_Verdicts), and the planner steps. `raise_on_verdict` forces the screen's
    can't-judge (exception → screen_open_web_hits returns None) fail-safe path."""

    def __init__(self, steps, *, subject_kind="specific_entity", keep=True,
                 raise_on_verdict=False):
        self._steps = list(steps)
        self._subject_kind = subject_kind
        self._keep = keep
        self._raise = raise_on_verdict
        self.verdict_calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        name = getattr(response_format, "__name__", "")
        if name == "_ContractOut":
            return LLMResult(
                parsed=response_format(mode="exploratory", subject_kind=self._subject_kind),
                output_tokens=3, model="fake")
        if name == "_Verdicts":
            self.verdict_calls += 1
            if self._raise:
                raise ValueError("judge unavailable")
            return LLMResult(
                parsed=response_format(verdicts=[{"index": i, "keep": self._keep} for i in range(50)]),
                output_tokens=3, model="fake")
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3, model="fake")


class RecordingAux:
    """Fake web source: records (web_open, web_denoise) of each request. Returns a
    mix of authority-stamped and unstamped open-web hits so the authoritative-subset
    fail-safe (keep only source_kind-stamped) is observable."""

    key = "web"

    def __init__(self) -> None:
        self.reqs: list[tuple[bool, bool]] = []
        self._texts: dict[str, str] = {}

    def make_block_loader(self, tenant_id, workspace_id=None):
        def _load(document_id, block_id):
            return self._texts.get(block_id)
        return _load

    def _hit(self, doc: str, blk: str, text: str, facets: dict) -> BlockHit:
        self._texts[blk] = text
        return BlockHit(document_id=doc, block_id=blk, text=text, facets=dict(facets),
                        locator=Locator("block_span", doc, {"block_id": blk}),
                        document_title=f"{doc} title")

    async def search(self, req):
        wo = bool(getattr(req, "web_open", False))
        wd = bool(getattr(req, "web_denoise", False))
        self.reqs.append((wo, wd))
        # 3 open-web candidates: two carry a venue-authority facet, one does not.
        return [
            self._hit("stamped0", "s0", "authoritative body 0", {"source_kind": "vc_firm"}),
            self._hit("stamped1", "s1", "authoritative body 1", {"source_kind": "startup_site"}),
            self._hit("bare0", "b0", "unstamped open-web body", {}),
        ]

    @property
    def web_open_values(self) -> list[bool]:
        return [wo for wo, _ in self.reqs]


def _corpus() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="c1", document_id="cd", tenant_id="A",
                         text="corpus body about the entity and its technology",
                         locator=Locator("block_span", "cd", {"block_id": "c1"})))
    return src


def _drive(*, web_open_denoise, entity_open_web=True, subject_kind="specific_entity",
           keep=True, raise_on_verdict=False):
    """Run run_react through step0 (search) + step1 (answer); return (result, aux, llm)."""
    llm = RoutingLLM(
        [AgentStep(action="search", query="what is the entity"),
         AgentStep(action="answer",
                   claims=[ClaimOut(text="x", atom_id="no_such_atom", quote="whatever")])],
        subject_kind=subject_kind, keep=keep, raise_on_verdict=raise_on_verdict)
    aux = RecordingAux()
    res = asyncio.run(run_react(
        question="what are the top VCs in tech?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=_corpus(),
        aux_source=aux, tenant_id="A", budget=BudgetState(max_calls=30),
        max_steps=4,
        question_contract="shadow", contract_prompt="derive a contract",
        entity_open_web=entity_open_web, web_open_denoise=web_open_denoise,
        web_quality_prompt="judge these pages",
        collect_diagnostics=True))
    return res, aux, llm


def test_denoise_on_opens_and_screens_the_web_leg() -> None:
    res, aux, llm = _drive(web_open_denoise=True, keep=True)
    # exactly ONE aux request (the single `web` leg on step 0) — no redundant entity_open probe
    assert len(aux.reqs) == 1, aux.reqs
    wo, wd = aux.reqs[0]
    assert wo is True and wd is True, aux.reqs
    # the screen ran on the web leg (verdicts applied → all 3 kept)
    assert llm.verdict_calls == 1
    dn = (res.diagnostics or {}).get("web_denoise")
    assert dn and dn[0]["step"] == 1 and dn[0]["raw"] == 3 and dn[0]["kept"] == 3, dn
    # and the entity-open leg never fired
    assert "web_entity_open" not in (res.diagnostics or {})


def test_denoise_cant_judge_falls_back_to_authoritative_subset() -> None:
    # judge raises → screen returns None → keep only the source_kind-stamped hits (2 of 3).
    res, aux, llm = _drive(web_open_denoise=True, raise_on_verdict=True)
    assert len(aux.reqs) == 1 and aux.reqs[0] == (True, True)
    assert llm.verdict_calls == 1  # the screen was attempted (then raised)
    dn = (res.diagnostics or {}).get("web_denoise")
    assert dn and dn[0]["raw"] == 3 and dn[0]["kept"] == 2, dn


def test_denoise_off_leaves_web_leg_unscreened() -> None:
    # OFF-byte-identical guard: entity_open_web off too → the ONLY aux request is the plain
    # `web` leg, whose web_open == pre-existing _web_open (Edit 2 reduces `_web_open or False`
    # to `_web_open`), web_denoise=False, and it is NOT screened.
    res, aux, llm = _drive(web_open_denoise=False, entity_open_web=False)
    assert len(aux.reqs) == 1, aux.reqs
    _wo, wd = aux.reqs[0]
    assert wd is False, aux.reqs          # web_denoise off → web.py gates are no-ops (T2)
    assert llm.verdict_calls == 0         # the web leg is not screened
    assert "web_denoise" not in (res.diagnostics or {})
