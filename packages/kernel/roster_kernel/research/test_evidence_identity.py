"""Evidence Contract stage 1 — the evidence-identity flag (ROSTER_EVIDENCE_IDENTITY).

ON: every LLM-visible evidence surface (planner observations, claims-first extractor batch,
entailment items, compose findings, panel synthesis, fallback grounder) renders each atom's
document identity ⟨TITLE — source⟩, and the claim-writing instructions require subject-faithful
attribution. OFF: every prompt string is byte-identical to the pre-flag rendering (exact-string
asserts below). Offline throughout — scripted LLMs, no network (mirrors test_react.py)."""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.atoms import IDENTITY_INSTRUCTION, Atom, identity_tag
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource

_TITLE = "EXAMPLE COMPOUND TABLET"
_BLOCK_TEXT = "The approved metric value was 9.8 percent for the term period."
_TAG = f"⟨{_TITLE} — dailymed⟩"


def _atom(title=_TITLE, source="dailymed", text=_BLOCK_TEXT) -> Atom:
    return Atom(atom_id="a1", document_id="d1", block_id="b1", text=text,
                source_key=source, document_title=title)


def _source() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A", text=_BLOCK_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
        document_title=_TITLE, source_key="dailymed"))
    return src


class RecordingLLM:
    """Returns pre-scripted parsed objects in order AND records every prompt it saw."""
    def __init__(self, steps):
        self._steps = list(steps)
        self.prompts: list[str] = []
        self.systems: list[str] = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        self.systems.append(system)
        self.prompts.append(messages[0]["content"])
        return LLMResult(parsed=self._steps.pop(0), output_tokens=5, model="scripted")


def _grounded_script():
    """search → answer (one verified claim) → compose."""
    return [
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent")]),
        ComposedAnswer(answer="The approved metric value was 9.8 percent [1]."),
    ]


def _run(llm, *, evidence_identity: bool):
    return asyncio.run(run_react(
        question="what was the metric value?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=_source(), tenant_id="A", budget=BudgetState(max_calls=20),
        evidence_identity=evidence_identity))


# ---- the tag itself -------------------------------------------------------------------------

def test_identity_tag_renders_title_and_source():
    assert identity_tag(_atom()) == "⟨EXAMPLE COMPOUND TABLET — dailymed⟩"


def test_identity_tag_empty_title_is_empty_string():
    assert identity_tag(_atom(title="")) == ""
    assert identity_tag(_atom(title="   ")) == ""


def test_identity_tag_without_source_key():
    assert identity_tag(_atom(source="")) == f"⟨{_TITLE}⟩"


def test_identity_tag_strips_newlines_verbatim_otherwise():
    a = _atom(title="EXAMPLE\nCOMPOUND\r\n  TABLET")
    assert identity_tag(a) == "⟨EXAMPLE COMPOUND TABLET — dailymed⟩"


def test_identity_tag_caps_long_titles():
    long_title = "T" * 200
    tag = identity_tag(_atom(title=long_title))
    assert tag == "⟨" + "T" * 80 + "… — dailymed⟩"


# ---- surface 1: planner observations --------------------------------------------------------

def test_planner_obs_off_is_byte_identical():
    llm = RecordingLLM(_grounded_script())
    res = _run(llm, evidence_identity=False)
    assert res.grounded
    step2 = llm.prompts[1]                       # the planner step AFTER the search gathered a1
    assert f"a1: {_BLOCK_TEXT}" in step2         # exact pre-change rendering
    assert "⟨" not in step2
    assert IDENTITY_INSTRUCTION not in step2


def test_planner_obs_on_carries_identity_tag_and_instruction():
    llm = RecordingLLM(_grounded_script())
    res = _run(llm, evidence_identity=True)
    assert res.grounded
    step2 = llm.prompts[1]
    assert f"a1 {_TAG}: {_BLOCK_TEXT}" in step2
    assert IDENTITY_INSTRUCTION in step2         # the ONE added claim-writing sentence


