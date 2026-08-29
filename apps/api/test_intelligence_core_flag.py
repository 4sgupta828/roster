"""Intelligence-core (ROSTER_INTELLIGENCE_CORE): flag → service.intelligence_core wiring (T1).

Locks the flag → app.intelligence_core_enabled() → service.intelligence_core field, and that the
vertical's intelligence-draft prompt flows through to the service as inert data
(service.intelligence_draft_prompt). No intelligence BEHAVIOR yet — T1 declares the ask()/run_react
params inert; the adversarial FOR/AGAINST retrieval + hypotheses compose arrive in T2/T3.
"""
from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_intelligence_core() -> None:
    os.environ["ROSTER_INTELLIGENCE_CORE"] = "1"
    a = _reload_app()
    assert a.intelligence_core_enabled() is True
    assert a.build_default_service().intelligence_core is True
    os.environ["ROSTER_INTELLIGENCE_CORE"] = ""
    a = _reload_app()
    assert a.intelligence_core_enabled() is False
    assert a.build_default_service().intelligence_core is False


def test_intelligence_draft_prompt_flows_from_manifest() -> None:
    # The manifest's intelligence-draft prompt is inert data — always set, independent of the flag —
    # so the service carries it regardless of ON/OFF (the flag + routing gate its USE, not its presence).
    os.environ["ROSTER_INTELLIGENCE_CORE"] = ""
    a = _reload_app()
    prompt = a.build_default_service().intelligence_draft_prompt
    assert isinstance(prompt, str) and prompt.strip()
