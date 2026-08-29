"""Offline tests for the ReAct research loop, driven by a scripted LLM."""
from __future__ import annotations

import asyncio

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


_BLOCK_TEXT = "The approved metric value was 9.8 percent for the term period."


def _source(tenant="A") -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id=tenant, text=_BLOCK_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
    ))
    return src


def _run(llm, source, *, tenant="A", budget=None, max_steps=8):
    return asyncio.run(run_react(
        question="what was the metric value?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=source,
        tenant_id=tenant, budget=budget or BudgetState(max_calls=10), max_steps=max_steps,
    ))


def test_happy_path_grounded_answer() -> None:
    llm = ScriptedLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent"),
        ]),
    ])
    res = _run(llm, _source())
    assert res.atoms_gathered == 1
    assert res.stopped_reason == "answered"
    assert res.grounded
    assert len(res.verified_claims) == 1 and not res.rejected_claims


def test_fabricated_quote_rejected() -> None:
    llm = ScriptedLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="value was 12.3", atom_id="a1", quote="the value was 12.3 percent"),
        ]),
    ])
    res = _run(llm, _source())
    assert not res.grounded
    assert res.rejected_claims[0].reason == "quote_not_grounded"


def test_unknown_atom_rejected() -> None:
    llm = ScriptedLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="x", atom_id="a99", quote="whatever"),
        ]),
    ])
    res = _run(llm, _source())
    assert res.rejected_claims[0].reason == "unknown_atom"


def test_budget_stops_the_loop() -> None:
    llm = ScriptedLLM([
        AgentStep(action="search", query="term"),
        AgentStep(action="search", query="term again"),   # never reached
    ])
    res = _run(llm, _source(), budget=BudgetState(max_calls=1))
    assert res.stopped_reason == "budget"
    assert res.steps == 1 and not res.verified_claims


def test_loop_reformulation_broadens_recall() -> None:
    # The agent issues a reformulation; multi-query fusion pulls in a block the
    # original query alone would miss.
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(block_id="b1", document_id="d", tenant_id="A",
                         text="alpha discussion of the first topic here",
                         locator=Locator("block_span", "d", {"block_id": "b1"})))
    src.add(IndexedBlock(block_id="b2", document_id="d", tenant_id="A",
                         text="beta discussion of the second topic here",
                         locator=Locator("block_span", "d", {"block_id": "b2"})))
    llm = ScriptedLLM([
        AgentStep(action="search", query="alpha", queries=["beta"]),
        AgentStep(action="answer", claims=[
            ClaimOut(text="c1", atom_id="a1", quote="alpha discussion of the first topic"),
            ClaimOut(text="c2", atom_id="a2", quote="beta discussion of the second topic"),
        ]),
    ])
    res = _run(llm, src)
    assert res.atoms_gathered == 2          # both retrieved via reformulation
    assert res.grounded and len(res.verified_claims) == 2


class _GapGating:
    def gate_applies(self, q, plan): return True                # noqa: ANN001,E704
    def claim_in_scope(self, claim, hits): return bool(hits)     # noqa: ANN001,E704
    def coverage_gap(self, q, hits): return None if hits else "no evidence in corpus"  # noqa: ANN001,E704


