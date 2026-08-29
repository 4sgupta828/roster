from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_deep_company_reader() -> None:
    os.environ["ROSTER_DEEP_COMPANY_READER"] = "1"
    a = _reload_app()
    svc = a.build_default_service()
    assert a.deep_company_reader_enabled() is True
    assert svc.deep_company is True
    assert isinstance(svc.company_reader, dict) and svc.company_reader.get("internal")

    os.environ["ROSTER_DEEP_COMPANY_READER"] = ""
    a = _reload_app()
    assert a.deep_company_reader_enabled() is False
    assert a.build_default_service().deep_company is False
