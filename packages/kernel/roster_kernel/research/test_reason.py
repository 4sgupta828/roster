"""The grounded-reasoning GATE, tested deterministically with a fake LLM (no network).

Proves the load-bearing properties: a candidate with no valid basis or a fabricated figure is dropped
by CODE before any judge; the epistemic label is assigned by the gate from (kind, validity verdict),
never self-declared; an ARBITRARY leap is never emitted as inference (suppressed, or at most a labeled
speculation in idea mode); a judge failure demotes rather than upgrades.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .reason import (DeriveCandidate, DeriveCandidates, DerivedClaim, _Ranking, _Verdict, _Verdicts,
                     _dedupe, derive)


@dataclass
class _F:                       # minimal stand-in for a VerifiedClaim
    text: str
    quote: str = ""


# Findings: three margins, transitively orderable. Note NO finding states "A > C" or any acquisition.
_FINDINGS = [
    _F("Company A operating margin is 40%."),
    _F("Company B operating margin is 30%."),
    _F("Company C operating margin is 25%."),
]


class _FakeLLM:
    """Returns canned candidates on the generate call and canned verdicts on the judge call, dispatched
    by response_format. `cands`/`verdicts` are what the 'model' proposes; the GATE decides what survives."""
    def __init__(self, cands, verdicts, order=None):
        self._cands, self._verdicts, self._order = cands, verdicts, order

    async def complete(self, *, system, messages, response_format, max_tokens):
        if response_format is DeriveCandidates:
            parsed = DeriveCandidates(derivations=self._cands)
        elif response_format is _Ranking:
            parsed = _Ranking(order=self._order or [])
        else:
            parsed = _Verdicts(verdicts=self._verdicts)
        return type("R", (), {"parsed": parsed, "output_tokens": 0})()


def _run(cands, verdicts, **kw):
    llm = _FakeLLM(cands, verdicts)
    return asyncio.run(derive("Which company is most efficient?", _FINDINGS, llm, **kw))


def test_valid_comparative_becomes_inference():
    out = _run(
        [DeriveCandidate(conclusion="A has a higher margin than C.", basis=[1, 3],
                         kind="comparative", warrant="40% > 25%")],
        [_Verdict(index=1, verdict="valid")])
    assert len(out) == 1 and out[0].label == "inference"
    assert out[0].basis == (1, 3)


def test_arbitrary_leap_is_dropped_not_shipped():
    # "A will acquire C" does NOT follow from margins — the judge calls it arbitrary → dropped.
    out = _run(
        [DeriveCandidate(conclusion="A will acquire C.", basis=[1, 3], kind="causal",
                         warrant="A is bigger", falsifier="no acquisition announced")],
        [_Verdict(index=1, verdict="arbitrary")])
    assert out == []                       # never emitted


def test_arbitrary_survives_only_as_labeled_speculation_in_idea_mode():
    out = _run(
        [DeriveCandidate(conclusion="A could roll up smaller-margin peers like C.", basis=[1, 3],
                         kind="opportunity", warrant="margin headroom", falsifier="no M&A capacity")],
        [_Verdict(index=1, verdict="arbitrary")], generate_ideas=True)
    assert len(out) == 1 and out[0].label == "speculation"   # quarantined, never inference


def test_valid_causal_needs_a_falsifier_to_survive():
    # a valid-but-jump causal step with NO falsifier is dropped (can't be an unfalsifiable claim)
    no_fals = _run([DeriveCandidate(conclusion="A's margin lead will compound.", basis=[1],
                                    kind="causal", warrant="scale economics")],
                   [_Verdict(index=1, verdict="valid")])
    assert no_fals == []
    with_fals = _run([DeriveCandidate(conclusion="A's margin lead will compound.", basis=[1],
                                      kind="causal", warrant="scale economics",
                                      falsifier="margins converge next filing")],
                     [_Verdict(index=1, verdict="valid")])
    assert len(with_fals) == 1 and with_fals[0].label == "hypothesis"


def test_no_basis_dropped_by_code_before_judge():
    # basis empty AND out-of-range → structural drop; the (would-be "valid") verdict never matters.
    out = _run([DeriveCandidate(conclusion="A is the best company.", basis=[], kind="comparative")],
               [_Verdict(index=1, verdict="valid")])
    assert out == []


def test_fabricated_figure_dropped_by_code():
    # introduces "$5 billion" — a hard token in NO basis finding, non-arithmetic → structural drop.
    out = _run([DeriveCandidate(conclusion="A is worth $5 billion given its 40% margin.", basis=[1],
                                kind="implication", warrant="high margin", falsifier="an independent estimate differs")],
               [_Verdict(index=1, verdict="valid")])
    assert out == []


def test_arithmetic_new_figure_allowed_when_operands_grounded():
    findings = [_F("A revenue is 100."), _F("A margin is 40%.")]
    llm = _FakeLLM([DeriveCandidate(conclusion="A profit is 40.", basis=[1, 2], kind="arithmetic",
                                    warrant="100 * 40%")],
                   [_Verdict(index=1, verdict="valid")])
    out = asyncio.run(derive("profit?", findings, llm))
    assert len(out) == 1 and out[0].label == "inference"


def test_dedupe_keeps_stronger_label_of_a_duplicate_pair():
    a = DerivedClaim(conclusion="Project A has the most GitHub stars overall.", basis=(1,),
                     kind="comparative", label="hypothesis")
    b = DerivedClaim(conclusion="Project A has the most GitHub stars.", basis=(1,),
                     kind="comparative", label="inference")   # same point, stronger label
    c = DerivedClaim(conclusion="Project C is growing the fastest by rate.", basis=(2,),
                     kind="comparative", label="inference")   # distinct point
    kept = _dedupe([a, b, c])
    assert len(kept) == 2                                     # the a/b duplicate collapses to one
    keptset = {k.conclusion for k in kept}
    assert "Project A has the most GitHub stars." in keptset  # kept the inference, dropped the hypothesis
    assert any("growing the fastest" in k.conclusion for k in kept)


def test_prioritize_caps_and_orders_by_ranker():
    # 6 GENUINELY DISTINCT valid derivations → 6 > cap(2) → ranker selects 2, in its order.
    texts = [
        "Group A leads on adoption today.",
        "Adoption is accelerating in the larger segment.",
        "Size does not predict the eventual winner here.",
        "Momentum favors the smaller entrant here.",
        "Outcomes track team quality more than the approach.",
        "The early lead is eroding for the leader.",
    ]
    cands = [DeriveCandidate(conclusion=t, basis=[1], kind="comparative", warrant="w") for t in texts]
    verdicts = [_Verdict(index=i, verdict="valid") for i in range(1, 7)]
    llm = _FakeLLM(cands, verdicts, order=[4, 2])             # ranker prefers #4 then #2
    out = asyncio.run(derive("Which matters?", _FINDINGS, llm, max_out=2))
    assert len(out) == 2
    assert "smaller entrant" in out[0].conclusion and "accelerating" in out[1].conclusion


class _MaxTokenRecorder:
    """Fake LLM that records the max_tokens passed on EACH call, keyed by response_format,
    so a test can assert the judge call got the judge_max_tokens value (not the gen value)."""
    def __init__(self):
        self.seen: dict = {}

    async def complete(self, *, system, messages, response_format, max_tokens):
        self.seen[response_format] = max_tokens
        if response_format is DeriveCandidates:
            parsed = DeriveCandidates(derivations=[
                DeriveCandidate(conclusion="A has a higher margin than C.", basis=[1, 3],
                                kind="comparative", warrant="40% > 25%")])
        elif response_format is _Ranking:
            parsed = _Ranking(order=[])
        else:
            parsed = _Verdicts(verdicts=[_Verdict(index=1, verdict="valid")])
        return type("R", (), {"parsed": parsed, "output_tokens": 0})()


def test_derive_passes_judge_max_tokens_through_to_the_judge_call():
    # The validity-judge call must receive the caller's judge_max_tokens (default 4000 ≥ the
    # former hardcoded 1200), while candidate generation still uses max_tokens — proving a
    # truncated verdict list can be fixed by the caller without touching the kernel.
    rec = _MaxTokenRecorder()
    out = asyncio.run(derive("Which company is most efficient?", _FINDINGS, rec,
                             max_tokens=2000, judge_max_tokens=7777))
    assert len(out) == 1 and out[0].label == "inference"
    assert rec.seen[_Verdicts] == 7777          # judge got the passed value
    assert rec.seen[DeriveCandidates] == 2000   # generation untouched


def test_derive_judge_max_tokens_defaults_to_at_least_1200():
    # Default must not regress below the old hardcoded 1200 (callers only improve).
    rec = _MaxTokenRecorder()
    asyncio.run(derive("Which company is most efficient?", _FINDINGS, rec))
    assert rec.seen[_Verdicts] >= 1200
    assert rec.seen[_Verdicts] == 4000


def test_judge_failure_demotes_does_not_upgrade():
    # no verdict returned for the candidate → conservative "plausible" → hypothesis (needs falsifier),
    # NEVER inference. Proves a judge outage can't mint a confident inference.
    out = _run([DeriveCandidate(conclusion="A leads its peers on efficiency.", basis=[1, 2, 3],
                                kind="comparative", warrant="highest margin",
                                falsifier="a peer reports higher margin")],
               [])   # empty verdicts
    assert len(out) == 1 and out[0].label == "hypothesis"