def test_coverage_gap_signalled_when_corpus_empty() -> None:
    src = _source()                          # has tenant-A data only
    llm = ScriptedLLM([
        AgentStep(action="search", query="nonexistent zzz topic"),   # matches nothing
        AgentStep(action="answer", claims=[]),                       # honestly refuses
    ])
    res = asyncio.run(run_react(
        question="what about a missing topic?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=src, tenant_id="A", budget=BudgetState(max_calls=10), gating=_GapGating()))
    assert res.coverage_gaps == ["no evidence in corpus"]
    assert not res.grounded                   # no claims → not grounded (honest)


def test_forced_answer_on_last_step() -> None:
    # An agent that always chooses "search" must still produce an answer on the
    # final step (forced), rather than silently returning nothing.
    src = _source()
    class _AlwaysSearch:
        def __init__(self): self.n = 0
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            if "VERIFIED FINDINGS" in messages[0]["content"]:      # compose step honors its contract
                from roster_kernel.research.react import ComposedAnswer
                return LLMResult(parsed=ComposedAnswer(answer="Metric value is 9.8 percent [1]."), output_tokens=5)
            self.n += 1
            # On the forced/last step the prompt says "MUST answer"; emit an answer then.
            if "MUST now" in messages[0]["content"]:
                return LLMResult(parsed=AgentStep(action="answer", claims=[
                    ClaimOut(text="v", atom_id="a1",
                             quote="the approved metric value was 9.8 percent")]), output_tokens=5)
            return LLMResult(parsed=AgentStep(action="search", query="term metric value"), output_tokens=5)
    res = asyncio.run(run_react(
        question="what was the metric value?", llm=_AlwaysSearch(), embedder=FakeEmbedder(dim=8),
        source=src, tenant_id="A", budget=BudgetState(max_calls=20), max_steps=3))
    assert res.stopped_reason == "answered"        # forced answer, not "max_steps"
    assert res.grounded and len(res.verified_claims) == 1


def test_forced_answer_can_honestly_refuse() -> None:
    # If forced to answer with no usable evidence, an empty-claims answer is fine
    # (honest refusal), still not a silent max_steps.
    src = _source()
    class _SearchThenEmpty:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            if "MUST now" in messages[0]["content"]:
                return LLMResult(parsed=AgentStep(action="answer", claims=[]), output_tokens=5)
            return LLMResult(parsed=AgentStep(action="search", query="x"), output_tokens=5)
    res = asyncio.run(run_react(
        question="?", llm=_SearchThenEmpty(), embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=20), max_steps=2))
    assert res.stopped_reason == "answered"
    assert not res.grounded and not res.verified_claims   # honest refusal


def test_extract_retry_recovers_empty_answer() -> None:
    # Rule 4 held-out case: the agent abstains (empty-claims answer) on its first
    # answer DESPITE having gathered relevant evidence; the recovery re-ask
    # (mode="extract") must extract the grounded claim rather than return nothing.
    src = _source()
    class _SearchEmptyThenExtract:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            content = messages[0]["content"]
            if "VERIFIED FINDINGS" in content:                    # compose step
                from roster_kernel.research.react import ComposedAnswer
                return LLMResult(parsed=ComposedAnswer(answer="Metric was 9.8 percent [1]."), output_tokens=5)
            if "empty answer is INVALID" in content:              # the extract retry
                return LLMResult(parsed=AgentStep(action="answer", claims=[
                    ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                             quote="the approved metric value was 9.8 percent")]), output_tokens=5)
            if "no evidence yet" in content:                      # first step: gather
                return LLMResult(parsed=AgentStep(action="search", query="term metric value"), output_tokens=5)
            return LLMResult(parsed=AgentStep(action="answer", claims=[]), output_tokens=5)  # abstain
    res = asyncio.run(run_react(
        question="what was the metric value?", llm=_SearchEmptyThenExtract(),
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20)))
    assert res.retried_empty                                      # the recovery fired
    assert res.grounded and len(res.verified_claims) == 1         # and it recovered a claim


def test_no_retry_when_evidence_truly_absent() -> None:
    # The recovery must NOT fire (or loop) when the agent honestly refuses with no
    # evidence gathered — otherwise a genuine no-answer would burn extra calls.
    src = _source()
    class _AnswerEmptyNoSearch:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            return LLMResult(parsed=AgentStep(action="answer", claims=[]), output_tokens=5)
    res = asyncio.run(run_react(
        question="?", llm=_AnswerEmptyNoSearch(), embedder=FakeEmbedder(dim=8),
        source=src, tenant_id="A", budget=BudgetState(max_calls=20)))
    assert not res.retried_empty and not res.grounded            # no atoms → no retry


