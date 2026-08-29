"""Tests for the red-team REFUTER (ROSTER_INTELLIGENCE_CORE / T-B).

Two layers:
  1. `refute_hypothesis` unit contract — FAIL-CLOSED (None judge / blank claim / judge error / empty
     → []), capped success, budget charged.
  2. `run_react`-level wiring (reuses the T2 `test_intelligence_retrieval` harness): with a genuinely
     cross-family judge the AGAINST legs use the REFUTER's queries (not the self-authored one); with no
     cross-family judge (`derive_judge_llm` None or == llm) they fall back to the self-authored
     `against_query`; a hypothesis whose against-search finds ZERO hits is marked under-tested; the
     under-tested note reaches the compose prompt; OFF (`hypotheses=None`) → no refuter, byte-identical.
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import BlockHit, Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import FakeLLM, LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.intelligence_draft import Hypothesis
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer, run_react
from roster_kernel.research.refuter import RefuterQueries, refute_hypothesis

# Reuse the T2 harness pieces (same package).
from roster_kernel.research.test_intelligence_retrieval import (
    RecordingSource,
    ScriptedLLM,
    _agentic_steps,
)


# ---------------------------------------------------------------------------
# helpers: a cross-family judge fake that answers ANY response_format shape
# (RefuterQueries for the refuter; GroundingProbe/etc. default-constructed).

def _judge(queries, *, raise_exc=False):
    """A FakeLLM standing in for the cross-family judge. For a RefuterQueries request it returns the
    configured queries; for any other shape (e.g. the grounding gate's GroundingProbe) it returns the
    default-constructed object so those callers see a benign empty verdict. `raise_exc` makes the
    refuter call blow up (to exercise the fail-closed error path)."""
    def _b(system, messages, response_format):
        if raise_exc and response_format is RefuterQueries:
            raise RuntimeError("boom")
        if response_format is RefuterQueries:
            return RefuterQueries(queries=list(queries))
        return response_format()
    return FakeLLM(_b)


# ===========================================================================
# 1. refute_hypothesis — unit contract
# ===========================================================================

def test_none_judge_fails_closed() -> None:
    b = BudgetState(max_calls=5)
    assert asyncio.run(refute_hypothesis("some claim", None, budget=b)) == []
    assert b.spent_calls == 0                       # no call attempted


def test_blank_claim_fails_closed() -> None:
    b = BudgetState(max_calls=5)
    j = _judge(["q1", "q2"])
    assert asyncio.run(refute_hypothesis("   ", j, budget=b)) == []
    assert asyncio.run(refute_hypothesis("", j, budget=b)) == []
    assert b.spent_calls == 0


def test_judge_error_fails_closed() -> None:
    b = BudgetState(max_calls=5)
    j = _judge(["q1"], raise_exc=True)
    assert asyncio.run(refute_hypothesis("a claim", j, budget=b)) == []


def test_empty_queries_returns_empty() -> None:
    b = BudgetState(max_calls=5)
    j = _judge([])
    assert asyncio.run(refute_hypothesis("a claim", j, budget=b)) == []


def test_blank_queries_filtered() -> None:
    b = BudgetState(max_calls=5)
    j = _judge(["", "  ", "real query"])
    assert asyncio.run(refute_hypothesis("a claim", j, budget=b)) == ["real query"]


def test_success_capped_and_budget_charged() -> None:
    b = BudgetState(max_calls=5)
    j = _judge(["q1", "q2", "q3"])                  # 3 returned, n=2 → capped to 2
    out = asyncio.run(refute_hypothesis("a claim", j, budget=b, n=2))
    assert out == ["q1", "q2"], out
    assert b.spent_calls == 1                        # exactly one judge call charged


def test_n_le_zero_returns_empty() -> None:
    b = BudgetState(max_calls=5)
    j = _judge(["q1"])
    assert asyncio.run(refute_hypothesis("a claim", j, budget=b, n=0)) == []


# ===========================================================================
# 2. run_react-level wiring (RecordingSource harness)
# ===========================================================================

def _drive(hypotheses, *, judge=None, budget=None):
    src = RecordingSource()
    llm = ScriptedLLM(_agentic_steps())
    res = asyncio.run(run_react(
        question="what explains the outcome?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=budget or BudgetState(max_calls=50),
        max_steps=4, hypotheses=hypotheses, derive_judge_llm=judge,
        collect_diagnostics=True))
    return res, src


def test_against_legs_use_refuter_queries_with_cross_family_judge() -> None:
    """A genuinely cross-family judge (distinct from llm) authors the disconfirming queries → the
    AGAINST legs search the REFUTER's queries, NOT the hypothesis's self-authored against_query."""
    j = _judge(["refuter disconfirm A", "refuter disconfirm B"])
    hyps = [Hypothesis(claim="H1 claim", for_query="h1 for",
                       against_query="h1 SELF against")]
    res, src = _drive(hyps, judge=j)
    assert "refuter disconfirm A" in src.queries, src.queries
    assert "refuter disconfirm B" in src.queries, src.queries
    assert "h1 SELF against" not in src.queries, src.queries     # self-authored REPLACED
    intel = (res.diagnostics or {}).get("intelligence")
    assert intel == {"hypotheses": 1, "for_hits": 1, "against_hits": 2}, intel
    rf = (res.diagnostics or {}).get("refuter")
    assert rf["refuter_queries"] == 2 and rf["hypotheses"] == 1, rf
    assert rf["undertested"] == []                              # both against legs found a hit


def test_refuter_against_cap_two_per_hypothesis() -> None:
    """A judge that offers many disconfirming queries is capped to 2 AGAINST legs per hypothesis."""
    j = _judge(["ref1", "ref2", "ref3", "ref4"])
    hyps = [Hypothesis(claim="H1", for_query="h1 for", against_query="h1 self")]
    res, src = _drive(hyps, judge=j)
    # refute_hypothesis itself caps at n=2, so only ref1/ref2 are ever authored
    assert "ref1" in src.queries and "ref2" in src.queries
    assert "ref3" not in src.queries and "ref4" not in src.queries, src.queries
    assert (res.diagnostics or {}).get("intelligence")["against_hits"] == 2


def test_no_judge_falls_back_to_self_authored() -> None:
    """No cross-family judge (`derive_judge_llm=None`) → the AGAINST leg uses the self-authored
    `against_query` (today's behavior)."""
    hyps = [Hypothesis(claim="H1 claim", for_query="h1 for", against_query="h1 SELF against")]
    res, src = _drive(hyps, judge=None)
    assert "h1 SELF against" in src.queries, src.queries
    rf = (res.diagnostics or {}).get("refuter")
    assert rf["refuter_queries"] == 0, rf                       # refuter never fired


def test_same_family_judge_is_not_cross_family() -> None:
    """`derive_judge_llm is llm` (SAME family) → treated as no cross-family judge → self-authored
    fallback (the exact grounding-gate 'genuinely cross-family' check)."""
    src = RecordingSource()
    llm = ScriptedLLM(_agentic_steps())
    res = asyncio.run(run_react(
        question="q", llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=50), max_steps=4,
        hypotheses=[Hypothesis(claim="H1", for_query="h1 for", against_query="h1 SELF against")],
        derive_judge_llm=llm,                                   # SAME object → not cross-family
        collect_diagnostics=True))
    assert "h1 SELF against" in src.queries, src.queries
    assert (res.diagnostics or {}).get("refuter")["refuter_queries"] == 0


def test_refuter_error_falls_back_to_self_authored() -> None:
    """A refuter that ERRORS fails closed → the self-authored against_query is used (never worse)."""
    j = _judge(["never returned"], raise_exc=True)
    hyps = [Hypothesis(claim="H1", for_query="h1 for", against_query="h1 SELF against")]
    res, src = _drive(hyps, judge=j)
    assert "h1 SELF against" in src.queries, src.queries
    assert (res.diagnostics or {}).get("refuter")["refuter_queries"] == 0


# --- under-tested flag: an AGAINST search that finds nothing ---------------

class _EmptyAgainstSource(RecordingSource):
    """Corpus source that returns a hit for every query EXCEPT ones containing `marker` (the refuter's
    disconfirming queries), which come back empty — so the AGAINST search surfaces ZERO hits."""

    def __init__(self, marker: str) -> None:
        super().__init__()
        self._marker = marker

    async def search(self, req):
        self.queries.append(req.query)
        if self._marker in req.query:
            return []                                           # disconfirming search finds NOTHING
        self._n += 1
        blk = f"b{self._n}"
        text = f"evidence body {self._n} for query: {req.query}"
        self._texts[blk] = text
        return [BlockHit(document_id=f"d{self._n}", block_id=blk, text=text,
                         locator=Locator("block_span", f"d{self._n}", {"block_id": blk}),
                         document_title=f"doc {self._n}")]


def test_undertested_when_against_search_empty() -> None:
    """A hypothesis whose disconfirming search returns 0 hits is marked UNDER-TESTED (result field +
    diag), and a hypothesis whose against-search finds a hit is NOT."""
    j = _judge(["REFUTE this claim"])                           # 1 disconfirming query, marker "REFUTE"
    src = _EmptyAgainstSource("REFUTE")
    llm = ScriptedLLM(_agentic_steps())
    res = asyncio.run(run_react(
        question="q", llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=50), max_steps=4,
        hypotheses=[Hypothesis(claim="H1 the claim", for_query="h1 for",
                               against_query="h1 self")],
        derive_judge_llm=j, collect_diagnostics=True))
    assert "REFUTE this claim" in src.queries, src.queries
    assert res.intelligence_undertested == [{"index": 1, "claim": "H1 the claim"}], \
        res.intelligence_undertested
    rf = (res.diagnostics or {}).get("refuter")
    assert rf["undertested"] == [1], rf
    assert (res.diagnostics or {}).get("intelligence")["against_hits"] == 0


