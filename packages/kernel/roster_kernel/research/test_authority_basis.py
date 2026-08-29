"""Authority-basis (ROSTER_AUTHORITY_BASIS / T1+T2) tests.

Uses `asyncio.run` to drive run_react (mirroring the sibling run_react harnesses
`test_entity_open_leg.py` / `test_react.py` / `test_freshness_rank.py`). NOTE: these are NOT
pytest-asyncio tests. An earlier pytest-asyncio version of this file perturbed the shared event-loop
state enough to flip the concurrent explore-leg SEARCH ORDER that
`test_explore_legs.py::test_flag_on_enumerative_behavior_unchanged` asserts is deterministic — a
cross-test contamination (that test uses `asyncio.run` and compares gather completion order). The
`asyncio.run` sibling harnesses have no such effect, so this file follows them. `asyncio.run` is the
standard entrypoint, not a hand-rolled loop.

Drives the REAL `run_react` with fakes:
- an LLM that scripts the planner steps and captures the compose `compose_user` (raising on the
  compose call — the partition + directive assembly both run BEFORE compose, so the verified-claim
  order and the compose prompt are already final when we inspect them);
- a corpus of blocks with IDENTICAL text (cosine ties → atom ids a1,a2,a3 follow insertion order,
  so the input claim order is deterministic) but distinct `source_key` per tier;
- a fake `classify_evidence` (source_key → evidence_kind) and `evidence_ranker` (evidence_kind →
  tier), so the tier of each verified claim is controlled structurally (Rule 18).

Asserts:
- OFF (authority_basis=False): claim order == pre-partition input order (byte-identical), and the
  authority-basis directive is NOT in the compose prompt.
- ON + not suppressed: the rank<=1 (blog) claim moves to the BACK, intra-bucket order preserved,
  and the directive IS in the compose prompt.
- ON but suppressed (suppress_authority=True): no partition, no directive (exempt stance path).
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource

_TXT = "shared body about the topic xyz that grounds a quote here"
_QUOTE = "shared body about the topic"
_MARKER = "AUTHORITY_FLOOR_MARKER"

# structural tier map (Rule 18): news → analysis tier (front bucket), blog → unstamped rank 0 (back)
_KIND = {"NEWS1": "news", "BLOG1": "blog", "NEWS2": "news"}
_RANK = {"news": 4, "blog": 0, "social": 1}


def _classify(source_key, facets, title, text):
    return _KIND.get(source_key, "")


def _ranker(kind):
    return _RANK.get(kind, 0)


class _CaptureLLM:
    """Scripts planner AgentSteps and captures the compose prompt. The compose call raises so we can
    read `compose_user` without needing a scripted ComposedAnswer — the answer text is irrelevant to
    these assertions (claim order + directive presence are both finalized before compose runs)."""

    def __init__(self, steps):
        self._steps = list(steps)
        self.compose_prompts: list[str] = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        if getattr(response_format, "__name__", "") == "ComposedAnswer":
            self.compose_prompts.append(messages[0]["content"])
            raise IndexError("no scripted compose")
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3, model="fake")


def _corpus() -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    # identical text → cosine ties → atom ids a1,a2,a3 in insertion order (deterministic input order)
    for i, sk in enumerate(("NEWS1", "BLOG1", "NEWS2")):
        src.add(IndexedBlock(block_id=f"b{i}", document_id=f"d{i}", tenant_id="A", text=_TXT,
                             source_key=sk, locator=Locator("block_span", f"d{i}", {"block_id": f"b{i}"})))
    return src


def _drive(*, authority_basis: bool, suppress: bool = False, ranker=_ranker):
    """Run through search (step 0) + answer (step 1); return (claim_texts, compose_prompts)."""
    llm = _CaptureLLM([
        AgentStep(action="search", query="topic xyz"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="c-news1", atom_id="a1", quote=_QUOTE),   # tier 4 (front)
            ClaimOut(text="c-blog1", atom_id="a2", quote=_QUOTE),   # tier 0 (back)
            ClaimOut(text="c-news2", atom_id="a3", quote=_QUOTE),   # tier 4 (front)
        ]),
    ])
    res = asyncio.run(run_react(
        question="q", llm=llm, embedder=FakeEmbedder(dim=8), source=_corpus(),
        tenant_id="A", budget=BudgetState(max_calls=20), max_steps=4,
        classify_evidence=_classify, evidence_ranker=ranker,
        authority_basis=authority_basis, authority_basis_directive=_MARKER,
        suppress_authority=suppress))
    return [vc.text for vc in res.verified_claims], llm.compose_prompts


def test_off_is_byte_identical_order_and_no_directive():
    order, prompts = _drive(authority_basis=False)
    # input order preserved (no partition)
    assert order == ["c-news1", "c-blog1", "c-news2"], order
    # the directive never reaches compose
    assert prompts and all(_MARKER not in p for p in prompts)


def test_on_pushes_low_basis_to_back_and_appends_directive():
    order, prompts = _drive(authority_basis=True)
    # rank<=1 (blog) → back; the two rank>=2 (news) claims stay in front, intra-bucket order preserved
    assert order == ["c-news1", "c-news2", "c-blog1"], order
    # the floor directive IS appended to the compose prompt
    assert prompts and any(_MARKER in p for p in prompts)


def test_suppressed_stance_is_exempt():
    # suppress_authority=True (opinion/foresight stance path) → _suppress_auth True → no partition,
    # no directive: byte-identical to OFF even though the flag is on.
    order, prompts = _drive(authority_basis=True, suppress=True)
    assert order == ["c-news1", "c-blog1", "c-news2"], order
    assert prompts and all(_MARKER not in p for p in prompts)


def test_on_without_ranker_is_byte_identical_order():
    # No ranker → the partition cannot tier claims → skipped → byte-identical order (the directive
    # still appends: it is gated on the flag + directive, not the ranker).
    order, prompts = _drive(authority_basis=True, ranker=None)
    assert order == ["c-news1", "c-blog1", "c-news2"], order
    assert prompts and any(_MARKER in p for p in prompts)