def test_tenant_isolation_end_to_end() -> None:
    # Source has only tenant-A data; a tenant-B run retrieves nothing, so a claim
    # citing a1 is rejected as unknown (B can never reach A's evidence).
    llm = ScriptedLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="stolen", atom_id="a1", quote="the approved metric value was 9.8 percent"),
        ]),
    ])
    res = _run(llm, _source(tenant="A"), tenant="B")
    assert res.atoms_gathered == 0
    assert res.rejected_claims[0].reason == "unknown_atom"


def test_composed_answer_grounded_in_findings():
    # After gathering verified findings, the agent composes a prose answer that
    # references them [n] — grounded by construction (composer sees only findings).
    src = _source()
    class _SearchAnswerCompose:
        def __init__(self): self.calls = 0
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            self.calls += 1
            content = messages[0]["content"]
            if "VERIFIED FINDINGS" in content:            # the compose step
                from roster_kernel.research.react import ComposedAnswer
                return LLMResult(parsed=ComposedAnswer(
                    answer="The approved metric value was 9.8 percent [1]."), output_tokens=5)
            if "no evidence yet" in content or self.calls == 1:
                return LLMResult(parsed=AgentStep(action="search", query="term metric value"), output_tokens=5)
            return LLMResult(parsed=AgentStep(action="answer", claims=[
                ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                         quote="the approved metric value was 9.8 percent")]), output_tokens=5)
    res = asyncio.run(run_react(
        question="what was the metric value?", llm=_SearchAnswerCompose(),
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20)))
    assert res.grounded and len(res.verified_claims) == 1
    assert res.composed_answer == "The approved metric value was 9.8 percent [1]."
    assert "[1]" in res.composed_answer                  # references the finding


def _compose_setup():
    """A source + a fake whose loop grounds ONE claim; compose behavior is injected per-test."""
    src = _source()
    def make(compose_fn):
        class _LLM:
            def __init__(self): self.compose_calls = 0
            async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
                if "VERIFIED FINDINGS" in messages[0]["content"]:     # compose step
                    self.compose_calls += 1
                    return compose_fn(self.compose_calls)
                if "no evidence yet" in messages[0]["content"]:
                    return LLMResult(parsed=AgentStep(action="search", query="term metric value"), output_tokens=5)
                return LLMResult(parsed=AgentStep(action="answer", claims=[
                    ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                             quote="the approved metric value was 9.8 percent")]), output_tokens=5)
        return _LLM()
    return src, make


def test_compose_retries_transient_failure(monkeypatch):
    # REGRESSION (the 'grounded, N claims, empty answer' bug): a transient error on the compose call
    # must NOT drop the answer — it retries and recovers.
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)     # no real sleeping in the test
    from roster_kernel.research.react import ComposedAnswer
    src, make = _compose_setup()
    def compose_fn(call_n):
        if call_n == 1:
            raise RuntimeError("transient overload")          # first attempt fails…
        return LLMResult(parsed=ComposedAnswer(answer="Metric value is 9.8 percent [1]."), output_tokens=5)
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A", budget=BudgetState(max_calls=20)))
    assert res.grounded and len(res.verified_claims) == 1
    assert res.composed_answer == "Metric value is 9.8 percent [1]."   # recovered on retry
    assert res.compose_failed is False
    assert llm.compose_calls == 2                              # exactly one retry


def test_compose_failure_surfaces_note(monkeypatch):
    # If compose can NEVER complete, the failure is SURFACED (a note), not a silent blank — and the
    # verified evidence still stands (grounded, claims intact).
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    src, make = _compose_setup()
    def compose_fn(call_n):
        raise RuntimeError("persistent outage")
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A", budget=BudgetState(max_calls=20)))
    assert res.grounded and len(res.verified_claims) == 1     # evidence survives the compose failure
    assert res.compose_failed is True
    assert res.composed_answer and res.composed_answer == react._COMPOSE_FAIL_NOTE   # not empty
    assert llm.compose_calls == react._COMPOSE_ATTEMPTS       # exhausted the retries


