"""KEEP FRESH (maps on a cadence) + notifications + on-demand people refresh: structural tests (no DB)."""
import importlib.util
import pathlib
import sys


def test_map_cadence_vocabulary_and_refresh_revision_reason():
    from api.maps import CADENCES, REVISION_REASONS
    assert set(CADENCES) == {"off", "daily", "weekly"} and CADENCES["daily"] == 1 and CADENCES["weekly"] == 7
    assert "refresh" in REVISION_REASONS


def test_notification_store_and_cadence_columns_are_declared():
    from api import accounts, maps
    assert "roster_notification" in accounts._DDL and "read_at" in accounts._DDL
    assert "refresh_every" in maps._DDL and "next_refresh_at" in maps._DDL
    assert hasattr(accounts.AccountStore, "add_notification") and hasattr(accounts.AccountStore, "mark_notifications_read")
    assert hasattr(maps.MapStore, "set_cadence") and hasattr(maps.MapStore, "due_maps") and hasattr(maps.MapStore, "mark_checked")


def test_people_refresh_is_conditional_and_only_relearns_changed_profiles():
    p = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "refresh_people.py"
    spec = importlib.util.spec_from_file_location("refresh_people", p)
    m = importlib.util.module_from_spec(spec); sys.modules["refresh_people"] = m; spec.loader.exec_module(m)
    stored = {"company": "Acme", "location": "Austin", "bio": "builder", "name": "A B", "blog": ""}
    assert not m.profile_changed(stored, {**stored})
    assert m.profile_changed(stored, {**stored, "company": "Beta Corp"})      # employer moved → re-extract
    assert m.profile_changed(stored, {**stored, "location": "Denver"})
    src = p.read_text()
    assert "If-None-Match" in src and "304" in src                              # conditional GET; 304 is free
    assert "DELETE FROM roster_entity_facet" in src                             # stale employer/role facets go


def test_endpoints_exist_and_are_gated():
    from fastapi.testclient import TestClient
    from api.app import create_app
    c = TestClient(create_app())
    assert c.post("/maps/nope/cadence", json={"every": "daily"}).status_code in (401, 403, 404, 503)
    assert c.get("/me/notifications").status_code in (401, 403, 404, 503)   # 404 = accounts off locally
    r = c.post("/admin/people/refresh", json={"limit": 5}, headers={"X-Admin-Token": "wrong"})
    assert r.status_code in (401, 503)
