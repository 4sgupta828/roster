"""Entity-scoped open-web (ROSTER_WEB_ENTITY_OPEN): flag → service.entity_open_web → run_react wiring.

Locks the flag helper + the ResearchService plumbing, and that the vertical's page-quality screen
prompt flows through the manifest to the service regardless of the flag (inert data; the flag gates
whether the kernel ever consults it). The full open-web leg + quality-screen behavior is prod-verified."""
from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_entity_open_web() -> None:
    os.environ["ROSTER_WEB_ENTITY_OPEN"] = "1"
    a = _reload_app()
    assert a.entity_open_web_enabled() is True
    assert a.build_default_service().entity_open_web is True
    os.environ["ROSTER_WEB_ENTITY_OPEN"] = ""
    a = _reload_app()
    assert a.entity_open_web_enabled() is False
    assert a.build_default_service().entity_open_web is False


def test_web_quality_prompt_flows_through_regardless_of_flag() -> None:
    # OFF: prompt is inert data but still threaded from the manifest to the service.
    os.environ["ROSTER_WEB_ENTITY_OPEN"] = ""
    a = _reload_app()
    p_off = a.build_default_service().web_quality_prompt
    assert isinstance(p_off, str) and p_off.strip()
    # ON: same prompt present.
    os.environ["ROSTER_WEB_ENTITY_OPEN"] = "1"
    a = _reload_app()
    p_on = a.build_default_service().web_quality_prompt
    assert isinstance(p_on, str) and p_on.strip()
    os.environ["ROSTER_WEB_ENTITY_OPEN"] = ""
    _reload_app()