def test_rank_claims_by_relevance_keeps_the_most_relevant():
    # Evidence selection (#2): under the flag, compose gets the claims most RELEVANT to the question,
    # not the first-come ones. Deterministic FakeEmbedder encodes relevance in a 2-d vector.
    from roster_kernel.research.react import _rank_claims_by_relevance, VerifiedClaim
    class _FE:
        def dim(self): return 2
        def embed(self, texts): return [[1.0, 0.0] if "relevant" in t else [0.0, 1.0] for t in texts]
    claims = [VerifiedClaim("off-topic A", "a1", "q"), VerifiedClaim("relevant B", "a2", "q"),
              VerifiedClaim("off-topic C", "a3", "q"), VerifiedClaim("relevant D", "a4", "q")]
    top2 = asyncio.run(_rank_claims_by_relevance("the relevant question", claims, _FE(), 2))
    assert [c.text for c in top2] == ["relevant B", "relevant D"]   # relevant win; stable among ties
    # already within cap → returned unchanged (no reorder)
    assert asyncio.run(_rank_claims_by_relevance("x", claims[:2], _FE(), 5)) == claims[:2]


def test_rank_claims_recency_breaks_controlling_tier_ties_only():
    # Recency term: among equally-relevant CONTROLLING-tier claims (guideline rank 6), the newer year
    # wins the cap cut; lower tiers get NO recency term (a landmark RCT never loses to a newer small
    # trial via recency), and an unknown year is a no-op.
    import datetime
    from roster_kernel.research.react import _rank_claims_by_relevance, VerifiedClaim
    class _FE:
        def dim(self): return 2
        def embed(self, texts): return [[1.0, 0.0] for _ in texts]     # everything equally relevant
    now = datetime.date.today().year
    ranker = lambda kind: {"guideline": 6, "rct": 5}.get(kind, 0)
    old_g = VerifiedClaim("old guideline", "a1", "q", facets={"year": str(now - 10)}, evidence_kind="guideline")
    new_g = VerifiedClaim("new guideline", "a2", "q", facets={"year": str(now)}, evidence_kind="guideline")
    top1 = asyncio.run(_rank_claims_by_relevance("q", [old_g, new_g], _FE(), 1, evidence_ranker=ranker))
    assert top1[0].text == "new guideline"                      # newest controlling guidance governs
    # tier still dominates recency: an old guideline (rank 6) beats a brand-new RCT (rank 5)
    new_rct = VerifiedClaim("new rct", "a3", "q", facets={"year": str(now)}, evidence_kind="rct")
    top1b = asyncio.run(_rank_claims_by_relevance("q", [new_rct, old_g], _FE(), 1, evidence_ranker=ranker))
    assert top1b[0].text == "old guideline"
    # no ranker (evidence-fitness off) → recency inert, original order kept
    top1c = asyncio.run(_rank_claims_by_relevance("q", [old_g, new_g], _FE(), 1))
    assert top1c[0].text == "old guideline"


def test_zero_evidence_answer_converts_to_search():
    # The 'Analyze this report' failure: rich attachment context convinced the planner to answer
    # IMMEDIATELY (zero searches → zero atoms → nothing groundable). The guard must first bounce
    # the premature answer back to the planner with a note, then (on repeat) force a search — the
    # run ends GROUNDED instead of empty.
    from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer
    src = _source()
    calls = {"n": 0}
    class _LLM:
        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            calls["n"] += 1
            content = messages[0]["content"]
            if "VERIFIED FINDINGS" in content:      # compose
                return LLMResult(parsed=ComposedAnswer(answer="Metric value is 9.8 percent [1]."), output_tokens=5)
            if "no retrieved evidence" in content.lower() or "evidence is" in content.lower():
                # planner sees the guard note → NOW it searches properly
                if "the approved metric value" not in content:
                    return LLMResult(parsed=AgentStep(action="search", query="term metric value"), output_tokens=5)
            if "term metric value" in content or "approved" in content:
                # evidence landed → extract claims
                return LLMResult(parsed=AgentStep(action="answer", claims=[
                    ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                             quote="the approved metric value was 9.8 percent")]), output_tokens=5)
            # FIRST planner step: premature answer with zero evidence (the failure mode)
            return LLMResult(parsed=AgentStep(action="answer", claims=[
                ClaimOut(text="made up from context", atom_id="a1", quote="not in any source")]), output_tokens=5)
    res = asyncio.run(run_react(question="Analyze this report.", llm=_LLM(),
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20),
        attachment_context="DOCUMENT — cbc.pdf (structured digest): Hemoglobin: 9.4 g/dL LOW"))
    assert res.grounded and len(res.verified_claims) == 1     # recovered: searched, grounded
    assert res.composed_answer.startswith("Metric value")


