"""Query expansion (ROSTER_QUERY_EXPANSION): enrich a terse question's retrieval brief.

Unit: expand_query + brief_text. Integration: _do_research augments the query with the brief and
keeps the PRISTINE question as graph_question; OFF = byte-identical (raw question, no graph_question).

    PYTHONPATH=apps:packages/vertical_roster:packages/kernel .venv/bin/python -m pytest \
        apps/api/test_query_expansion.py -q
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from api.query_expansion import brief_text, expand_query


class _LLM:
    def __init__(self, aspects, keywords):
        self._a, self._k = aspects, keywords

    async def complete(self, **kw):
        return SimpleNamespace(parsed=SimpleNamespace(aspects=list(self._a), keywords=list(self._k)))


def _run(c):
    return asyncio.run(c)


def test_expand_query_returns_aspects_and_keywords() -> None:
    llm = _LLM(["founders & team", "funding & investors", "traction"], ["Blazel", "personal branding AI"])
    out = _run(expand_query(llm, "Tell me about Blazel"))
    assert out["aspects"][0] == "founders & team"
    assert "Blazel" in out["keywords"]


def test_expand_query_none_on_no_llm_or_empty() -> None:
    assert _run(expand_query(None, "q")) is None
    assert _run(expand_query(_LLM([], []), "")) is None
    assert _run(expand_query(_LLM([], []), "q")) is None   # empty aspects+keywords → None


def test_expand_query_never_raises() -> None:
    class _Boom:
        async def complete(self, **kw):
            raise RuntimeError("down")
    assert _run(expand_query(_Boom(), "q")) is None


def test_brief_text_renders_aspects_and_keywords() -> None:
    b = brief_text({"aspects": ["moat", "competition"], "keywords": ["Stripe", "payments"]})
    assert "Coverage brief" in b and "moat; competition" in b and "Stripe, payments" in b
    assert "research questions, not facts" in b   # framed as targets, not facts


# ---- integration: _do_research augments the query + keeps graph_question pristine ---------------- #
def _client(flag: str, calls: dict):
    os.environ["ROSTER_QUERY_EXPANSION"] = flag
    from fastapi.testclient import TestClient
    from api.app import create_app
    app = create_app()

    async def ask(**kw):
        calls.clear(); calls.update(kw)
        return SimpleNamespace(composed_answer="A.", grounded=True, verified_claims=[], rejected_claims=[],
                               source_stats={}, coverage_gaps=[], visual_observation="", stopped_reason="answered",
                               atoms_gathered=1, retried_empty=False, resolved_question="", derived_from_prior=False,
                               effort=1.0)
    app.state.service = SimpleNamespace(
        ask=ask, ui=SimpleNamespace(source_url=lambda *a, **k: None),
        llm=_LLM(["founders", "funding", "traction"], ["Blazel", "LinkedIn content AI"]))
    return TestClient(app)


def test_flag_on_augments_query_and_keeps_pristine_graph_question() -> None:
    calls: dict = {}
    r = _client("1", calls).post("/research", json={"question": "Tell me about Blazel", "tenant_id": "demo"})
    assert r.status_code == 200
    assert "Coverage brief" in calls["question"]          # the query was enriched
    assert "founders" in calls["question"] and "Blazel" in calls["question"]
    assert calls["graph_question"] == "Tell me about Blazel"   # pristine subject preserved for the graph


def test_flag_off_is_byte_identical() -> None:
    calls: dict = {}
    r = _client("", calls).post("/research", json={"question": "Tell me about Blazel", "tenant_id": "demo"})
    assert r.status_code == 200
    assert calls["question"] == "Tell me about Blazel"     # untouched
    assert calls["graph_question"] is None                 # not set