# --- OFF: hypotheses is None → no refuter, byte-identical -------------------

def test_off_no_hypotheses_no_refuter() -> None:
    """`hypotheses=None` → no refuter, no under-tested tracking, no `refuter` diag key, and the only
    corpus query is the planner's own search (byte-identical to today)."""
    j = _judge(["should never fire"])
    res, src = _drive(None, judge=j)
    assert "refuter" not in (res.diagnostics or {})
    assert "intelligence" not in (res.diagnostics or {})
    assert res.intelligence_undertested == []
    assert "should never fire" not in src.queries
    assert src.queries == ["planner original query"], src.queries


# ===========================================================================
# 3. under-tested note reaches the COMPOSE prompt + the runtime render
# ===========================================================================

_FIXED_TEXT = "Widget adoption rose sharply across the market in 2024."


class _ComposeEmptyAgainstSource:
    """Corpus source for the compose path: returns the stable b1 block (so a claim citing `a1`
    verifies and compose runs) for every query EXCEPT disconfirming ones containing `marker`, which
    return [] → the AGAINST search finds nothing → the hypothesis is under-tested."""

    key = "corpus"

    def __init__(self, marker: str) -> None:
        self.queries: list[str] = []
        self._marker = marker

    def make_block_loader(self, tenant_id, workspace_id=None):
        def _load(document_id, block_id):
            return _FIXED_TEXT if block_id == "b1" else None
        return _load

    async def search(self, req):
        self.queries.append(req.query)
        if self._marker in req.query:
            return []
        return [BlockHit(document_id="d1", block_id="b1", text=_FIXED_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"}),
                         document_title="doc 1")]


