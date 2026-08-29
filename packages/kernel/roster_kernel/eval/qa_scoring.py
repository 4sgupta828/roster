"""Re-authored QA scorer — generic, domain-free, pure math (no I/O, no DB).

Kept from the prior system's qa scorer: value-present (with the standalone-number
guard so "3.8" doesn't match inside "13.8"), forbidden-values-absent, required/
forbidden phrases, refused-correctly, answered, and citation-grounded.

Attribution to a required source class is a GENERIC citation constraint the
vertical supplies per case (a facet requirement), not a hardcoded domain gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_NORM_STRIPS = re.compile(r"[\$,%]")
# A numeric token: leading digit, optional thousands/decimals, optional percent.
_NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


@dataclass(frozen=True)
class QaClaim:
    text: str
    verified: bool                       # produced by the vertical CitationVerifier
    citation_facets: dict[str, str] = field(default_factory=dict)
    evidence_kind: str = ""              # vertical-classified evidence tier (for the evidence_floor check)


@dataclass(frozen=True)
class QaAnswer:
    prose: str
    refused: bool = False
    claims: tuple[QaClaim, ...] = ()
    coverage_gaps: tuple[str, ...] = ()   # the run's flagged gaps (for the "absence" contract)


@dataclass(frozen=True)
class QaCase:
    id: str
    expect_kind: str = "value"           # "value" | "refuse" | "preservation"
    expected_values: tuple[str, ...] = ()
    forbidden_values: tuple[str, ...] = ()
    required_phrases: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = ()
    must_be_grounded: bool = True
    # Each constraint: a facet map a cited claim must ⊇ (generic source-class etc).
    citation_constraints: tuple[dict[str, str], ...] = ()
    # Evidence-floor: at least one VERIFIED claim's evidence_kind must be in this acceptable set
    # (e.g. ("rct","systematic_review","guideline") = "must rest on RCT-or-better"). Empty = no floor.
    # Domain-free: the vertical decides which tiers count; the scorer only checks set membership.
    evidence_floor_kinds: tuple[str, ...] = ()
    clinical_risk: str = "low"           # "low" | "med" | "high" — risk-weights a failure (thesis §11)
    category: str = ""


@dataclass
class QaScore:
    case_id: str
    values_present: bool
    forbidden_values_absent: bool
    phrases_ok: bool
    refused_correctly: bool
    answered: bool
    citation_grounded: bool
    evidence_floor_ok: bool
    fully_correct: bool
    clinical_risk: str = "low"


def _norm_num(s: str) -> str:
    return _NORM_STRIPS.sub("", s.strip().lower())


def _value_present(prose: str, value: str) -> bool:
    """Standalone match: `3.8` must NOT match inside `13.8` / `3.85`.

    Numeric values are compared against whole numeric TOKENS extracted from the
    prose (so a trailing sentence period isn't mistaken for a decimal). Non-numeric
    values fall back to a word-boundary substring match.
    """
    v = _norm_num(value)
    if not v:
        return False
    if any(ch.isdigit() for ch in v):
        return any(_norm_num(tok) == v for tok in _NUM_TOKEN.findall(prose))
    return re.search(rf"\b{re.escape(v)}\b", prose.lower()) is not None


def _facets_satisfy(have: dict[str, str], want: dict[str, str]) -> bool:
    return all(have.get(k) == v for k, v in want.items())


def score_qa(case: QaCase, answer: QaAnswer) -> QaScore:
    prose = answer.prose or ""
    low = prose.lower()

    values_present = all(_value_present(prose, v) for v in case.expected_values)
    forbidden_values_absent = not any(_value_present(prose, v) for v in case.forbidden_values)
    phrases_ok = (
        all(p.lower() in low for p in case.required_phrases)
        and not any(p.lower() in low for p in case.forbidden_phrases)
    )

    should_refuse = case.expect_kind == "refuse"
    refused_correctly = (answer.refused == should_refuse)
    answered = answer.refused is False if not should_refuse else True

    # ABSENCE contract: the asked entity does not exist → the correct answer STATES the absence (flags a
    # coverage gap) and does NOT confabulate it (forbidden phrases). Grounding is fine — the answer may
    # cite what IS known. This is the deterministic proxy for "recognized the absence" (Rule 4/6).
    if case.expect_kind == "absence":
        confabulated = any(p.lower() in low for p in case.forbidden_phrases)
        absence_ok = bool(answer.coverage_gaps) and not confabulated
        return QaScore(
            case_id=case.id, values_present=True, forbidden_values_absent=not confabulated,
            phrases_ok=not confabulated, refused_correctly=True, answered=True,
            citation_grounded=True, evidence_floor_ok=True, fully_correct=absence_ok,
            clinical_risk=case.clinical_risk)

    # Grounding: when we answered and grounding is required, every claim must be
    # verified AND every declared citation constraint met by some verified claim.
    if should_refuse or not case.must_be_grounded:
        citation_grounded = True
    else:
        all_verified = bool(answer.claims) and all(c.verified for c in answer.claims)
        constraints_met = all(
            any(c.verified and _facets_satisfy(c.citation_facets, want) for c in answer.claims)
            for want in case.citation_constraints
        )
        citation_grounded = all_verified and constraints_met

    # Evidence-floor: does a VERIFIED claim rest on an acceptable evidence tier? (skip on refusal /
    # when no floor declared). This is what measures whether evidence-fitness ranking actually surfaces
    # the right tier of evidence — provenance-adjacent, deterministic, domain-free (set membership).
    if should_refuse or not case.evidence_floor_kinds:
        evidence_floor_ok = True
    else:
        floor = set(case.evidence_floor_kinds)
        evidence_floor_ok = any(c.verified and c.evidence_kind in floor for c in answer.claims)

    if should_refuse:
        fully_correct = refused_correctly
    else:
        fully_correct = (
            values_present
            and forbidden_values_absent
            and phrases_ok
            and refused_correctly
            and answered
            and citation_grounded
            and evidence_floor_ok
        )

    return QaScore(
        case_id=case.id,
        values_present=values_present,
        forbidden_values_absent=forbidden_values_absent,
        phrases_ok=phrases_ok,
        refused_correctly=refused_correctly,
        answered=answered,
        citation_grounded=citation_grounded,
        evidence_floor_ok=evidence_floor_ok,
        fully_correct=fully_correct,
        clinical_risk=case.clinical_risk,
    )