def test_rank_claims_superseded_partitioned_below_current():
    # Evidence Pulse C1 (spec A3): superseded/retracted-source claims sort BELOW current ones as a
    # hard partition — unconditionally, including the <=top early-return path that skips scoring.
    from roster_kernel.research.react import _rank_claims_by_relevance, VerifiedClaim
    class _FE:
        def dim(self): return 2
        def embed(self, texts): return [[1.0, 0.0] for _ in texts]     # equal relevance everywhere
    old = VerifiedClaim("from 2012 edition", "a1", "q", facets={"superseded_by": "g:kdigo-2026"})
    new = VerifiedClaim("from 2026 edition", "a2", "q", facets={})
    # scoring path (claims > top): current wins the cap
    top1 = asyncio.run(_rank_claims_by_relevance("q", [old, new], _FE(), 1))
    assert top1[0].text == "from 2026 edition"
    # early-return path (claims <= top): partition still applies — current FIRST in compose order
    both = asyncio.run(_rank_claims_by_relevance("q", [old, new], _FE(), 5))
    assert [c.text for c in both] == ["from 2026 edition", "from 2012 edition"]


def test_reasoning_conclusion_repaired_when_guard_blanks_it(monkeypatch):
    # Robustness (the "Informed judgment randomly absent" bug): a conclusion stating a figure OUTSIDE
    # the allowance (findings + answer) is blanked by the guard — the repair pass restates it
    # qualitatively and it SURVIVES. Guard stays authoritative throughout.
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    from roster_kernel.research.react import (ComposedAnswer, ConfidenceRead, ConfidenceDim)
    src, make = _compose_setup()
    def compose_fn(call_n):
        return LLMResult(parsed=ComposedAnswer(
            answer="The approved metric value was 9.8 percent [1].",
            reasoning_purpose="Whether the metric value can be relied on.",
            # "3.4 percent" appears in NEITHER the finding NOR the answer → guard blanks this
            reasoning_conclusion="The value implies a 3.4 percent shortfall, so act cautiously.",
            confidence=ConfidenceRead(
                factual=ConfidenceDim(level="low", rationale="single source"),
                causal=ConfidenceDim(level="unknown", rationale=""),
                generalization=ConfidenceDim(level="low", rationale="one period"))),
            output_tokens=5)
    llm = make(compose_fn)
    _orig = llm.complete
    async def _complete(*, system, messages, response_format, max_tokens=2048, temperature=None):
        if "Restate these reasoning-frame fields" in (system or ""):     # the repair call
            class _Fix:
                reasoning_purpose = "Whether the metric value can be relied on."
                reasoning_conclusion = "The value implies a meaningful shortfall, so act cautiously."
            return LLMResult(parsed=_Fix(), output_tokens=3)
        return await _orig(system=system, messages=messages,
                           response_format=response_format, max_tokens=max_tokens, temperature=temperature)
    llm.complete = _complete
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20), reasoning_read=True))
    assert res.grounded
    assert res.reasoning_conclusion == "The value implies a meaningful shortfall, so act cautiously."
    assert res.reasoning_purpose                                # untouched frame survives as-is


