from __future__ import annotations

import os


def _reload_app():
    import importlib
    import api.app as a
    importlib.reload(a)
    return a


def test_flag_drives_service_deep_person_reader() -> None:
    os.environ["ROSTER_DEEP_PEOPLE_READER"] = "1"
    a = _reload_app()
    svc = a.build_default_service()
    assert a.deep_people_reader_enabled() is True
    assert svc.deep_person is True
    assert isinstance(svc.person_reader, dict) and svc.person_reader.get("external")
    assert svc.question_contract == "shadow"

    os.environ["ROSTER_DEEP_PEOPLE_READER"] = ""
    a = _reload_app()
    assert a.deep_people_reader_enabled() is False
    assert a.build_default_service().deep_person is False
