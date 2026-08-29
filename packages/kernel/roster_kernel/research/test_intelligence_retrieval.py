"""Tests for the adversarial FOR/AGAINST pre-seed retrieval (ROSTER_INTELLIGENCE_CORE / T2).

Drives the REAL `run_react` with `hypotheses=[Hypothesis(...)]` present and fakes:
- a RecordingSource (corpus) that records EVERY query it is asked to search and returns one
  distinct hit per query (so each pre-seed leg adds an atom to the pool);
- a ScriptedLLM that serves the agentic loop's planner steps (a search then an answer) — the
  pre-seed runs BEFORE this loop, which still runs in full.

Covers (T2 contract):
- 2 hypotheses → exactly a FOR-query AND an AGAINST-query retrieval per hypothesis (4 targeted
  corpus searches), all seeding the atom pool;
- capped at 3 hypotheses — a 4th hypothesis's queries never fire;
- a blank against_query → that leg is skipped (no crash), only the FOR leg fires;
- OFF (`hypotheses=None`) → NO pre-seed retrieval, no `intelligence` diag, and the agentic loop
  runs exactly as today (byte-identical) — the only corpus query is the planner's search.
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import BlockHit, Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.intelligence_draft import Hypothesis
from roster_kernel.research.react import AgentStep, ClaimOut, run_react


class RecordingSource:
    """Fake corpus source: records the `query` of every request and returns one distinct hit per
    query (unique doc/block id) so each pre-seed leg contributes a fresh atom to the pool."""

    key = "corpus"

    def __init__(self) -> None:
        self.queries: list[str] = []
        self._texts: dict[str, str] = {}
        self._n = 0

    def make_block_loader(self, tenant_id, workspace_id=None):
        def _load(document_id, block_id):
            return self._texts.get(block_id)
        return _load

    async def search(self, req):
        self.queries.append(req.query)
        self._n += 1
        blk = f"b{self._n}"
        text = f"evidence body {self._n} for query: {req.query}"
        self._texts[blk] = text
        return [BlockHit(document_id=f"d{self._n}", block_id=blk, text=text,
                         locator=Locator("block_span", f"d{self._n}", {"block_id": blk}),
                         document_title=f"doc {self._n}")]


class ScriptedLLM:
    """Serves the agentic loop: pops pre-scripted AgentStep/answer objects in order (ignores prompt).
    Popping past the end raises IndexError, which compose's own retry loop tolerates (answer still
    surfaces from verified/rejected claims)."""

    def __init__(self, steps):
        self._steps = list(steps)

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._steps.pop(0), output_tokens=3, model="scripted")


def _agentic_steps():
    # a benign search then an answer whose claim resolves to no known atom → REJECTED (not empty),
    # which finalizes the loop without triggering extract-recovery re-asks. We assert on the pre-seed
    # legs, not on grounded output.
    return [
        AgentStep(action="search", query="planner original query"),
        AgentStep(action="answer",
                  claims=[ClaimOut(text="x", atom_id="no_such_atom", quote="whatever")]),
    ]


def _drive(hypotheses, *, budget=None):
    src = RecordingSource()
    llm = ScriptedLLM(_agentic_steps())
    res = asyncio.run(run_react(
        question="what explains the outcome?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=budget or BudgetState(max_calls=50),
        max_steps=4, hypotheses=hypotheses, collect_diagnostics=True))
    return res, src


# ---------------------------------------------------------------------------
# ON — the FOR/AGAINST pre-seed fires per hypothesis

def test_two_hypotheses_fire_for_and_against_each() -> None:
    """2 hypotheses → exactly a FOR-query AND an AGAINST-query corpus retrieval per hypothesis
    (4 targeted searches), and their hits land in the atom pool (diag counts + atoms_gathered)."""
    hyps = [
        Hypothesis(claim="H1 claim", for_query="h1 for", against_query="h1 against"),
        Hypothesis(claim="H2 claim", for_query="h2 for", against_query="h2 against"),
    ]
    res, src = _drive(hyps)
    # every FOR and AGAINST query was searched, before the planner's own query
    for q in ("h1 for", "h1 against", "h2 for", "h2 against"):
        assert q in src.queries, (q, src.queries)
    # 4 pre-seed corpus searches precede the single planner search leg
    assert src.queries[:4] == ["h1 for", "h1 against", "h2 for", "h2 against"], src.queries
    assert "planner original query" in src.queries         # the agentic loop STILL ran (not skipped)
    diag = res.diagnostics or {}
    intel = diag.get("intelligence")
    assert intel == {"hypotheses": 2, "for_hits": 2, "against_hits": 2}, intel
    # the pre-seed hits (>=4) are in the pool alongside the loop's own atoms
    assert res.atoms_gathered >= 4, res.atoms_gathered


def test_capped_at_three_hypotheses() -> None:
    """A 4th hypothesis is ignored — only the first 3 hypotheses' FOR/AGAINST legs fire
    (6 targeted searches); the 4th's queries never appear."""
    hyps = [
        Hypothesis(claim=f"H{i} claim", for_query=f"h{i} for", against_query=f"h{i} against")
        for i in range(1, 5)      # H1..H4
    ]
    res, src = _drive(hyps)
    for i in (1, 2, 3):
        assert f"h{i} for" in src.queries and f"h{i} against" in src.queries
    # the 4th hypothesis is capped out entirely
    assert "h4 for" not in src.queries and "h4 against" not in src.queries, src.queries
    intel = (res.diagnostics or {}).get("intelligence")
    assert intel["hypotheses"] == 3 and intel["for_hits"] == 3 and intel["against_hits"] == 3


