"""ROSTER_WEB_OPEN_DENOISE: flag → service.web_open_denoise → run_react wiring.

Locks the flag → service field. The full funnel behavior (open leg + gates + screen) is unit-
tested in the kernel (test_web_open_denoise_leg / test_web_denoise / test_web_quality) and
prod-verified via shadow-diff."""
from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_web_open_denoise() -> None:
    os.environ["ROSTER_WEB_OPEN_DENOISE"] = "1"
    a = _reload_app()
    assert a.open_web_denoise_enabled() is True
    assert a.build_default_service().web_open_denoise is True
    os.environ["ROSTER_WEB_OPEN_DENOISE"] = ""
    a = _reload_app()
    assert a.open_web_denoise_enabled() is False
    assert a.build_default_service().web_open_denoise is False
