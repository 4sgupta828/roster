"""DSN-gated + flag-gated endpoint tests for the CROSSVIEWS surface (Task CV2).

Skipped unless ROSTER_CORPUS_DSN points at Postgres (mirrors the other integration
tests). Seeds a small GROUNDED graph under a UNIQUE tenant, then drives the HTTP surface
via TestClient:

  * flag OFF (ROSTER_CROSSVIEWS unset) → all four endpoints 404 (true no-op);
  * flag ON → /crossviews/options returns the grounded column catalog + categories;
    /crossviews/build returns a grounded, cited grid; /crossviews/save persists a
    kind='crossview' session that /sessions?kind=crossview lists and /sessions/{id}
    reopens with the saved spec on thread[0].

The /crossviews/agent LLM turn is covered offline (test_crossviews_agent.py); here we
prove the grounded DB + persistence WIRING, no live model.

    ROSTER_CORPUS_DSN=postgresql://strata:strata@localhost:5433/roster_cv_ep_test \
      /Users/sgupta/roster/.venv/bin/python -m pytest \
        apps/api/test_crossviews_endpoints.py -q
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.claimgraph_tech import make_tech_claim_store

DSN = os.environ.get("ROSTER_CORPUS_DSN")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DSN, reason="set ROSTER_CORPUS_DSN for crossviews endpoint tests"),
]


def _uid(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4().hex[:12]}"


async def _seed(tenant: str, cat_norm: str, cat_entity: str) -> dict:
    """Two grounded companies in one category; company A also has a founder edge + a
    product cell. Everything cited. Returns the ids."""
    store = make_tech_claim_store(DSN)
    try:
        await store.upsert_entity(cat_entity, "category", "Widget Platforms", tenant_id=tenant)
        a = _uid("domain")
        await store.upsert_entity(a, "company", "Acme", primary_domain="acme.com",
                                  tenant_id=tenant)
        ca = await store.upsert_claim(
            subject_id=a, predicate="operates_in_category", object_kind="entity",
            object_entity_id=cat_entity, object_norm=cat_norm, confidence=0.9, tenant_id=tenant)
        await store.add_evidence(ca, _uid("doc"), "b1", "Acme operates in widgets",
                                 authority_tier=3, tenant_id=tenant)
        pa = await store.upsert_claim(
            subject_id=a, predicate="offers_product", object_kind="value",
            object_value="AcmeWidget", object_norm="acmewidget", confidence=0.8, tenant_id=tenant)
        await store.add_evidence(pa, _uid("doc"), "b2", "Acme offers AcmeWidget",
                                 authority_tier=2, tenant_id=tenant)
        person = _uid("person")
        await store.upsert_entity(person, "person", "Jane Roe", tenant_id=tenant)
        fa = await store.upsert_claim(
            subject_id=a, predicate="has_founder", object_kind="entity",
            object_entity_id=person, object_norm="jane roe", confidence=0.9, tenant_id=tenant)
        await store.add_evidence(fa, _uid("doc"), "b3", "Jane Roe co-founded Acme",
                                 authority_tier=3, tenant_id=tenant)
        b = _uid("domain")
        await store.upsert_entity(b, "company", "Beta", primary_domain="beta.com",
                                  tenant_id=tenant)
        cb = await store.upsert_claim(
            subject_id=b, predicate="operates_in_category", object_kind="entity",
            object_entity_id=cat_entity, object_norm=cat_norm, confidence=0.9, tenant_id=tenant)
        await store.add_evidence(cb, _uid("doc"), "b4", "Beta operates in widgets",
                                 authority_tier=2, tenant_id=tenant)
        return {"a": a, "b": b, "person": person}
    finally:
        await store.close()


def test_flag_off_all_endpoints_404(monkeypatch) -> None:
    monkeypatch.delenv("ROSTER_CROSSVIEWS", raising=False)
    client = TestClient(create_app(None))
    assert client.get("/crossviews/options").status_code == 404
    assert client.post("/crossviews/agent",
                       json={"row_kind": "company", "goal": "x"}).status_code == 404
    assert client.post("/crossviews/build",
                       json={"spec": {"row_kind": "company"}}).status_code == 404
    assert client.post("/crossviews/save",
                       json={"spec": {"row_kind": "company"}}).status_code == 404


def test_options_build_save_reopen(monkeypatch) -> None:
    monkeypatch.setenv("ROSTER_CROSSVIEWS", "1")
    monkeypatch.setenv("ROSTER_CORPUS_DSN", DSN)
    tenant = "cv-ep-" + uuid.uuid4().hex[:10]
    cat_norm = "cat-" + uuid.uuid4().hex[:10]
    cat_entity = "category:" + cat_norm
    ids = asyncio.new_event_loop().run_until_complete(_seed(tenant, cat_norm, cat_entity))

    # Context-manager so ALL requests share ONE portal/event-loop — otherwise the cached
    # session-store asyncpg pool (created on the save request's loop) is reused on the
    # list request's loop and asyncpg raises a cross-loop error.
    with TestClient(create_app(None)) as client:
        _drive(client, tenant, cat_norm, ids)


def _drive(client: TestClient, tenant: str, cat_norm: str, ids: dict) -> None:
    # ---- /crossviews/options: grounded column catalog + categories ----
    r = client.get("/crossviews/options",
                   params={"row_kind": "company", "category_norm": cat_norm,
                           "tenant_id": tenant})
    assert r.status_code == 200
    opts = r.json()
    assert set(opts["row_kinds"]) == {"company", "person", "category"}
    cov_by_id = {c["id"]: c["coverage"] for c in opts["columns"]}
    assert cov_by_id.get("operates_in_category") == 2      # A + B
    assert cov_by_id.get("offers_product") == 1            # A only
    assert cov_by_id.get("has_founder") == 1               # A only
    # every catalog column is grounded (coverage>0) and carries its dimension
    for c in opts["columns"]:
        assert c["coverage"] > 0 and c["dimension"]
    assert cat_norm in {c["object_norm"] for c in opts["categories"]}

    # ---- /crossviews/build: grounded, cited grid ----
    spec = {"row_kind": "company", "filters": {"category_norm": cat_norm},
            "columns": [{"id": "offers_product", "kind": "predicate"},
                        {"id": "has_founder", "kind": "predicate"},
                        {"id": "raised_funding", "kind": "predicate"}]}
    r = client.post("/crossviews/build", json={"spec": spec, "tenant_id": tenant})
    assert r.status_code == 200
    grid = r.json()
    assert grid["meta"]["row_count"] == 2
    acme = next(row for row in grid["rows"] if row["id"] == ids["a"])
    assert acme["cells"]["offers_product"]["value"] == "AcmeWidget"
    assert acme["cells"]["offers_product"]["citations"][0]["document_id"]  # grounded
    assert acme["cells"]["raised_funding"] == {"collected": False}         # honest gap

    # ---- /crossviews/save: persists a kind='crossview' session ----
    r = client.post("/crossviews/save", json={
        "spec": spec, "grid": grid, "transcript": [{"role": "user", "content": "widgets"}],
        "title": "Widget landscape", "tenant_id": tenant})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    assert sid

    # ---- /sessions?kind=crossview lists it ----
    r = client.get("/sessions", params={"tenant_id": tenant, "kind": "crossview"})
    assert r.status_code == 200
    listed = r.json()["sessions"]
    assert any(s["id"] == sid and s["kind"] == "crossview" for s in listed)
    # the plain-research filter must NOT surface the crossview
    r2 = client.get("/sessions", params={"tenant_id": tenant, "kind": "research"})
    assert all(s["id"] != sid for s in r2.json()["sessions"])

    # ---- reopen: /sessions/{id} carries the saved spec on thread[0] ----
    r = client.get(f"/sessions/{sid}")
    assert r.status_code == 200
    row = r.json()
    turn0 = row["thread"][0]
    assert turn0["kind"] == "crossview"
    assert turn0["crossview_spec"]["row_kind"] == "company"
    assert [c["id"] for c in turn0["crossview_spec"]["columns"]] == \
        ["offers_product", "has_founder", "raised_funding"]
