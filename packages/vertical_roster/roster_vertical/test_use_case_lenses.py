"""Use-case lenses: each mode is a well-formed {directive, suppress_authority} dict, wired into the
manifest's answer_modes, and the OPINION lenses (foresight/wisdom) neutralize the authority tier-boost
while the FACT lenses (market/moat/genesis/whitespace) keep it. The acquirer mode stays a plain string
(back-compat). This locks the contract the app layer reads."""
from __future__ import annotations

from .use_case_lenses import USE_CASE_LENSES


def test_lenses_are_wellformed():
    for name, lens in USE_CASE_LENSES.items():
        assert isinstance(lens, dict), name
        assert isinstance(lens.get("directive"), str) and lens["directive"].strip(), name
        assert isinstance(lens.get("suppress_authority"), bool), name


def test_opinion_lenses_suppress_authority_fact_lenses_do_not():
    # foresight + wisdom lead with expert opinion/discussion → neutralize the tier boost so those
    # sources aren't demoted below filings; the rest keep authority ordering.
    assert USE_CASE_LENSES["foresight"]["suppress_authority"] is True
    assert USE_CASE_LENSES["wisdom"]["suppress_authority"] is True
    for m in ("genesis", "market", "whitespace", "moat"):
        assert USE_CASE_LENSES[m]["suppress_authority"] is False


def test_wired_into_manifest_answer_modes():
    from .manifest import build_manifest
    modes = build_manifest().answer_modes
    for m in USE_CASE_LENSES:
        assert m in modes
    # acquirer stays a plain-string directive (back-compat with the dict|str app resolution)
    assert isinstance(modes["acquirer"], str)
