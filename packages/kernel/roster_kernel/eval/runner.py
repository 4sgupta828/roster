"""Eval runner — run the vertical's held-out gold through the agent and score it.

Ties the (generic) qa scorer to a vertical's eval_gold. Decoupled from the runtime
(takes an `ask` callable) so it doesn't drag in providers. Run it in `replay` for
free (against captured cassettes) as a CI gate; the one-time baseline is a `record`
run — the single budgeted-credit action.
"""
from __future__ import annotations

from typing import Awaitable, Callable

from roster_kernel.eval.qa_scoring import QaAnswer, QaCase, QaClaim, QaScore, score_qa
from roster_kernel.research.react import AnswerResult

AskFn = Callable[..., Awaitable[AnswerResult]]


def _answer_from_result(res: AnswerResult) -> QaAnswer:
    # Score the COMPOSED prose when compose succeeded (the user-facing answer); fall back to the
    # concatenated verified findings when compose failed or wasn't produced (keeps scripted CI runs that
    # don't script a compose step working). Each claim carries its evidence tier + facets so a case's
    # evidence_floor / citation_constraints are checkable.
    composed = (res.composed_answer or "").strip()
    prose = composed if (composed and not getattr(res, "compose_failed", False)) \
        else " ".join(f"{c.text} {c.quote}" for c in res.verified_claims)
    return QaAnswer(
        prose=prose,
        refused=not res.grounded,
        coverage_gaps=tuple(getattr(res, "coverage_gaps", ()) or ()),
        claims=tuple(
            QaClaim(text=c.text, verified=True,
                    citation_facets=dict(getattr(c, "facets", {}) or {},
                                         **({"source_class": c.source_key} if c.source_key else {})),
                    evidence_kind=getattr(c, "evidence_kind", "") or "")
            for c in res.verified_claims
        ),
    )


def _case_from_gold(case_id: str, spec: dict) -> QaCase:
    return QaCase(
        id=case_id,
        expect_kind=spec.get("expect", "value"),
        expected_values=tuple(spec.get("expected_values", ())),
        forbidden_values=tuple(spec.get("forbidden_values", ())),
        required_phrases=tuple(spec.get("required_phrases", ())),
        forbidden_phrases=tuple(spec.get("forbidden_phrases", ())),
        citation_constraints=tuple(spec.get("citation_constraints", ())),
        evidence_floor_kinds=tuple(spec.get("evidence_floor_kinds", ())),
        clinical_risk=spec.get("clinical_risk", "low"),
        category=spec.get("category", ""),
    )


async def run_qa_eval(ask: AskFn, gold: dict, *, tenant_id: str,
                      source_keys: list[str] | None = None) -> dict[str, QaScore]:
    scores: dict[str, QaScore] = {}
    for case_id, spec in gold.items():
        res = await ask(question=spec["question"], tenant_id=tenant_id, source_keys=source_keys)
        scores[case_id] = score_qa(_case_from_gold(case_id, spec), _answer_from_result(res))
    return scores


# Risk weights (thesis §11: optimize for expected clinical harm, not average rubric score).
_RISK_WEIGHT = {"low": 1, "med": 3, "high": 8}


def summarize(scores: dict[str, QaScore]) -> dict:
    total = len(scores) or 1
    passed = sum(1 for s in scores.values() if s.fully_correct)
    failing = [cid for cid, s in scores.items() if not s.fully_correct]
    # Risk-weighted score: a high-risk failure costs far more than a minor one.
    wsum = sum(_RISK_WEIGHT.get(s.clinical_risk, 1) for s in scores.values()) or 1
    wpass = sum(_RISK_WEIGHT.get(s.clinical_risk, 1) for s in scores.values() if s.fully_correct)
    # CRITICAL gate: any HIGH-risk case failing fails the suite regardless of overall pass-rate.
    critical_failures = [cid for cid, s in scores.items()
                         if not s.fully_correct and s.clinical_risk == "high"]
    return {
        "passed": passed, "total": len(scores), "pass_rate": passed / total,
        "risk_weighted_pass_rate": wpass / wsum,
        "critical_failures": critical_failures,
        "ok": passed == len(scores) and not critical_failures,
        "failing": failing,
        # evidence-fitness signal: how many non-refusal cases met their evidence_floor
        "evidence_floor_met": sum(1 for s in scores.values() if s.evidence_floor_ok),
    }