def test_blank_against_query_skips_that_leg() -> None:
    """A hypothesis with a blank against_query → only its FOR leg fires (no crash, no against leg).
    A blank for_query falls back to the claim text."""
    hyps = [
        Hypothesis(claim="H1 claim", for_query="h1 for", against_query=""),   # no disconfirming leg
        Hypothesis(claim="H2 fallback claim", for_query="", against_query="h2 against"),  # for← claim
    ]
    res, src = _drive(hyps)
    assert "h1 for" in src.queries
    # H1's against leg was skipped — nothing blank was searched
    assert "" not in src.queries
    # H2's blank for_query fell back to its claim text; its against leg fired
    assert "H2 fallback claim" in src.queries and "h2 against" in src.queries
    intel = (res.diagnostics or {}).get("intelligence")
    # 3 legs total fired: H1-for, H2-for(=claim), H2-against
    assert intel == {"hypotheses": 2, "for_hits": 2, "against_hits": 1}, intel


# ---------------------------------------------------------------------------
# OFF — no hypotheses → no pre-seed, agentic loop unchanged (byte-identical)

def test_off_no_hypotheses_no_preseed() -> None:
    """`hypotheses=None` → the pre-seed block is skipped entirely: no `intelligence` diag key and
    the ONLY corpus query is the planner's own search — the agentic loop runs exactly as today."""
    res, src = _drive(None)
    assert "intelligence" not in (res.diagnostics or {})
    # no for/against queries — only the planner's original search reached the corpus
    assert src.queries == ["planner original query"], src.queries


# ===========================================================================
# T3 — COMPOSE: hypotheses as analytical frame + the crux register
# ===========================================================================
# Drives the REAL `run_react` all the way through compose (a verified claim exists), fakes a corpus
# source that returns ONE stable block (so a claim citing `a1` verifies), and a RecordingLLM that
# serves the loop then RECORDS every compose prompt. Asserts (a) the intelligence addendum (frame +
# hypothesis lines) is IN the compose_user when hypotheses present and ABSENT when None (OFF byte-
# identical); (b)/(c) the crux register populates from the falsifiers ON and stays empty OFF.

from roster_kernel.research.react import ComposedAnswer  # noqa: E402

_FIXED_TEXT = "Widget adoption rose sharply across the market in 2024."


class FixedSource:
    """Corpus source that returns ONE stable block for EVERY query (same doc/block id → all hits dedupe
    to a single atom `a1`), so a claim citing `a1` with a verbatim quote from `_FIXED_TEXT` verifies and
    compose runs. Records queries for parity with the pre-seed assertions."""

    key = "corpus"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def make_block_loader(self, tenant_id, workspace_id=None):
        def _load(document_id, block_id):
            return _FIXED_TEXT if block_id == "b1" else None
        return _load

    async def search(self, req):
        self.queries.append(req.query)
        return [BlockHit(document_id="d1", block_id="b1", text=_FIXED_TEXT,
                         locator=Locator("block_span", "d1", {"block_id": "b1"}),
                         document_title="doc 1")]