def test_planner_obs_on_tagless_atom_renders_as_today():
    # An atom with no document_title must render exactly as the OFF path even when ON.
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=_BLOCK_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"})))
    llm = RecordingLLM(_grounded_script())
    res = asyncio.run(run_react(
        question="what was the metric value?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=src, tenant_id="A", budget=BudgetState(max_calls=20), evidence_identity=True))
    assert res.grounded
    assert f"a1: {_BLOCK_TEXT}" in llm.prompts[1]     # the obs line is untagged (as today)
    assert "a1 ⟨" not in llm.prompts[1]               # (the ON-path instruction sentence still
    #                                                   mentions ⟨⟩ — that's expected)


# ---- surface 4: compose findings ------------------------------------------------------------

def test_compose_findings_off_is_byte_identical():
    llm = RecordingLLM(_grounded_script())
    _run(llm, evidence_identity=False)
    compose = next(p for p in llm.prompts if "VERIFIED FINDINGS" in p)
    assert ('[1] the metric value was 9.8 percent  (quote: "the approved metric value was '
            '9.8 percent" — source: dailymed)') in compose    # exact pre-change rendering
    assert "⟨" not in compose


def test_compose_findings_on_appends_identity_tag():
    llm = RecordingLLM(_grounded_script())
    _run(llm, evidence_identity=True)
    compose = next(p for p in llm.prompts if "VERIFIED FINDINGS" in p)
    assert ('[1] the metric value was 9.8 percent  (quote: "the approved metric value was '
            f'9.8 percent" — source: dailymed {_TAG})') in compose


# ---- surfaces 2+3: claims-first extractor batch + entailment items --------------------------

def _run_claims_first(monkeypatch, *, evidence_identity: bool):
    """run_react with claims_first=True; extract/entail are captured at the claims_first module."""
    captured: dict = {}
    import roster_kernel.research.claims_first as cf

    async def fake_extract(**kw):
        captured["extract"] = kw
        return [{"text": "metric fact", "atom_id": "a1",
                 "quote": "approved metric value was 9.8 percent"}]

    async def fake_entail(**kw):
        captured["entail"] = kw
        return [True] * len(kw["claims"])

    monkeypatch.setattr(cf, "extract_claims", fake_extract)
    monkeypatch.setattr(cf, "entail_claims", fake_entail)
    llm = RecordingLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent")]),
        ComposedAnswer(answer="The approved metric value was 9.8 percent [1]."),
    ])
    res = asyncio.run(run_react(
        question="what was the metric value?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=_source(), tenant_id="A", budget=BudgetState(max_calls=20),
        claims_first=True, evidence_identity=evidence_identity))
    return res, captured


def test_claims_first_off_passes_raw_atoms_and_no_tags(monkeypatch):
    res, captured = _run_claims_first(monkeypatch, evidence_identity=False)
    assert res.grounded
    assert captured["extract"]["atoms"] == [("a1", _BLOCK_TEXT)]      # exact pre-change tuples
    assert captured["extract"]["evidence_identity"] is False
    assert captured["entail"]["tags"] is None


def test_claims_first_on_passes_tagged_atoms_and_parallel_tags(monkeypatch):
    res, captured = _run_claims_first(monkeypatch, evidence_identity=True)
    assert res.grounded
    assert captured["extract"]["atoms"] == [("a1", f"{_TAG} {_BLOCK_TEXT}")]
    assert captured["extract"]["evidence_identity"] is True
    assert captured["entail"]["tags"] == [_TAG]


class _FakeJson:
    """Records the (system, user) of every complete_json call; returns a scripted payload."""
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, str, str]] = []

    async def complete_json(self, *, model, system, user):
        self.calls.append((model, system, user))
        return self.payload


def test_extractor_batch_rendering_and_system_sentence():
    from roster_kernel.research.claims_first import extract_claims
    # OFF: the batch block and system are byte-identical to the pre-change rendering
    cl = _FakeJson({"claims": []})
    asyncio.run(extract_claims(question="q?", atoms=[("a1", _BLOCK_TEXT)], client=cl))
    _, system, user = cl.calls[0]
    assert f"[a1] {_BLOCK_TEXT}" in user
    assert IDENTITY_INSTRUCTION not in system
    # ON: the caller renders the tag INTO the text; the system gains exactly one sentence
    cl2 = _FakeJson({"claims": []})
    asyncio.run(extract_claims(question="q?", atoms=[("a1", f"{_TAG} {_BLOCK_TEXT}")],
                               client=cl2, evidence_identity=True))
    _, system2, user2 = cl2.calls[0]
    assert f"[a1] {_TAG} {_BLOCK_TEXT}" in user2
    assert system2 == system + "\n- " + IDENTITY_INSTRUCTION


def test_entailment_items_rendering_with_and_without_tags():
    from roster_kernel.research.claims_first import entail_claims
    claims = [{"text": "c1", "atom_id": "a1", "quote": "q1"},
              {"text": "c2", "atom_id": "a2", "quote": "q2"}]
    # OFF (tags=None): byte-identical pre-change item rendering
    cl = _FakeJson({"verdicts": [{"i": 0, "ok": True}, {"i": 1, "ok": True}]})
    out = asyncio.run(entail_claims(claims=claims, client=cl))
    assert out == [True, True]
    user = cl.calls[0][2]
    assert "ITEM 0\nCLAIM: c1\nQUOTE: q1\n\nITEM 1\nCLAIM: c2\nQUOTE: q2" in user
    assert "SOURCE:" not in user
    # ON: each item with a non-empty tag gains a SOURCE line; empty tags render as today
    cl2 = _FakeJson({"verdicts": [{"i": 0, "ok": True}, {"i": 1, "ok": True}]})
    out2 = asyncio.run(entail_claims(claims=claims, client=cl2, tags=[_TAG, ""]))
    assert out2 == [True, True]
    user2 = cl2.calls[0][2]
    assert f"ITEM 0\nCLAIM: c1\nSOURCE: {_TAG}\nQUOTE: q1" in user2
    assert "ITEM 1\nCLAIM: c2\nQUOTE: q2" in user2


