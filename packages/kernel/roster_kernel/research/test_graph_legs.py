"""A9 graph-guided evidence legs — offline contract tests (scripted LLM, in-memory source).

The contract: merged legs pre-gather citable evidence the question's wording would never
retrieve; shadow legs retrieve + log but merge NOTHING; no legs → byte-identical loop.
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


class ScriptedLLM:
    def __init__(self, steps):
        self._steps = list(steps)

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._steps.pop(0), output_tokens=5, model="scripted")


_ADJ_TEXT = "anemia of chronic kidney disease is treated per the KDIGO guideline"


def _source() -> InMemoryRetrievalSource:
    """One block only findable via the graph-leg query's terms (adjacent topic), one via the
    question's own terms."""
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="q1", document_id="dq", tenant_id="A",
        text="fatigue workup includes history and examination",
        locator=Locator("block_span", "dq", {"block_id": "q1"})))
    src.add(IndexedBlock(
        block_id="g1", document_id="dg", tenant_id="A", text=_ADJ_TEXT,
        locator=Locator("block_span", "dg", {"block_id": "g1"})))
    return src


_LEGS = [{"query": "anemia kdigo guideline treated", "note": "ckd increases_risk_of anemia"}]


def _run(llm, *, legs=None, shadow=False, late=False):
    return asyncio.run(run_react(
        question="fatigue workup examination", llm=llm, embedder=FakeEmbedder(dim=8),
        source=_source(), tenant_id="A", budget=BudgetState(max_calls=10), max_steps=4,
        graph_legs=legs, graph_shadow=shadow, graph_late=late, collect_diagnostics=True))


def test_merged_leg_evidence_is_present_and_citable():
    llm = ScriptedLLM([
        AgentStep(action="search", query="fatigue workup examination"),
        AgentStep(action="answer", claims=[
            # cites the GRAPH-pulled atom (a1 = first atom added — the leg runs pre-loop)
            ClaimOut(text="anemia of CKD is treated per KDIGO", atom_id="a1",
                     quote="anemia of chronic kidney disease is treated per the KDIGO guideline"),
        ]),
    ])
    res = _run(llm, legs=_LEGS)
    assert res.stopped_reason == "answered" and res.grounded
    assert len(res.verified_claims) == 1          # graph-leg block passed the span gate
    gl = res.diagnostics["graph_legs"]
    assert gl["shadow"] is False and gl["legs"][0]["merged"] >= 1


_Q_CLAIM = ClaimOut(text="workup includes history and exam", atom_id="a1",
                    quote="fatigue workup includes history and examination")


def test_shadow_leg_merges_nothing_but_logs():
    llm = ScriptedLLM([
        AgentStep(action="search", query="fatigue workup examination"),
        AgentStep(action="answer", claims=[_Q_CLAIM]),
    ])
    res = _run(llm, legs=_LEGS, shadow=True)
    gl = res.diagnostics["graph_legs"]
    assert gl["shadow"] is True
    assert gl["legs"][0]["hits"] >= 1 and gl["legs"][0]["merged"] == 0
    # only the planner's own search contributed atoms
    assert res.atoms_gathered == 1


def test_late_merge_keeps_loop_identical_but_adds_atoms_post_loop():
    llm = ScriptedLLM([
        AgentStep(action="search", query="fatigue workup examination"),
        AgentStep(action="answer", claims=[_Q_CLAIM]),
    ])
    res = _run(llm, legs=_LEGS, late=True)
    gl = res.diagnostics["graph_legs"]
    assert gl["mode"] == "late" and gl["legs"][0]["hits"] >= 1
    # the loop's own claim verified exactly as in the no-legs run (planner untouched)...
    assert res.stopped_reason == "answered" and len(res.verified_claims) == 1
    # ...and the stash landed AFTER the loop: total atoms = planner's 1 + late-merged
    assert gl["legs"][0]["merged"] >= 1
    assert res.atoms_gathered == 1 + gl["legs"][0]["merged"]


def test_no_legs_is_byte_identical_loop():
    llm = ScriptedLLM([
        AgentStep(action="search", query="fatigue workup examination"),
        AgentStep(action="answer", claims=[_Q_CLAIM]),
    ])
    res = _run(llm, legs=None)
    assert "graph_legs" not in (res.diagnostics or {})
    assert res.atoms_gathered == 1


def test_match_topics_containment_word_boundary():
    from roster_kernel.graph.store import GraphStore, build_adjacency

    g = GraphStore.__new__(GraphStore)          # no DB: stub the snapshot
    adj = build_adjacency([
        {"id": "e1", "subject": "chronic kidney disease", "subject_norm": "chronic kidney disease",
         "relation": "increases_risk_of", "object": "anemia", "object_norm": "anemia",
         "context_topic": "", "label": "established", "provenance": "curated", "confidence": 1.0},
    ])

    async def _adj():
        return adj
    g._adjacency = _adj

    async def go(text):
        return await g.match_topics(text)
    assert asyncio.run(go("Why is my patient with chronic kidney disease tired?")) == \
        ["chronic kidney disease"]
    assert asyncio.run(go("anemia, refractory")) == ["anemia"]     # punctuation-safe
    assert asyncio.run(go("pandemic response")) == []              # no substring false-positive


def test_match_topics_punctuated_labels_match_stripped_text():
    """v3 panel bug: labels like 'NASH / MASH' keep punctuation in their norm while the
    question text is stripped — the compare must normalize BOTH sides."""
    from roster_kernel.graph.store import GraphStore, build_adjacency

    g = GraphStore.__new__(GraphStore)
    adj = build_adjacency([
        {"id": "e1", "subject": "NASH / MASH", "subject_norm": "nash / mash",
         "relation": "causes", "object": "variceal bleeding / cirrhosis",
         "object_norm": "variceal bleeding / cirrhosis", "context_topic": "",
         "label": "established", "provenance": "curated", "confidence": 1.0},
    ])

    async def _adj():
        return adj
    g._adjacency = _adj

    async def go(text):
        return await g.match_topics(text)
    assert asyncio.run(go("patient with NASH MASH and fibrosis")) == ["nash / mash"]