def test_reasoning_read_flows_through_when_enabled(monkeypatch):
    # End-to-end: with reasoning_read=True, a compose that emits a GROUNDED interpretation item +
    # confidence surfaces both on the result (validated). The basis finding [1] is "9.8 percent".
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    from roster_kernel.research.react import (
        ComposedAnswer, InterpretationItem, ConfidenceRead, ConfidenceDim)
    src, make = _compose_setup()
    def compose_fn(call_n):
        return LLMResult(parsed=ComposedAnswer(
            answer="The approved metric value was 9.8 percent [1].",
            reasoning_purpose="Whether the metric value is reliable enough to act on.",
            interpretation=[
                InterpretationItem(text="Only one source reports this, limiting confidence",
                                   kind="gap", basis_findings=[1]),
                InterpretationItem(text="A value near 12 percent is possible",   # 12 not in finding → dropped
                                   kind="implication", basis_findings=[1])],
            reasoning_conclusion="The 9.8 percent figure is the only supported value, so it can be used cautiously.",
            confidence=ConfidenceRead(
                factual=ConfidenceDim(level="low", rationale="single source"),
                causal=ConfidenceDim(level="unknown", rationale=""),
                generalization=ConfidenceDim(level="low", rationale="one term period"))),
            output_tokens=5)
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20), reasoning_read=True))
    assert res.grounded
    # the fabricated-number item is dropped; the grounded gap item survives
    assert len(res.interpretation) == 1 and res.interpretation[0]["kind"] == "gap"
    assert res.confidence and res.confidence["factual"]["level"] == "low"
    # purpose (no numbers) passes; conclusion reuses only the grounded 9.8 → both survive
    assert res.reasoning_purpose.startswith("Whether the metric value")
    assert "9.8 percent" in res.reasoning_conclusion


def test_reasoning_conclusion_with_fabricated_number_is_dropped(monkeypatch):
    # The purpose/conclusion frame is grounded too: a conclusion inventing a figure not in ANY finding
    # is dropped (fail-safe), while a clean purpose survives.
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    from roster_kernel.research.react import ComposedAnswer
    src, make = _compose_setup()
    def compose_fn(call_n):
        return LLMResult(parsed=ComposedAnswer(
            answer="The approved metric value was 9.8 percent [1].",
            reasoning_purpose="Whether the value can be trusted.",
            reasoning_conclusion="The true value is likely closer to 15 percent."),  # 15 in no finding
            output_tokens=5)
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20), reasoning_read=True))
    assert res.reasoning_purpose == "Whether the value can be trusted."   # clean → survives
    assert res.reasoning_conclusion == ""                                 # fabricated figure → dropped


def test_reasoning_retry_grafts_missing_reasoning(monkeypatch):
    # Reliability: the first compose writes the answer but SKIPS the reasoning fields (the Q2 bug);
    # a retry supplies them and they're grafted onto the existing answer.
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    from roster_kernel.research.react import (
        ComposedAnswer, InterpretationItem, ConfidenceRead, ConfidenceDim)
    src, make = _compose_setup()
    def compose_fn(call_n):
        if call_n == 1:   # first compose: good answer, NO reasoning (the failure mode)
            return LLMResult(parsed=ComposedAnswer(answer="Metric value is 9.8 percent [1]."), output_tokens=5)
        return LLMResult(parsed=ComposedAnswer(   # retry: supplies reasoning
            answer="Metric value is 9.8 percent [1].",
            reasoning_purpose="Whether the value is reliable.",
            interpretation=[InterpretationItem(text="single source", kind="gap", basis_findings=[1])],
            confidence=ConfidenceRead(factual=ConfidenceDim(level="low"))), output_tokens=5)
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20), reasoning_read=True))
    assert res.composed_answer == "Metric value is 9.8 percent [1]."   # answer preserved
    assert len(res.interpretation) == 1 and res.confidence   # reasoning grafted from the retry
    assert llm.compose_calls == 2


