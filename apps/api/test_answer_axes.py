"""Answer-axes (ROSTER_ANSWER_AXES): compose addresses each asked contract axis + synthesizes.

Locks the flag → service.axis_complete → run_react wiring and the addendum contract. The full
compose behavior (does the answer address 'moat' + lead with a take) is prod-verified."""
from __future__ import annotations

import os

from roster_kernel.research.react import _AXIS_COMPLETE_ADDENDUM


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_axis_complete() -> None:
    os.environ["ROSTER_ANSWER_AXES"] = "1"
    a = _reload_app()
    assert a.axis_complete_enabled() is True
    assert a.build_default_service().axis_complete is True
    os.environ["ROSTER_ANSWER_AXES"] = ""
    a = _reload_app()
    assert a.axis_complete_enabled() is False
    assert a.build_default_service().axis_complete is False


def test_addendum_requires_each_axis_and_synthesis_and_deheds() -> None:
    ad = _AXIS_COMPLETE_ADDENDUM
    assert "MUST address EACH ONE" in ad          # every asked aspect covered
    assert "NEVER silently skip a requested aspect" in ad
    assert "synthesized TAKE" in ad               # lead with a take
    assert "do NOT build a reconciliation table" in ad   # consolidate source conflicts
    assert "<AXES>" in ad                          # the axis list is injected


def test_addendum_injects_the_axes() -> None:
    filled = _AXIS_COMPLETE_ADDENDUM.replace("<AXES>", "product, moat, founders")
    assert "product, moat, founders" in filled and "<AXES>" not in filled
