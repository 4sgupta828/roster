"""Deep-synthesis COMPOSE behavior tests (ROSTER_DEEP_SYNTHESIS, T2+T3).

Drives the REAL `run_react` with fakes (same harness family as test_entity_open_leg /
test_web_open_denoise_leg): a RoutingLLM that dispatches by the requested
`response_format` so ONE fake serves the planner steps, the derive gate
(DeriveCandidates → _Verdicts → _Ranking), AND compose (ComposedAnswer, whose
`compose_user` prompt is recorded so we can assert what the compose LLM actually saw).

Coverage:
- OFF (deep_synthesis=False): compose_user has NO "GROUNDED DERIVATIONS" block, the
  base directive is the NORMAL answer_format (never the deep format), derive not invoked.
- lookup + deep on: crisp — no derive, no deep format.
- management / understanding + deep on: derive IS invoked, its [D1] block appears in the
  compose_user the LLM receives, and the deep_answer_format is the compose base.
- prose-audit: an unsupported figure in the composed prose triggers exactly ONE recompose
  (single-recompose success path); a STILL-unsupported recompose falls back to the non-deep
  compose (fallback path).
"""
from __future__ import annotations

import asyncio

import pytest

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.research.reason import DeriveCandidate
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource

_BLOCK_TEXT = "The approved metric value was 9.8 percent for the term period."
_GROUNDED_CLAIM = ClaimOut(
    text="the metric value was 9.8 percent", atom_id="a1",
    quote="the approved metric value was 9.8 percent")
# a derivation with NO new hard token (survives no_new_hard_tokens) and a code-decidable
# kind (comparative → "valid" verdict yields the strongest "inference" label).
_DERIV_CONCLUSION = "the metric outcome is comparatively strong versus the baseline"


class RoutingLLM:
    """Dispatches by `response_format.__name__` so one fake serves every call site.
    Records each compose prompt (`compose_users`) and serves compose answers from a queue."""

    def __init__(self, steps, compose_answers):
        self._steps = list(steps)
        self._compose_answers = list(compose_answers)
        self.compose_users: list[str] = []
        self.derive_proposed = 0
        self.derive_judged = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        name = getattr(response_format, "__name__", "")
        if name == "DeriveCandidates":
            self.derive_proposed += 1
            return LLMResult(
                parsed=response_format(derivations=[
                    DeriveCandidate(conclusion=_DERIV_CONCLUSION, basis=[1],
                                    kind="comparative", warrant="follows from the finding",
                                    falsifier="")]),
                output_tokens=3, model="fake")
        if name == "_Verdicts":
            # derive's validity judge → mark the (only) candidate valid
            self.derive_judged += 1
            return LLMResult(
                parsed=response_format(verdicts=[
                    {"index": i, "verdict": "valid", "reason": "follows"} for i in range(1, 6)]),
                output_tokens=3, model="fake")
        if name == "_Ranking":
            return LLMResult(parsed=response_format(order=[1, 2, 3, 4, 5, 6]), output_tokens=3, model="fake")
        if name == "ComposedAnswer":
            self.compose_users.append(messages[0]["content"])
            ans = self._compose_answers.pop(0) if self._compose_answers else "fallback default [1]"
            return LLMResult(parsed=response_format(answer=ans), output_tokens=5, model="fake")
        # planner AgentStep
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3, model="fake")


def _corpus() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_BLOCK_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"})))
    return src


async def _drive(*, deep_synthesis, kind, compose_answers=("A clean synthesis. [1]",)):
    llm = RoutingLLM(
        [AgentStep(action="search", query="metric value"),
         AgentStep(action="answer", claims=[_GROUNDED_CLAIM])],
        compose_answers=compose_answers)
    res = await run_react(
        question="what was the metric value and what does it imply?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=_corpus(),
        tenant_id="A", budget=BudgetState(max_calls=40), max_steps=4,
        answer_format="NORMAL_FMT_SENTINEL", deep_answer_format="DEEP_FMT_SENTINEL",
        deep_synthesis=deep_synthesis, kind=kind)
    return res, llm