def test_reasoning_read_off_is_noop(monkeypatch):
    # OFF path (default): even if the model volunteers interpretation/confidence, the result surfaces
    # NEITHER — byte-identical to the pre-flag answer (Rule 20).
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    from roster_kernel.research.react import (
        ComposedAnswer, InterpretationItem, ConfidenceRead, ConfidenceDim)
    src, make = _compose_setup()
    def compose_fn(call_n):
        return LLMResult(parsed=ComposedAnswer(
            answer="The approved metric value was 9.8 percent [1].",
            reasoning_purpose="a purpose", reasoning_conclusion="a conclusion",
            interpretation=[InterpretationItem(text="grounded gap", kind="gap", basis_findings=[1])],
            confidence=ConfidenceRead(factual=ConfidenceDim(level="high"))), output_tokens=5)
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20)))              # reasoning_read defaults False
    assert res.grounded
    assert res.interpretation == [] and res.confidence is None
    assert res.reasoning_purpose == "" and res.reasoning_conclusion == ""


def test_diagnostics_trace_captures_turns_tools_and_funnel(monkeypatch):
    # collect_diagnostics=True builds a troubleshooting trace: per-turn steps, tool-call breakdown,
    # the grounding funnel, retries, and timing — with NO extra model calls.
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    from roster_kernel.research.react import ComposedAnswer
    src, make = _compose_setup()
    def compose_fn(call_n):
        return LLMResult(parsed=ComposedAnswer(answer="Metric value is 9.8 percent [1]."), output_tokens=5)
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A",
        budget=BudgetState(max_calls=20), collect_diagnostics=True))
    d = res.diagnostics
    assert d is not None
    # trace has a search turn then an answer turn
    actions = [t["action"] for t in d["trace"]]
    assert "search" in actions and "answer" in actions
    # funnel reflects the one grounded finding
    assert d["funnel"]["verified"] == 1 and d["funnel"]["atoms_gathered"] >= 1
    # tool-call breakdown + budget are present
    assert d["tool_calls"]["compose_calls"] == 1 and d["tool_calls"]["searches"] >= 1
    assert d["budget"]["llm_calls"] == budget_calls(res) and d["budget"]["max_calls"] == 20
    assert d["stopped_reason"] == "answered" and "duration_ms" in d


def budget_calls(res):
    # planner steps + 1 compose (the fixture: 1 search + 1 answer step + 1 compose = 3)
    return res.steps + 1


def test_diagnostics_off_is_none():
    # Default (flag off): no trace captured — byte-identical.
    src, make = _compose_setup()
    from roster_kernel.research.react import ComposedAnswer
    llm = make(lambda n: LLMResult(parsed=ComposedAnswer(answer="x [1]."), output_tokens=5))
    res = asyncio.run(run_react(question="q?", llm=llm, embedder=FakeEmbedder(dim=8),
        source=src, tenant_id="A", budget=BudgetState(max_calls=20)))
    assert res.diagnostics is None


def test_evidence_fitness_boosts_stronger_tier_on_ties():
    # Two equally-relevant claims (identical embeddings); the guideline tier is boosted above the case
    # report into the top-1 cap. Boost-only: without a ranker, order is preserved.
    from roster_kernel.research.react import _rank_claims_by_relevance, VerifiedClaim
    class _FE:
        def dim(self): return 2
        def embed(self, texts): return [[1.0, 0.0] for _ in texts]   # everything identical → pure tie
    weak = VerifiedClaim("weak", "a1", "q", evidence_kind="case_report")
    strong = VerifiedClaim("strong", "a2", "q", evidence_kind="guideline")
    claims = [weak, strong]                                  # weak first
    rank = {"case_report": 1, "guideline": 6}.get
    top1 = asyncio.run(_rank_claims_by_relevance("q", claims, _FE(), 1, evidence_ranker=lambda k: rank(k, 0)))
    assert top1[0].evidence_kind == "guideline"             # stronger tier won the tie
    # no ranker → boost is a no-op → the relevance order (stable) is kept
    noboost = asyncio.run(_rank_claims_by_relevance("q", claims, _FE(), 1))
    assert noboost[0].evidence_kind == "case_report"


def test_prose_hard_token_scan_flags_unsupported_figure():
    from roster_kernel.research.react import _unsupported_prose_tokens, VerifiedClaim
    v = [VerifiedClaim("response was 53%", "a1", "response was 53%")]
    assert _unsupported_prose_tokens("The response rate was 53%.", v) == set()      # supported
    assert "70" in _unsupported_prose_tokens("Response could reach 70%.", v)        # fabricated in prose
    assert _unsupported_prose_tokens("", v) == set()                                # no answer → nothing


