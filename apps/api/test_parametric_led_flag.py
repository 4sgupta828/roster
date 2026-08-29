"""Parametric-led (ROSTER_PARAMETRIC_LED): flag → service.parametric_led wiring (T1).

Locks the flag → app.parametric_led_enabled() → service.parametric_led field, and that the
vertical's prior-draft prompt flows through to the service as inert data (service.prior_draft_prompt).
No parametric BEHAVIOR yet — T1 declares the ask()/run_react params inert; the verify loop + compose
arrive in T2/T3.
"""
from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_parametric_led() -> None:
    os.environ["ROSTER_PARAMETRIC_LED"] = "1"
    a = _reload_app()
    assert a.parametric_led_enabled() is True
    assert a.build_default_service().parametric_led is True
    os.environ["ROSTER_PARAMETRIC_LED"] = ""
    a = _reload_app()
    assert a.parametric_led_enabled() is False
    assert a.build_default_service().parametric_led is False


def test_prior_draft_prompt_flows_from_manifest() -> None:
    # The manifest's prior-draft prompt is inert data — always set, independent of the flag — so the
    # service carries it regardless of ON/OFF (the flag + routing gate its USE, not its presence).
    os.environ["ROSTER_PARAMETRIC_LED"] = ""
    a = _reload_app()
    prompt = a.build_default_service().prior_draft_prompt
    assert isinstance(prompt, str) and prompt.strip()


def test_research_out_carries_unverified_priors() -> None:
    """T3: ResearchOut exposes the parametric `unverified_priors` register field (defaults to [] so the
    OFF/non-parametric response is unchanged) and round-trips a populated register."""
    a = _reload_app()
    default = a.ResearchOut(grounded=False, claims=[], coverage_gaps=[], rejected=0)
    assert default.unverified_priors == []                       # default [] → OFF response unchanged
    priors = [{"text": "Acme is profitable", "needs_freshness": False}]
    populated = a.ResearchOut(grounded=True, claims=[], coverage_gaps=[], rejected=0,
                              unverified_priors=priors)
    assert populated.unverified_priors == priors
    assert "unverified_priors" in populated.model_dump()
