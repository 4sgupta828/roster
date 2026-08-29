"""Authority-basis (ROSTER_AUTHORITY_BASIS): flag → service field wiring (T1/T2).

Locks the flag → app.authority_basis_enabled() → service.authority_basis field, and that the
vertical's compose floor directive flows through to the service as inert data
(service.authority_basis_directive) regardless of the flag (the flag gates its USE in run_react,
not its presence on the service).
"""
from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_authority_basis() -> None:
    os.environ["ROSTER_AUTHORITY_BASIS"] = "1"
    a = _reload_app()
    assert a.authority_basis_enabled() is True
    assert a.build_default_service().authority_basis is True
    os.environ["ROSTER_AUTHORITY_BASIS"] = ""
    a = _reload_app()
    assert a.authority_basis_enabled() is False
    assert a.build_default_service().authority_basis is False


def test_directive_flows_from_manifest_regardless_of_flag() -> None:
    # Inert data — always set, independent of the flag — so the service carries it ON or OFF.
    os.environ["ROSTER_AUTHORITY_BASIS"] = ""
    a = _reload_app()
    directive = a.build_default_service().authority_basis_directive
    assert isinstance(directive, str) and directive.strip()
