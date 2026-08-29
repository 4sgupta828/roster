"""Offline tests for the re-authored generic QA scorer. Domain-neutral fixtures."""
from __future__ import annotations

from roster_kernel.eval.qa_scoring import QaAnswer, QaCase, QaClaim, score_qa


def _grounded(text="claim", **facets):
    return QaClaim(text=text, verified=True, citation_facets=facets)


def test_value_case_fully_correct() -> None:
    case = QaCase(id="v1", expected_values=("9.8%",), required_phrases=("approved",))
    ans = QaAnswer(prose="The approved figure was 9.8% overall.", claims=(_grounded(),))
    assert score_qa(case, ans).fully_correct


def test_standalone_number_guard() -> None:
    # expected 3.8 must NOT be satisfied by "13.8" appearing in prose.
    case = QaCase(id="v2", expected_values=("3.8",))
    ans = QaAnswer(prose="The value was 13.8 units.", claims=(_grounded(),))
    s = score_qa(case, ans)
    assert not s.values_present and not s.fully_correct


def test_forbidden_value_present_fails() -> None:
    case = QaCase(id="v3", expected_values=("5",), forbidden_values=("7",))
    ans = QaAnswer(prose="It was 5, definitely not 7.", claims=(_grounded(),))
    s = score_qa(case, ans)
    assert not s.forbidden_values_absent and not s.fully_correct


def test_refuse_case() -> None:
    case = QaCase(id="r1", expect_kind="refuse")
    assert score_qa(case, QaAnswer(prose="Not enough evidence.", refused=True)).fully_correct
    # answering when it should refuse is wrong
    assert not score_qa(case, QaAnswer(prose="It is 9.8%", refused=False)).fully_correct


def test_ungrounded_claim_fails() -> None:
    case = QaCase(id="v4", expected_values=("9.8%",))
    ans = QaAnswer(
        prose="It is 9.8%.",
        claims=(QaClaim(text="c", verified=False),),  # not verified
    )
    assert not score_qa(case, ans).citation_grounded
    assert not score_qa(case, ans).fully_correct


def test_citation_constraint_requires_matching_facet() -> None:
    # A generic "must cite a source of class=authoritative" constraint.
    case = QaCase(id="v5", expected_values=("9.8%",),
                  citation_constraints=({"source_class": "authoritative"},))
    ok = QaAnswer(prose="It is 9.8%.", claims=(_grounded(source_class="authoritative"),))
    bad = QaAnswer(prose="It is 9.8%.", claims=(_grounded(source_class="secondary"),))
    assert score_qa(case, ok).fully_correct
    assert not score_qa(case, bad).citation_grounded
