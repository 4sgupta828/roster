"""Intra-retrieval progress events: {"type":"retrieving","source":...,"hits":N} narrates each
search leg (corpus / aux web) the moment it lands, between 'search' and 'found' — purely ADDITIVE
to the existing event stream (offline, scripted sources)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


class ScriptedLLM:
    """Returns pre-scripted AgentStep objects in order (ignores the prompt)."""
    def __init__(self, steps):
        self._steps = list(steps)

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._steps.pop(0), output_tokens=5, model="scripted")


class DelayedSource:
    """Wraps a retrieval source with an optional artificial delay — an offline stand-in for a
    slow leg (e.g. a multi-minute web search) so completion ORDER is deterministic in tests."""
    def __init__(self, inner, delay: float = 0.0):
        self._inner = inner
        self._delay = delay

    def make_block_loader(self, tenant_id, workspace_id=None):
        return self._inner.make_block_loader(tenant_id, workspace_id)

    async def search(self, req):
        if self._delay:
            await asyncio.sleep(self._delay)
        return await self._inner.search(req)


_CORPUS_TEXT = "The approved metric value was 9.8 percent for the term period."
_WEB_TEXT = "A web page also reports the metric near ten percent overall."


def _corpus() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_CORPUS_TEXT, source_key="corpus",
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
    ))
    return src


def _web() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="w1", document_id="dw", tenant_id="A", text=_WEB_TEXT, source_key="web",
        locator=Locator("block_span", "dw", {"block_id": "w1"}),
    ))
    return src


def _happy_llm() -> ScriptedLLM:
    return ScriptedLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent"),
        ]),
    ])


def _collect():
    events = []
    async def on_event(ev):
        events.append(ev)
    return events, on_event


def _run(llm, *, aux_source=None, on_event=None):
    return asyncio.run(run_react(
        question="what was the metric value?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=_corpus(), aux_source=aux_source, tenant_id="A",
        budget=BudgetState(max_calls=10), on_event=on_event))


def test_retrieving_events_emitted_per_source_in_completion_order() -> None:
    """Corpus + slow web leg: one 'retrieving' event per source, corpus first (it completes
    first), web when IT lands — both strictly between 'search' and 'found', with real hit counts."""
    events, on_event = _collect()
    res = _run(_happy_llm(), aux_source=DelayedSource(_web(), delay=0.02), on_event=on_event)
    assert res.grounded

    types = [e["type"] for e in events]
    retr = [e for e in events if e["type"] == "retrieving"]
    assert [(e["source"], e["hits"]) for e in retr] == [("corpus", 1), ("web", 1)]
    # both land after this step's 'search' and before its 'found'
    i_search, i_found = types.index("search"), types.index("found")
    idx = [i for i, t in enumerate(types) if t == "retrieving"]
    assert all(i_search < i < i_found for i in idx)
    # both legs' evidence was merged (corpus + web atoms)
    assert res.atoms_gathered == 2


def test_qa_no_aux_stream_is_additive_only() -> None:
    """No aux (today's Q&A default): the stream is byte-identical to the legacy sequence PLUS
    exactly one additive corpus 'retrieving' event per search — nothing else changed."""
    events, on_event = _collect()
    res = _run(_happy_llm(), on_event=on_event)
    assert res.grounded

    types = [e["type"] for e in events]
    legacy = [t for t in types if t != "retrieving"]
    # the legacy narration, unchanged and in order (pinned pre-change sequence for this script)
    assert legacy == ["step", "search", "found", "step", "verifying", "verified", "composing"]
    # additive events: exactly one per search, corpus-labelled, correct count, between search/found
    retr = [e for e in events if e["type"] == "retrieving"]
    assert [(e["source"], e["hits"]) for e in retr] == [("corpus", 1)]
    assert types.index("search") < types.index("retrieving") < types.index("found")


def test_aux_failure_emits_no_retrieving_for_that_leg() -> None:
    """A dead web leg stays on the existing degrade path: corpus narrates + answer proceeds;
    the failed leg emits NO 'retrieving' event (nothing false is narrated)."""
    class _BoomAux:
        def make_block_loader(self, tenant_id, workspace_id=None):
            return lambda document_id, block_id: None
        async def search(self, req):
            raise RuntimeError("web down")

    events, on_event = _collect()
    res = _run(_happy_llm(), aux_source=_BoomAux(), on_event=on_event)
    assert res.grounded                       # corpus leg alone still answers
    retr = [(e["source"], e["hits"]) for e in events if e["type"] == "retrieving"]
    assert retr == [("corpus", 1)]


# ---- panel: 'retrieving' is whitelisted specialist narration -------------------------------------

@dataclass(frozen=True)
class _Spec:
    id: str
    specialty: str
    lens: str
    focus: str
    source_keys: tuple = ()


def test_panel_forwards_retrieving_trace_events(monkeypatch) -> None:
    """'retrieving' is in the specialist_trace forward whitelist: a lens's per-source retrieval
    narration reaches the panel stream (tagged), while internal chatter is still dropped."""
    import roster_kernel.research.panel as panel_mod
    from roster_kernel.research.panel import _TRACE_FORWARD, run_panel
    assert "retrieving" in _TRACE_FORWARD

    scripted = [{"type": "search", "query": "q"},
                {"type": "retrieving", "source": "corpus", "hits": 12},
                {"type": "retrieving", "source": "web", "hits": 3},
                {"type": "selecting", "from": 9, "to": 5},          # chatter: still dropped
                {"type": "found", "added": 4, "total": 4}]

    class _NoRes:
        verified_claims = []
        grounded = False
        composed_answer = ""

    async def fake_run_react(*, on_event=None, **kw):
        for ev in scripted:
            if on_event is not None:
                await on_event(ev)
        return _NoRes()

    monkeypatch.setattr(panel_mod, "run_react", fake_run_react)
    events, on_event = _collect()
    asyncio.run(run_panel(
        question="q", specialists=[_Spec("pharm", "Clinical Pharmacology", "lens", "focus")],
        llm=None, embedder=None, make_retrievers=lambda k: (None, None), tenant_id="A",
        on_event=on_event))
    fwd = [e["ev"] for e in events if e["type"] == "specialist_trace"]
    assert [e["type"] for e in fwd] == ["search", "retrieving", "retrieving", "found"]
    assert [(e.get("source"), e.get("hits")) for e in fwd if e["type"] == "retrieving"] \
        == [("corpus", 12), ("web", 3)]