class RecordingComposeLLM:
    """Serves the agentic loop (a search then a grounded answer citing `a1`), then for the COMPOSE call
    (response_format=ComposedAnswer) RECORDS the user prompt and returns a valid grounded answer. The
    loop steps are popped in order; compose is identified by its response_format so it can be re-asked
    (ref-retry) any number of times without exhausting the loop script."""

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


def _drive_compose(hypotheses, *, frame=None):
    src = FixedSource()
    llm = RecordingComposeLLM()
    res = asyncio.run(run_react(
        question="what explains the outcome?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=50),
        max_steps=4, hypotheses=hypotheses, intelligence_frame=frame,
        collect_diagnostics=True))
    return res, llm


_CRUX_HYPS = [
    Hypothesis(claim="Network effects drove adoption", for_query="network effects adoption",
               against_query="adoption without network effects",
               falsifier="adoption grew fastest where the network was smallest"),
    Hypothesis(claim="Price cuts drove adoption", for_query="price cuts adoption",
               against_query="adoption despite stable price",
               falsifier="adoption rose while prices were flat"),
]


def test_intelligence_addendum_in_compose_user_when_present() -> None:
    """ON: the intelligence addendum — the frame text, the hypothesis claim + falsifier lines, and the
    'analytical FRAME the evidence TESTS' guard — is present in the compose prompt."""
    res, llm = _drive_compose(_CRUX_HYPS, frame="Adoption is driven by network effects.")
    assert res.composed_answer, "compose did not run (need a verified claim)"
    assert llm.compose_users, "no compose prompt was recorded"
    cu = llm.compose_users[0]
    assert "INTELLIGENCE FRAME" in cu
    assert "Adoption is driven by network effects." in cu               # the frame is threaded
    assert "H1: Network effects drove adoption" in cu                   # hypothesis line
    assert "adoption grew fastest where the network was smallest" in cu  # its falsifier
    assert "analytical FRAME the evidence TESTS" in cu                  # the not-a-fact guard
    assert "cites [n]" in cu                                            # facts still retrieval-authored


def test_no_intelligence_addendum_when_hypotheses_none() -> None:
    """OFF byte-identical: `hypotheses=None` → the addendum is NOT in the compose prompt and the crux
    register is empty."""
    res, llm = _drive_compose(None)
    assert llm.compose_users, "no compose prompt was recorded"
    cu = llm.compose_users[0]
    assert "INTELLIGENCE FRAME" not in cu
    assert "analytical FRAME the evidence TESTS" not in cu
    assert res.intelligence_cruxes == []


def test_cruxes_populated_from_falsifiers_when_present() -> None:
    """ON: `result.intelligence_cruxes` is exactly the hypotheses' falsifier texts (in order)."""
    res, _ = _drive_compose(_CRUX_HYPS)
    assert res.intelligence_cruxes == [
        "adoption grew fastest where the network was smallest",
        "adoption rose while prices were flat",
    ], res.intelligence_cruxes


def test_cruxes_skip_blank_falsifiers() -> None:
    """A hypothesis with a blank falsifier contributes no crux (only non-empty falsifiers register)."""
    hyps = [
        Hypothesis(claim="H1", for_query="a", against_query="b", falsifier="X would flip it"),
        Hypothesis(claim="H2", for_query="c", against_query="d", falsifier=""),   # no crux
    ]
    res, _ = _drive_compose(hyps)
    assert res.intelligence_cruxes == ["X would flip it"], res.intelligence_cruxes


def test_render_cruxes_present_and_absent() -> None:
    """The runtime render: a 'What would change this read' section from non-blank falsifiers when
    present, and "" (no section) when empty/all-blank (OFF byte-identical)."""
    from roster_kernel.runtime.research import _render_cruxes
    assert _render_cruxes([]) == ""
    assert _render_cruxes(["", "  "]) == ""            # all-blank → no section
    out = _render_cruxes(["adoption rose while prices were flat", "network was smallest"])
    assert out.startswith("## What would change this read")
    assert "- adoption rose while prices were flat" in out
    assert "- network was smallest" in out
