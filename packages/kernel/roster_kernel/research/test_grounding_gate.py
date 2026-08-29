"""Cross-family SEMANTIC grounding gate (ROSTER_INTELLIGENCE_CORE / T4).

Two layers, mirroring the factra `finding_grounding_gate` port:

1. UNIT — `cross_family_ground_check` in isolation: a fake cross-family judge that
   flags an ungrounded mechanism sentence → the gate returns it; the FAIL-CLOSED
   contract (judge None / judge error / no verified claims / empty answer → []);
   budget is charged on a real call.

2. run_react-LEVEL — the REAL `run_react` in intelligence mode (`hypotheses`
   present) with a SEPARATE cross-family `derive_judge_llm`:
   - judge flags a span → a recompose fires (the 2nd compose call carries the
     GROUNDING FIX and the flagged span is gone);
   - judge ERROR → NO recompose, the answer is unchanged (fail-closed to today's
     hard-token audit result);
   - same-family (`derive_judge_llm is llm`) → gate never runs (never same-family);
   - OFF (`hypotheses=None`) → the judge is never called (byte-identical).

Sync tests via asyncio.run (the contamination-free sibling pattern).
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.grounding_gate import GroundingProbe, cross_family_ground_check
from roster_kernel.research.refuter import RefuterQueries
from roster_kernel.research.intelligence_draft import Hypothesis
from roster_kernel.research.react import AgentStep, ClaimOut, VerifiedClaim, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


# ===========================================================================
# UNIT — cross_family_ground_check in isolation
# ===========================================================================

class _FakeJudge:
    """A different-family judge (temp 0, closed shape). Returns a fixed span list and
    records that it was asked for the GroundingProbe shape at temperature 0."""

    def __init__(self, spans, output_tokens=7):
        self._spans = list(spans)
        self._tok = output_tokens
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.calls += 1
        assert response_format is GroundingProbe        # closed shape
        assert temperature == 0                          # deterministic audit
        return LLMResult(parsed=GroundingProbe(unsupported=list(self._spans)),
                         output_tokens=self._tok, model="gpt-judge")


class _BoomJudge:
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        raise RuntimeError("judge provider down")


class _NoFieldModel:
    """A parsed object missing `unsupported` — the gate must fail closed on it."""


class _WeirdJudge:
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=_NoFieldModel(), output_tokens=3, model="gpt-judge")


def _claims():
    return [
        VerifiedClaim(text="the metric value was 9.8 percent", atom_id="a1",
                      quote="the approved metric value was 9.8 percent"),
    ]


def test_flags_ungrounded_mechanism_and_charges_budget() -> None:
    judge = _FakeJudge(["Acme routes packets through a novel photonic mesh."])
    budget = BudgetState(max_calls=10)
    out = asyncio.run(cross_family_ground_check(
        "Acme routes packets through a novel photonic mesh. The metric was 9.8 percent [1].",
        _claims(), judge, budget=budget))
    assert out == ["Acme routes packets through a novel photonic mesh."]
    assert judge.calls == 1
    assert budget.spent_calls == 1 and budget.spent_tokens == 7   # charged from output_tokens


def test_none_judge_fails_closed() -> None:
    budget = BudgetState(max_calls=10)
    out = asyncio.run(cross_family_ground_check("anything [1].", _claims(), None, budget=budget))
    assert out == []
    assert budget.spent_calls == 0                                 # no call, no charge


def test_judge_error_fails_closed() -> None:
    budget = BudgetState(max_calls=10)
    out = asyncio.run(cross_family_ground_check("anything [1].", _claims(), _BoomJudge(), budget=budget))
    assert out == []                                               # ANY judge error → [] (no action)


def test_no_verified_claims_fails_closed() -> None:
    judge = _FakeJudge(["would-have-flagged"])
    budget = BudgetState(max_calls=10)
    out = asyncio.run(cross_family_ground_check("anything [1].", [], judge, budget=budget))
    assert out == []
    assert judge.calls == 0                                        # never reaches the judge


def test_empty_answer_fails_closed() -> None:
    judge = _FakeJudge(["would-have-flagged"])
    out = asyncio.run(cross_family_ground_check("   [1]  ", _claims(), judge, budget=BudgetState(max_calls=10)))
    assert out == []
    assert judge.calls == 0                                        # markers-only → nothing to judge


def test_malformed_output_fails_closed() -> None:
    out = asyncio.run(cross_family_ground_check("something [1].", _claims(), _WeirdJudge(),
                                                budget=BudgetState(max_calls=10)))
    assert out == []                                               # no `unsupported` list → []


def test_clean_answer_returns_empty() -> None:
    judge = _FakeJudge([])                                          # judge finds nothing wrong
    out = asyncio.run(cross_family_ground_check("The metric was 9.8 percent [1].", _claims(), judge,
                                                budget=BudgetState(max_calls=10)))
    assert out == [] and judge.calls == 1


# ===========================================================================
# run_react-LEVEL — the gate wired into the real compose path
# ===========================================================================

_BLOCK_TEXT = "The approved metric value was 9.8 percent for the term period."
_GROUNDED_CLAIM = ClaimOut(
    text="the metric value was 9.8 percent", atom_id="a1",
    quote="the approved metric value was 9.8 percent")


class RoutingLLM:
    """The COMPOSER (same family as the run). Serves planner AgentSteps then compose
    answers from a queue, recording each compose prompt. It must NEVER be asked for a
    GroundingProbe — that would mean the gate ran same-family (a bug)."""

    def __init__(self, steps, compose_answers):
        self._steps = list(steps)
        self._compose_answers = list(compose_answers)
        self.compose_users: list[str] = []
        self.grounding_asked = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        name = getattr(response_format, "__name__", "")
        if name == "GroundingProbe":
            self.grounding_asked += 1                    # the gate must never route here
            return LLMResult(parsed=GroundingProbe(unsupported=[]), output_tokens=1, model="fake")
        if name == "ComposedAnswer":
            self.compose_users.append(messages[0]["content"])
            ans = self._compose_answers.pop(0) if self._compose_answers else "fallback [1]"
            return LLMResult(parsed=response_format(answer=ans), output_tokens=5, model="fake")
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3, model="fake")


class ScriptedJudge:
    """The cross-family grounding judge (a DISTINCT object from the composer). Pops a
    verdict (list of flagged spans) per call; records the answer text it saw."""

    def __init__(self, verdicts):
        self._verdicts = list(verdicts)
        self.calls = 0
        self.answers_seen: list[str] = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        # T-B: in intelligence mode the SAME cross-family judge also authors the red-team refuter's
        # disconfirming queries. Return an empty query set (→ retrieval falls back to the self-authored
        # against_query) and do NOT count it as a grounding-gate call.
        if getattr(response_format, "__name__", "") == "RefuterQueries":
            return LLMResult(parsed=RefuterQueries(queries=[]), output_tokens=1, model="gpt-judge")
        self.calls += 1
        self.answers_seen.append(messages[0]["content"])
        assert response_format is GroundingProbe
        v = self._verdicts.pop(0) if self._verdicts else []
        return LLMResult(parsed=GroundingProbe(unsupported=list(v)), output_tokens=4, model="gpt-judge")


class BoomJudge:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.calls += 1
        raise RuntimeError("judge down")


def _corpus() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_BLOCK_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"})))
    return src


def _hyps():
    return [Hypothesis(claim="H1: the metric drove the outcome",
                       for_query="metric value", against_query="metric value",
                       falsifier="the metric was unrelated to the outcome")]


def _drive(*, hypotheses, derive_judge_llm, compose_answers):
    llm = RoutingLLM(
        [AgentStep(action="search", query="metric value"),
         AgentStep(action="answer", claims=[_GROUNDED_CLAIM])],
        compose_answers=list(compose_answers))
    res = asyncio.run(run_react(
        question="what was the metric value and what does it imply?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=_corpus(),
        tenant_id="A", budget=BudgetState(max_calls=60), max_steps=4,
        answer_format="NORMAL_FMT_SENTINEL",
        hypotheses=hypotheses, derive_judge_llm=derive_judge_llm,
        collect_diagnostics=True))
    return res, llm


def test_run_react_gate_recomposes_on_flagged_span() -> None:
    # intelligence mode + a cross-family judge that flags the first answer's mechanism, then
    # (after the recompose) finds it clean → exactly ONE recompose fires and the span is gone.
    judge = ScriptedJudge(verdicts=[
        ["Acme routes packets through a novel photonic mesh."],   # initial audit: flag
        [],                                                        # re-check after recompose: clean
    ])
    res, llm = _drive(
        hypotheses=_hyps(), derive_judge_llm=judge,
        compose_answers=[
            "Acme routes packets through a novel photonic mesh. The metric value was 9.8 percent [1].",
            "The metric value was 9.8 percent [1].",              # clean recompose
        ])
    assert res.grounded and len(res.verified_claims) == 1
    assert judge.calls == 2, "initial audit + one re-check after the recompose"
    assert len(llm.compose_users) == 2, "exactly one recompose fired"
    assert "GROUNDING FIX" in llm.compose_users[1], "the recompose carried the fix instruction"
    assert "photonic mesh" not in res.composed_answer
    assert llm.grounding_asked == 0, "the gate must NEVER run same-family (composer never asked to judge)"
    diag = (res.diagnostics or {}).get("intelligence_grounding")
    assert diag and diag["recomposed"] is True and diag["resolved"] is True, diag


def test_run_react_judge_error_no_recompose_answer_unchanged() -> None:
    # judge ERROR → the gate fails closed: NO recompose, the composed answer is unchanged
    # (today's hard-token audit result stands). The ungrounded sentence survives — proving the
    # gate did NOT weaken grounding by fabricating a pass; it simply took no action.
    judge = BoomJudge()
    original = "Acme uses a novel photonic mesh. The metric value was 9.8 percent [1]."
    res, llm = _drive(hypotheses=_hyps(), derive_judge_llm=judge, compose_answers=[original])
    assert judge.calls >= 1, "the gate attempted the judge"
    assert len(llm.compose_users) == 1, "no recompose on a judge error (fail-closed)"
    assert res.composed_answer == original, "answer unchanged when the judge is unavailable"


def test_run_react_same_family_judge_skipped() -> None:
    # derive_judge_llm IS the composer llm (same family) → the gate must SKIP entirely (never
    # run same-family). No recompose; the composer is never asked for a GroundingProbe.
    llm = RoutingLLM(
        [AgentStep(action="search", query="metric value"),
         AgentStep(action="answer", claims=[_GROUNDED_CLAIM])],
        compose_answers=["Acme uses a novel photonic mesh. The metric value was 9.8 percent [1]."])
    res = asyncio.run(run_react(
        question="what was the metric value and what does it imply?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=_corpus(),
        tenant_id="A", budget=BudgetState(max_calls=60), max_steps=4,
        answer_format="NORMAL_FMT_SENTINEL",
        hypotheses=_hyps(), derive_judge_llm=llm,          # SAME object as the composer
        collect_diagnostics=True))
    assert res.grounded and len(res.verified_claims) == 1
    assert len(llm.compose_users) == 1, "no recompose — the gate skipped same-family"
    assert llm.grounding_asked == 0, "the gate never asked the (same-family) composer to judge"


def test_run_react_off_no_hypotheses_judge_never_called() -> None:
    # OFF (hypotheses=None) → the gate is not invoked at all; a provided judge stays untouched.
    judge = ScriptedJudge(verdicts=[["would-have-flagged"]])
    res, llm = _drive(
        hypotheses=None, derive_judge_llm=judge,
        compose_answers=["The metric value was 9.8 percent [1]."])
    assert res.grounded and len(res.verified_claims) == 1
    assert judge.calls == 0, "OFF path never calls the grounding judge (byte-identical)"
    assert len(llm.compose_users) == 1
    assert (res.diagnostics or {}).get("intelligence_grounding") is None
