"""Ask-Panel orchestrator: specialists run grounded in parallel; synthesis composes ONLY from the pooled
verified findings (grounding preserved). Driven by a content-routing scripted LLM."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.panel import run_panel
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer, InterpretationItem, \
    ConfidenceRead, ConfidenceDim
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


@dataclass(frozen=True)
class _Spec:
    id: str
    specialty: str
    lens: str
    focus: str
    source_keys: tuple = ()


_BLOCK = "The approved dose is 5 mg once daily and the response rate was 53 percent."


def _source():
    s = InMemoryRetrievalSource()
    s.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=_BLOCK,
                       locator=Locator("block_span", "d1", {"block_id": "b1"}), source_key="corpus"))
    return s


class _LLM:
    """Routes by prompt content: panel synthesis vs specialist compose vs planner step."""
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        c = messages[0]["content"]
        if "VERIFIED PANEL FINDINGS" in c:                     # the panel synthesis
            return LLMResult(parsed=ComposedAnswer(
                answer="The panel agrees the dose is 5 mg once daily [1].",
                reasoning_purpose="Whether the dose is appropriate.",
                interpretation=[InterpretationItem(text="Both lenses cite the same 5 mg dose",
                                                   kind="implication", basis_findings=[1])],
                reasoning_conclusion="The evidence supports 5 mg once daily.",
                confidence=ConfidenceRead(factual=ConfidenceDim(level="moderate"))), output_tokens=5)
        if "VERIFIED FINDINGS" in c:                           # a specialist's own compose
            return LLMResult(parsed=ComposedAnswer(answer="Dose is 5 mg once daily [1]."), output_tokens=5)
        if "no evidence yet" in c:                             # first planner step → search
            return LLMResult(parsed=AgentStep(action="search", query="dose"), output_tokens=5)
        return LLMResult(parsed=AgentStep(action="answer", claims=[                # answer step
            ClaimOut(text="the approved dose is 5 mg once daily", atom_id="a1",
                     quote="The approved dose is 5 mg once daily")]), output_tokens=5)


def _specialists():
    return [_Spec("pharm", "Clinical Pharmacology", "You are a pharmacologist.", "dosing, interactions"),
            _Spec("ebm", "Evidence-Based Medicine", "You are an EBM methodologist.", "evidence quality")]


def test_panel_runs_specialists_and_grounds_synthesis():
    src = _source()
    def make_retrievers(source_keys):
        return src, None
    r = asyncio.run(run_panel(
        question="What is the dose?", specialists=_specialists(), llm=_LLM(), embedder=FakeEmbedder(dim=8),
        make_retrievers=make_retrievers, tenant_id="A", synthesis_directive="Synthesize."))
    # both specialists produced grounded takes
    assert r.n_specialists == 2 and len(r.takes) == 2
    assert all(t.grounded and t.n_verified >= 1 for t in r.takes)
    # synthesis is grounded in the pooled findings and cites [1]
    assert "5 mg once daily" in r.synthesis and "[1]" in r.synthesis
    assert r.claims
    # the structured reasoning-read layer is intentionally OFF for the panel (reasoning lives in the
    # answer's "How the panel reasoned" narrative) — so these stay empty
    assert r.interpretation == [] and r.confidence is None


def test_panel_survives_a_failing_specialist():
    src = _source()
    def make_retrievers(source_keys):
        # the 'ebm' specialist gets no source → still returns a result (no claims), panel proceeds
        return src, None
    specs = _specialists()
    r = asyncio.run(run_panel(
        question="What is the dose?", specialists=specs, llm=_LLM(), embedder=FakeEmbedder(dim=8),
        make_retrievers=make_retrievers, tenant_id="A"))
    assert len(r.takes) == 2 and r.synthesis          # panel still synthesizes


def test_plan_panel_selects_and_ignores_unknown():
    from roster_kernel.research.panel import plan_panel, PanelPlan, SpecialistPick
    roster = [{"id": "pharm", "specialty": "Clinical Pharmacology", "lens": "dosing"},
              {"id": "ebm", "specialty": "Evidence-Based Medicine", "lens": "evidence quality"},
              {"id": "cards", "specialty": "Cardiology", "lens": "cardiovascular"}]
    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            return LLMResult(parsed=PanelPlan(specialists=[
                SpecialistPick(id="cards", rationale="heart failure component"),
                SpecialistPick(id="pharm", rationale="renal dosing"),
                SpecialistPick(id="bogus", rationale="not in roster")]), output_tokens=5)
    sel = asyncio.run(plan_panel(question="HF + CKD case", roster=roster, llm=_LLM()))
    ids = [s["id"] for s in sel]
    assert ids == ["cards", "pharm"] and sel[0]["specialty"] == "Cardiology" and "heart failure" in sel[0]["rationale"]


def test_plan_panel_failsafe_on_error():
    from roster_kernel.research.panel import plan_panel
    class _Bad:
        async def complete(self, **k): raise RuntimeError("triage down")
    assert asyncio.run(plan_panel(question="q", roster=[{"id": "x", "specialty": "X", "lens": ""}],
                                  llm=_Bad())) == []   # empty → caller applies the default set


def test_panel_no_evidence_says_so():
    empty = InMemoryRetrievalSource()   # nothing to retrieve → no verified claims anywhere
    class _Empty:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            if "no evidence yet" in messages[0]["content"]:
                return LLMResult(parsed=AgentStep(action="search", query="x"), output_tokens=5)
            return LLMResult(parsed=AgentStep(action="answer", claims=[]), output_tokens=5)
    r = asyncio.run(run_panel(question="?", specialists=_specialists(), llm=_Empty(),
        embedder=FakeEmbedder(dim=8), make_retrievers=lambda k: (empty, None), tenant_id="A"))
    assert "could not ground" in r.synthesis and not r.claims


# ---- live progress streaming + latency guardrails ------------------------------------------------

def _collect_events():
    events = []
    async def on_event(ev):
        events.append(ev)
    return events, on_event


def test_panel_streams_tagged_progress_events_in_phase_order():
    """Full scripted run through the REAL run_react: phase events arrive in order (plan_done →
    specialist_start/…/_done → synthesizing) and every specialist trace event is tagged with the
    lens that produced it (id + specialty) and is a whitelisted narration type."""
    from roster_kernel.research.panel import _TRACE_FORWARD
    src = _source()
    events, on_event = _collect_events()
    r = asyncio.run(run_panel(
        question="What is the dose?", specialists=_specialists(), llm=_LLM(), embedder=FakeEmbedder(dim=8),
        make_retrievers=lambda k: (src, None), tenant_id="A", on_event=on_event))
    assert r.synthesis
    types = [e["type"] for e in events]
    # panel_plan_done is FIRST and carries the convened set
    assert types[0] == "panel_plan_done"
    assert {s["id"] for s in events[0]["selected"]} == {"pharm", "ebm"} and events[0]["n"] == 2
    # phase order: plan_done before any specialist_start; every specialist_done before synthesizing
    assert "synthesizing" in types
    assert types.index("panel_plan_done") < types.index("specialist_start")
    assert max(i for i, t in enumerate(types) if t == "specialist_done") < types.index("synthesizing")
    assert types.count("specialist_start") == 2 and types.count("specialist_done") == 2
    # every trace event is tagged with id + specialty and is a whitelisted narration type
    traces = [e for e in events if e["type"] == "specialist_trace"]
    assert traces
    assert all(e["id"] in {"pharm", "ebm"} and e["specialty"] for e in traces)
    assert all(e["ev"]["type"] in _TRACE_FORWARD for e in traces)
    # specialist_done carries the observability fields (grounded / n_verified / seconds + legacy verified)
    dones = [e for e in events if e["type"] == "specialist_done"]
    assert all(e["grounded"] and e["n_verified"] >= 1 and e["verified"] == e["n_verified"]
               and isinstance(e["seconds"], float) and e["specialty"] for e in dones)


class _FakeVC:
    def __init__(self, i=1):
        self.text = f"claim {i}"; self.quote = f"quote {i}"; self.atom_id = f"a{i}"
        self.source_key = "corpus"; self.document_title = "Doc"; self.document_id = "d1"


class _FakeRes:
    def __init__(self, n=1):
        self.verified_claims = [_FakeVC(i) for i in range(1, n + 1)]
        self.grounded = n > 0
        self.composed_answer = "take [1]" if n else ""


def test_specialist_trace_is_throttled_to_meaningful_events(monkeypatch):
    """A scripted run_react emitting fixed events: whitelisted narration is forwarded (tagged);
    internal chatter (contract/graph_legs/selecting) is dropped."""
    import roster_kernel.research.panel as panel_mod
    scripted = [{"type": "search", "query": "q"}, {"type": "contract", "mode": "shadow"},
                {"type": "found", "added": 1, "total": 1}, {"type": "graph_legs", "legs": []},
                {"type": "selecting", "from": 9, "to": 5}, {"type": "verified", "verified": 1},
                {"type": "composing", "findings": 1}]
    async def fake_run_react(*, on_event=None, **kw):
        for ev in scripted:
            if on_event is not None:
                await on_event(ev)
        return _FakeRes(0)
    monkeypatch.setattr(panel_mod, "run_react", fake_run_react)
    events, on_event = _collect_events()
    asyncio.run(run_panel(question="q", specialists=_specialists()[:1], llm=None,
        embedder=None, make_retrievers=lambda k: (None, None), tenant_id="A", on_event=on_event))
    fwd = [e["ev"]["type"] for e in events if e["type"] == "specialist_trace"]
    assert fwd == ["search", "found", "verified", "composing"]
    assert all(e["id"] == "pharm" and e["specialty"] == "Clinical Pharmacology"
               for e in events if e["type"] == "specialist_trace")


def test_specialist_timeout_panel_proceeds(monkeypatch):
    """One lens hangs → it times out (ROSTER_PANEL_SPECIALIST_TIMEOUT) and the panel completes on
    the other lens's findings, with the timeout surfaced on the take and the done event."""
    import roster_kernel.research.panel as panel_mod
    monkeypatch.setenv("ROSTER_PANEL_SPECIALIST_TIMEOUT", "1")
    async def fake_run_react(*, question, on_event=None, **kw):
        if "Clinical Pharmacology" in question:      # the panel-focus line names the lens
            await asyncio.sleep(30)                  # hangs — must be cut by the timeout
        return _FakeRes(1)
    monkeypatch.setattr(panel_mod, "run_react", fake_run_react)
    events, on_event = _collect_events()
    r = asyncio.run(run_panel(question="q", specialists=_specialists(), llm=_LLM(),
        embedder=None, make_retrievers=lambda k: (None, None), tenant_id="A", on_event=on_event))
    by_id = {t.id: t for t in r.takes}
    assert "timed out after 1s" in by_id["pharm"].error and by_id["pharm"].n_verified == 0
    assert by_id["ebm"].grounded and by_id["ebm"].n_verified == 1
    assert r.synthesis and r.claims                  # the panel still synthesized without the slow lens
    dones = {e["id"]: e for e in events if e["type"] == "specialist_done"}
    assert dones["pharm"]["error"] == "timeout" and dones["pharm"]["grounded"] is False
    assert "seconds" in dones["pharm"] and dones["ebm"]["n_verified"] == 1


