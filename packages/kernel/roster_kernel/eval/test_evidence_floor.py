"""Phase-1 eval machinery: evidence_floor + risk-weighting + the tier flowing end-to-end.

Deterministic, domain-free. Proves (a) the scorer gates on evidence tier, (b) the summary is
risk-weighted with a hard critical gate, and (c) a vertical classifier stamps each verified claim's
evidence_kind through run_react so the eval can check it.
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.eval.qa_scoring import QaAnswer, QaCase, QaClaim, score_qa
from roster_kernel.eval.runner import _answer_from_result, summarize
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, AnswerResult, ClaimOut, VerifiedClaim, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


def _ans(prose, claims):
    return QaAnswer(prose=prose, refused=False, claims=tuple(claims))


def test_evidence_floor_gates_on_tier():
    case = QaCase(id="c", expected_values=("53%",), evidence_floor_kinds=("rct", "systematic_review"))
    # met: a verified RCT-tier claim → floor ok → fully correct
    ok = score_qa(case, _ans("response was 53%", [QaClaim("x", True, evidence_kind="rct")]))
    assert ok.evidence_floor_ok and ok.fully_correct
    # not met: only a cohort-tier claim → floor fails → NOT fully correct (even though 53% is present)
    bad = score_qa(case, _ans("response was 53%", [QaClaim("x", True, evidence_kind="cohort")]))
    assert not bad.evidence_floor_ok and not bad.fully_correct


def test_evidence_floor_skipped_on_refusal_and_when_unset():
    refuse = QaCase(id="r", expect_kind="refuse", evidence_floor_kinds=("rct",))
    assert score_qa(refuse, QaAnswer(prose="", refused=True)).evidence_floor_ok
    nofloor = QaCase(id="n", expected_values=("5 mg",))
    assert score_qa(nofloor, _ans("5 mg daily", [QaClaim("x", True)])).evidence_floor_ok


def test_absence_contract():
    case = QaCase(id="a", expect_kind="absence",
                  forbidden_phrases=("the approved product is",))
    # correct: flags a coverage gap + doesn't confabulate → pass (grounding on context is fine)
    ok = score_qa(case, QaAnswer(prose="There is no approved therapy; here is the related research.",
                                 claims=(QaClaim("x", True),),
                                 coverage_gaps=("no approved therapy exists",)))
    assert ok.fully_correct
    # did NOT recognize the absence (no gap flagged) → fail
    nogap = score_qa(case, QaAnswer(prose="Here are some options.", claims=(QaClaim("x", True),)))
    assert not nogap.fully_correct
    # confabulated that it exists → fail even with a gap
    confab = score_qa(case, QaAnswer(prose="The approved product is drugX.",
                                     coverage_gaps=("partial",)))
    assert not confab.fully_correct


def test_summarize_risk_weight_and_critical_gate():
    # one high-risk FAIL + two low-risk passes: overall not ok (critical gate), weighted rate reflects risk
    scores = {
        "hi": score_qa(QaCase(id="hi", expected_values=("9",), clinical_risk="high"),
                       _ans("value is 7", [QaClaim("x", True)])),          # fails (9 absent)
        "lo1": score_qa(QaCase(id="lo1", expected_values=("3",), clinical_risk="low"),
                        _ans("value is 3", [QaClaim("x", True)])),
        "lo2": score_qa(QaCase(id="lo2", expected_values=("4",), clinical_risk="low"),
                        _ans("value is 4", [QaClaim("x", True)])),
    }
    s = summarize(scores)
    assert s["critical_failures"] == ["hi"] and s["ok"] is False
    # weighted: passed low-risk weight 1+1=2 of total 8(high)+1+1=10
    assert abs(s["risk_weighted_pass_rate"] - 0.2) < 1e-9
    assert s["pass_rate"] == 2 / 3


def test_answer_from_result_carries_evidence_kind():
    res = AnswerResult(composed_answer="Drug A response 53% [1].")
    res.verified_claims = [VerifiedClaim("Drug A response 53%", "a1", "53%", source_key="europepmc",
                                         facets={"pub_type": "RCT"}, evidence_kind="rct")]
    qa = _answer_from_result(res)
    assert qa.prose.startswith("Drug A") and qa.claims[0].evidence_kind == "rct"
    assert qa.claims[0].citation_facets.get("pub_type") == "RCT"


class _Scripted:
    def __init__(self, steps): self._s = list(steps)
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        from roster_kernel.research.react import ComposedAnswer
        # compose step: emit a grounded answer (so composed_answer is real, not the fail-note fallback)
        if "VERIFIED FINDINGS" in messages[0]["content"]:
            return LLMResult(parsed=ComposedAnswer(answer="The response rate was 53% [1]."), output_tokens=5)
        return LLMResult(parsed=self._s.pop(0), output_tokens=5)


def test_classifier_stamps_evidence_kind_end_to_end():
    # A faceted RCT block → the vertical classifier stamps evidence_kind='rct' on the verified claim,
    # which the eval's evidence_floor can then check. Proves the hook flows through run_react.
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A",
                         text="In the phase 3 trial the response rate was 53% at 12 months.",
                         locator=Locator("block_span", "d1", {"block_id": "b1"}),
                         facets={"study_type": "interventional", "phase": "phase3"}))

    def classify(source_key, facets, title="", text=""):
        return "rct" if (facets or {}).get("study_type") == "interventional" else ""

    llm = _Scripted([
        AgentStep(action="search", query="response rate"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the response rate was 53%", atom_id="a1",
                     quote="the response rate was 53%")]),
    ])
    res = asyncio.run(run_react(question="what was the response rate?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20), classify_evidence=classify))
    assert res.grounded and res.verified_claims[0].evidence_kind == "rct"
    # and the eval floor now passes for this answer
    case = QaCase(id="e2e", expected_values=("53%",), evidence_floor_kinds=("rct",))
    assert score_qa(case, _answer_from_result(res)).fully_correct