def test_evidence_fitness_reported_in_diagnostics(monkeypatch):
    # With fitness + diagnostics on, the trace reports the evidence-tier histogram and the prose scan.
    import roster_kernel.research.react as react
    monkeypatch.setattr(react, "_COMPOSE_BACKOFF_S", 0)
    from roster_kernel.research.react import ComposedAnswer
    src, make = _compose_setup()
    def compose_fn(call_n):
        return LLMResult(parsed=ComposedAnswer(answer="Metric value is 9.8 percent [1]."), output_tokens=5)
    llm = make(compose_fn)
    res = asyncio.run(run_react(question="what was the metric value?", llm=llm,
        embedder=FakeEmbedder(dim=8), source=src, tenant_id="A", budget=BudgetState(max_calls=20),
        collect_diagnostics=True, classify_evidence=lambda sk, f, t=None, x=None: "rct",
        evidence_fitness=True, evidence_ranker=lambda k: 5 if k == "rct" else 0))
    assert res.grounded
    assert res.diagnostics["evidence_tiers"].get("rct") == 1
    assert res.diagnostics["prose_unsupported_tokens"] == []   # 9.8 is in the finding


def test_rank_claims_fail_safe_on_embed_error():
    # Any embedding failure must degrade to today's behavior (first `top`), never crash the answer.
    from roster_kernel.research.react import _rank_claims_by_relevance, VerifiedClaim
    class _Bad:
        def dim(self): return 2
        def embed(self, texts): raise RuntimeError("embed down")
    claims = [VerifiedClaim(f"c{i}", f"a{i}", "q") for i in range(5)]
    got = asyncio.run(_rank_claims_by_relevance("x", claims, _Bad(), 2))
    assert got == claims[:2]


def test_strip_control_tags_removes_leaked_serialization():
    """A completion that bleeds the tool-call/structured-output tags into the answer string must be
    truncated at the first control tag — the real answer precedes it (the specialist-review leak)."""
    from roster_kernel.research.react import strip_control_tags as s
    leak = ("Bottom line: The stepwise plan is well aligned [1][2].</answer> "
            "<directly_addresses>true</directly_addresses> <gap_note></gap_note> </invoke>")
    assert s(leak) == "Bottom line: The stepwise plan is well aligned [1][2]."
    # clean answers are untouched (byte-identical, no-op)
    clean = "Use metformin first-line [1]. Titrate to eGFR [2]."
    assert s(clean) == clean
    # a legitimate less-than is NOT a control tag
    assert s("Give if CrCl < 30 mL/min [1].") == "Give if CrCl < 30 mL/min [1]."
    assert s("") == ""


def test_country_boost_surfaces_region_evidence():
    """A country_boost lifts region-tagged findings into the compose cap WITHOUT filtering — an equally
    relevant IN-tagged finding should win a tie over a non-tagged one when {"IN"} is boosted."""
    import asyncio
    from roster_kernel.research.react import _rank_claims_by_relevance, VerifiedClaim
    from roster_kernel.providers.embeddings import FakeEmbedder

    def _c(text, country=""):
        return VerifiedClaim(text=text, quote=text, atom_id=text, source_key="s",
                             document_title="", document_id="", facets={"source_country": country})
    # identical text → identical cosine to the question; the boost is the only differentiator
    claims = [_c("dengue management", ""), _c("dengue management", "IN")]
    emb = FakeEmbedder(dim=8)
    top1 = asyncio.run(_rank_claims_by_relevance("dengue management", claims, emb, 1, country_boost={"IN"}))
    assert top1[0].facets.get("source_country") == "IN"          # boosted IN finding surfaces
    # no boost → order unchanged (byte-identical), IN not preferred
    top1_noboost = asyncio.run(_rank_claims_by_relevance("dengue management", claims, emb, 1))
    assert top1_noboost[0].facets.get("source_country") == ""    # first-in-order kept
