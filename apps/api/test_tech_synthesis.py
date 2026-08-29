"""Tech-synthesis (ROSTER_TECH_SYNTHESIS): compose adds a strategic 'how it works' technical synthesis.

Locks the flag → service.tech_synthesis → run_react wiring and the addendum contract. The full
compose behavior (does the answer explain the tech end-to-end, disclosed-vs-likely labeled) is
prod-verified."""
from __future__ import annotations

import os

from roster_kernel.research.react import _TECH_SYNTHESIS_ADDENDUM


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_tech_synthesis() -> None:
    os.environ["ROSTER_TECH_SYNTHESIS"] = "1"
    a = _reload_app()
    assert a.tech_synthesis_enabled() is True
    assert a.build_default_service().tech_synthesis is True
    os.environ["ROSTER_TECH_SYNTHESIS"] = ""
    a = _reload_app()
    assert a.tech_synthesis_enabled() is False
    assert a.build_default_service().tech_synthesis is False


def test_addendum_demands_end_to_end_disclosed_vs_likely_and_strategy() -> None:
    ad = _TECH_SYNTHESIS_ADDENDUM
    assert "END-TO-END" in ad                        # the full user flow
    assert "core technical building blocks" in ad    # the parts the product is built on
    assert "[[R]]" in ad and "[[/R]]" in ad          # likely design is labeled grounded inference
    assert "Separate DISCLOSED from LIKELY" in ad     # never present inference as disclosed fact
    assert "moat" in ad                              # tie to strategy/defensibility
    assert "Skip this entirely if the subject has no technical product" in ad  # self-skips non-tech