# ---- surface 6: fallback grounder -----------------------------------------------------------

def _run_fallback(monkeypatch, *, evidence_identity: bool):
    """Loop abstains (answer with 0 claims + 3 extract retries) → the fallback grounder fires."""
    captured: dict = {}
    import roster_kernel.research.fallback_grounder as fg

    async def fake_ground(**kw):
        captured.update(kw)
        return []

    monkeypatch.setattr(fg, "ground_claimless", fake_ground)
    llm = RecordingLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[]),      # abstains
        AgentStep(action="answer", claims=[]),      # extract-recovery retries (3)
        AgentStep(action="answer", claims=[]),
        AgentStep(action="answer", claims=[]),
    ])
    asyncio.run(run_react(
        question="what was the metric value?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=_source(), tenant_id="A", budget=BudgetState(max_calls=20),
        evidence_identity=evidence_identity))
    return captured


def test_fallback_grounder_off_sees_raw_atom_text(monkeypatch):
    captured = _run_fallback(monkeypatch, evidence_identity=False)
    assert captured["atoms"] == [("a1", _BLOCK_TEXT)]     # exact pre-change tuples


def test_fallback_grounder_on_sees_identity_tagged_text(monkeypatch):
    captured = _run_fallback(monkeypatch, evidence_identity=True)
    assert captured["atoms"] == [("a1", f"{_TAG} {_BLOCK_TEXT}")]


# ---- surface 5: panel synthesis -------------------------------------------------------------

from dataclasses import dataclass


@dataclass(frozen=True)
class _Spec:
    id: str
    specialty: str
    lens: str
    focus: str
    source_keys: tuple = ()


class _PanelLLM:
    """Routes by prompt content (mirrors test_panel.py) and records every prompt."""
    def __init__(self):
        self.prompts: list[str] = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        c = messages[0]["content"]
        self.prompts.append(c)
        if "VERIFIED PANEL FINDINGS" in c:
            return LLMResult(parsed=ComposedAnswer(
                answer="The metric value is 9.8 percent [1]."), output_tokens=5)
        if "VERIFIED FINDINGS" in c:
            return LLMResult(parsed=ComposedAnswer(answer="Value is 9.8 percent [1]."), output_tokens=5)
        if "no evidence yet" in c:
            return LLMResult(parsed=AgentStep(action="search", query="metric"), output_tokens=5)
        return LLMResult(parsed=AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent")]), output_tokens=5)


def _run_panel(*, evidence_identity: bool):
    from roster_kernel.research.panel import run_panel
    src = _source()
    llm = _PanelLLM()
    r = asyncio.run(run_panel(
        question="What is the metric value?",
        specialists=[_Spec("s1", "Specialty One", "You are specialist one.", "metrics")],
        llm=llm, embedder=FakeEmbedder(dim=8), make_retrievers=lambda keys: (src, None),
        tenant_id="A", synthesis_directive="Synthesize.", evidence_identity=evidence_identity))
    return r, llm


def test_panel_synthesis_off_is_byte_identical():
    r, llm = _run_panel(evidence_identity=False)
    assert r.claims
    synth = next(p for p in llm.prompts if "VERIFIED PANEL FINDINGS" in p)
    assert ('[1] (Specialty One) the metric value was 9.8 percent  (quote: "the approved '
            'metric value was 9.8 percent" — dailymed)') in synth   # exact pre-change rendering
    assert "⟨" not in synth


def test_panel_synthesis_on_appends_identity_tag():
    r, llm = _run_panel(evidence_identity=True)
    assert r.claims
    synth = next(p for p in llm.prompts if "VERIFIED PANEL FINDINGS" in p)
    assert ('[1] (Specialty One) the metric value was 9.8 percent  (quote: "the approved '
            f'metric value was 9.8 percent" — dailymed {_TAG})') in synth
    # the flag also reached the specialist's own planner loop (surface 1 inside the panel)
    spec_step = next(p for p in llm.prompts
                     if "EVIDENCE GATHERED SO FAR" in p and f"a1 {_TAG}:" in p)
    assert spec_step