class _RecordingComposeLLM:
    """Serves the agentic loop then records the compose prompt (identified by ComposedAnswer)."""

    def __init__(self) -> None:
        self.compose_users: list[str] = []
        self._loop = [
            AgentStep(action="search", query="planner original query"),
            AgentStep(action="answer",
                      claims=[ClaimOut(text="Adoption rose.", atom_id="a1",
                                       quote="Widget adoption rose sharply")]),
        ]

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        if getattr(response_format, "__name__", "") == "ComposedAnswer":
            self.compose_users.append(messages[-1]["content"])
            return LLMResult(parsed=ComposedAnswer(answer="The evidence best supports H1 [1]."),
                             output_tokens=5, model="scripted")
        return LLMResult(parsed=self._loop.pop(0), output_tokens=3, model="scripted")


def test_undertested_note_in_compose_prompt() -> None:
    """An under-tested hypothesis surfaces a caution block in the compose prompt so the model does not
    treat a hypothesis with zero disconfirming evidence as confirmed."""
    j = _judge(["REFUTE adoption"])                            # disconfirming query returns nothing
    src = _ComposeEmptyAgainstSource("REFUTE")
    llm = _RecordingComposeLLM()
    res = asyncio.run(run_react(
        question="what explains the outcome?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=60), max_steps=4,
        hypotheses=[Hypothesis(claim="Network effects drove adoption", for_query="net effects",
                               against_query="self against")],
        intelligence_frame="Adoption is driven by network effects.",
        derive_judge_llm=j, collect_diagnostics=True))
    assert res.composed_answer, "compose did not run (need a verified claim)"
    assert llm.compose_users, "no compose prompt recorded"
    cu = llm.compose_users[0]
    assert "UNDER-TESTED" in cu, cu
    assert "H1: Network effects drove adoption" in cu, cu
    assert res.intelligence_undertested == [{"index": 1, "claim": "Network effects drove adoption"}]


def test_render_undertested_present_and_absent() -> None:
    """The runtime render: a labeled section from under-tested entries when present, "" when empty
    (OFF byte-identical)."""
    from roster_kernel.runtime.research import _render_undertested
    assert _render_undertested([]) == ""
    assert _render_undertested([{"nope": 1}]) == ""            # no valid entry → no section
    out = _render_undertested([{"index": 2, "claim": "Price cuts drove adoption"}])
    assert out.startswith("## Under-tested — not yet disconfirmed")
    assert "- H2: Price cuts drove adoption — not yet disconfirmed by any evidence found" in out
