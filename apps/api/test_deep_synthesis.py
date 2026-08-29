"""Deep-synthesis (ROSTER_DEEP_SYNTHESIS): flag → service.deep_synthesis wiring (T1).

Locks the flag → app.deep_synthesis_enabled() → service.deep_synthesis field, and that the
vertical's deep-synthesis compose format flows through to the service as inert data
(service.deep_answer_format). No deep BEHAVIOR yet — T1 declares the run_react params inert; the
compose behavior arrives in T2/T3.
"""
from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_deep_synthesis() -> None:
    os.environ["ROSTER_DEEP_SYNTHESIS"] = "1"
    a = _reload_app()
    assert a.deep_synthesis_enabled() is True
    assert a.build_default_service().deep_synthesis is True
    os.environ["ROSTER_DEEP_SYNTHESIS"] = ""
    a = _reload_app()
    assert a.deep_synthesis_enabled() is False
    assert a.build_default_service().deep_synthesis is False


def test_deep_answer_format_flows_from_manifest() -> None:
    # The manifest's deep format is inert data — always set, independent of the flag — so the service
    # carries it regardless of ON/OFF (the flag + question kind gate its USE, not its presence).
    os.environ["ROSTER_DEEP_SYNTHESIS"] = ""
    a = _reload_app()
    fmt = a.build_default_service().deep_answer_format
    assert isinstance(fmt, str) and fmt.strip()