@pytest.mark.asyncio
async def test_off_byte_identical_compose_prompt() -> None:
    # OFF guard: no derive, no deep format base, no derivations block.
    res, llm = await _drive(deep_synthesis=False, kind="management")
    assert res.grounded and len(res.verified_claims) == 1
    assert llm.derive_proposed == 0, "derive must not run when the flag is OFF"
    cu = llm.compose_users[-1]
    assert "GROUNDED DERIVATIONS" not in cu
    assert "NORMAL_FMT_SENTINEL" in cu            # normal base directive threaded through
    assert "DEEP_FMT_SENTINEL" not in cu          # deep format never used
    assert not res.derivations


@pytest.mark.asyncio
async def test_lookup_stays_crisp_with_deep_on() -> None:
    # deep ON but kind==lookup → crisp: no derive, no deep format.
    res, llm = await _drive(deep_synthesis=True, kind="lookup")
    assert llm.derive_proposed == 0, "lookup must not trigger the derive-weave"
    cu = llm.compose_users[-1]
    assert "GROUNDED DERIVATIONS" not in cu
    assert "NORMAL_FMT_SENTINEL" in cu and "DEEP_FMT_SENTINEL" not in cu


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["management", "understanding"])
async def test_deep_weaves_derivations_into_compose(kind) -> None:
    # deep ON + non-lookup → derive runs, its [D1] block is in the compose prompt, deep base used.
    res, llm = await _drive(deep_synthesis=True, kind=kind)
    assert llm.derive_proposed == 1 and llm.derive_judged == 1, "derive gate must run once"
    cu = llm.compose_users[-1]
    assert "GROUNDED DERIVATIONS" in cu, cu
    assert "[D1] inference:" in cu, cu                       # gate assigned the epistemic label
    assert _DERIV_CONCLUSION in cu                           # the derived conclusion is woven in
    assert "DEEP_FMT_SENTINEL" in cu                         # deep format is the compose base
    assert res.derivations and res.derivations[0].label == "inference"


@pytest.mark.asyncio
async def test_prose_audit_recomposes_once_on_unsupported_figure() -> None:
    # first compose emits an unsupported figure (42.7); the audit recomposes ONCE with a fix
    # instruction; the recompose is clean → no fallback.
    res, llm = await _drive(
        deep_synthesis=True, kind="management",
        compose_answers=["The metric rose 42.7 percent overall [1].",   # unsupported 42.7
                         "The metric outcome is qualitatively strong [1]."])  # clean recompose
    assert len(llm.compose_users) == 2, "exactly one recompose expected"
    assert "GROUNDING FIX" in llm.compose_users[1], "the recompose carried the fix instruction"
    assert "42.7" not in res.composed_answer
    assert res.deep_synthesis_fell_back is False


@pytest.mark.asyncio
async def test_prose_audit_falls_back_to_non_deep_when_recompose_still_unsupported() -> None:
    # recompose STILL unsupported → fall back to the non-deep compose (answer_format base).
    res, llm = await _drive(
        deep_synthesis=True, kind="management",
        compose_answers=["The metric rose 42.7 percent [1].",     # unsupported
                         "Actually it was 55.5 percent [1].",      # recompose still unsupported
                         "The metric outcome is qualitatively strong [1]."])  # non-deep fallback, clean
    assert len(llm.compose_users) == 3, "original + recompose + non-deep fallback"
    # the fallback compose used the NON-DEEP base directive
    assert "NORMAL_FMT_SENTINEL" in llm.compose_users[2] and "DEEP_FMT_SENTINEL" not in llm.compose_users[2]
    assert res.deep_synthesis_fell_back is True
    assert "42.7" not in res.composed_answer and "55.5" not in res.composed_answer
