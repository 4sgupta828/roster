"""Contract tests for POST /research/focus (span-focus 'Go deeper' / 'Rethink').

Offline: app.state.service is replaced with a fake that RECORDS the kwargs the endpoint passes
to `ask(...)` and returns a canned AnswerResult, so we can assert the judge-panel-reviewed
wiring without any LLM/corpus:
  - flag OFF → 404 (true no-op); empty span → 400
  - the span rides in `question` (drives retrieval); mode rides in `answer_format_override`
  - history=None (bypass the follow-up resolver); answer_focus=True; graph_question = the
    PRISTINE original question (correct graph-expander anchor); response shape.

    ROSTER_FOCUS_DEEPEN=1 PYTHONPATH=apps:packages/vertical_roster:packages/kernel \
      .venv/bin/python -m pytest apps/api/test_focus.py -q
"""
from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi.testclient import TestClient


def _fake_service():
    calls = {}

    class _UI:
        def source_url(self, document_id, quote=None):
            return "https://example.test/" + document_id if document_id else None

    async def ask(**kw):
        calls.update(kw)
        claim = SimpleNamespace(text="deeper fact [1]", quote="verbatim quote",
                                atom_id="a1", source_key="edgar", document_title="Form D",
                                document_id="edgar:0001-23-000001")
        return SimpleNamespace(verified_claims=[claim], composed_answer="A tight deeper note. [1]",
                               grounded=True, rejected_claims=[])

    svc = SimpleNamespace(ask=ask, ui=_UI())
    return svc, calls


def _client(flag: str):
    os.environ["ROSTER_FOCUS_DEEPEN"] = flag
    # import inside so the env flag is read at request time (the enabled-fn reads os.environ live)
    from api.app import create_app
    app = create_app()
    svc, calls = _fake_service()
    app.state.service = svc          # prevent build_default_service(); capture ask kwargs
    return TestClient(app), calls


def test_flag_off_returns_404() -> None:
    client, _ = _client("")
    r = client.post("/research/focus", json={"question": "q", "span": "some span", "tenant_id": "demo"})
    assert r.status_code == 404


def test_empty_span_returns_400() -> None:
    client, _ = _client("1")
    r = client.post("/research/focus", json={"question": "q", "span": "   ", "tenant_id": "demo"})
    assert r.status_code == 400


def test_deeper_wiring_and_response_shape() -> None:
    client, calls = _client("1")
    r = client.post("/research/focus", json={
        "question": "How is Abalone Bio funded?", "span": "raised $9.1M in total",
        "mode": "deeper", "tenant_id": "demo"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "deeper" and body["span"] == "raised $9.1M in total"
    assert body["grounded"] is True and body["answer"]
    assert body["claims"][0]["url"] == "https://example.test/edgar:0001-23-000001"
    # the span rides in the question (drives retrieval); the original question is preserved for context
    assert "raised $9.1M in total" in calls["question"]
    assert "How is Abalone Bio funded?" in calls["question"]
    assert "GREATER DEPTH" in calls["question"]
    # bypass the follow-up resolver; scope compose; pristine graph anchor
    assert calls["history"] is None
    assert calls["answer_focus"] is True
    assert calls["graph_question"] == "How is Abalone Bio funded?"
    # mode rides in the compact override (skips the memo + closing block)
    assert "FOCUSED deepening" in calls["answer_format_override"]


def test_rethink_uses_the_critical_directive() -> None:
    client, calls = _client("1")
    r = client.post("/research/focus", json={
        "question": "Is Abalone's moat strong?", "span": "cell-specific antibody drugs",
        "mode": "rethink", "tenant_id": "demo"})
    assert r.status_code == 200 and r.json()["mode"] == "rethink"
    assert "CRITICALLY RE-EXAMINED" in calls["question"]
    assert "critical re-examination" in calls["answer_format_override"]
    # strict grounding guard is present in the directive (don't manufacture a counterpoint)
    assert "manufacture a counterpoint" in calls["answer_format_override"]


def test_unknown_mode_falls_back_to_deeper() -> None:
    client, calls = _client("1")
    r = client.post("/research/focus", json={
        "question": "q", "span": "a highlighted phrase", "mode": "bogus", "tenant_id": "demo"})
    assert r.status_code == 200 and r.json()["mode"] == "deeper"
    assert "FOCUSED deepening" in calls["answer_format_override"]