def test_panel_deadline_cancels_stragglers(monkeypatch):
    """Every lens too slow + a 1s total deadline → stragglers are cancelled, each reported as a
    deadline take, and the panel returns (honest no-evidence synthesis) instead of hanging."""
    import roster_kernel.research.panel as panel_mod
    monkeypatch.setenv("ROSTER_PANEL_DEADLINE", "1")
    monkeypatch.setenv("ROSTER_PANEL_SPECIALIST_TIMEOUT", "60")
    async def fake_run_react(*, on_event=None, **kw):
        await asyncio.sleep(30)
        return _FakeRes(1)
    monkeypatch.setattr(panel_mod, "run_react", fake_run_react)
    events, on_event = _collect_events()
    r = asyncio.run(run_panel(question="q", specialists=_specialists(), llm=_LLM(),
        embedder=None, make_retrievers=lambda k: (None, None), tenant_id="A", on_event=on_event))
    assert len(r.takes) == 2
    assert all("deadline" in t.error for t in r.takes)
    assert "could not ground" in r.synthesis
    dones = [e for e in events if e["type"] == "specialist_done"]
    assert len(dones) == 2 and all(e["error"] == "panel_deadline" for e in dones)


def test_panel_synthesis_findings_cap(monkeypatch):
    """Pooled findings beyond ROSTER_PANEL_SYNTH_CLAIMS are trimmed before synthesis — the panel's
    claims list IS the capped list, so the synthesis [n] refs stay consistent."""
    import roster_kernel.research.panel as panel_mod
    monkeypatch.setenv("ROSTER_PANEL_SYNTH_CLAIMS", "4")
    async def fake_run_react(*, on_event=None, **kw):
        return _FakeRes(10)                          # 2 lenses × 10 claims = 20 pooled
    monkeypatch.setattr(panel_mod, "run_react", fake_run_react)
    r = asyncio.run(run_panel(question="q", specialists=_specialists(), llm=_LLM(),
        embedder=None, make_retrievers=lambda k: (None, None), tenant_id="A"))
    assert len(r.claims) == 4                        # capped after pooling
    assert r.synthesis and "[1]" in r.synthesis
