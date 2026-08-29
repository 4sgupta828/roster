"""Offline tests for the parametric-led DIRECTED VERIFY LOOP (ROSTER_PARAMETRIC_LED, T2).

Drives the REAL `run_react` with a `prior_draft` present. Fakes:
- InMemoryRetrievalSource (a REAL span-gate via its block loader) holding controlled blocks;
- a RoutingLLM keyed on the requested `response_format` — it answers the directed grounder
  (`_GroundVerdict`) via an injected `ground_fn(user_content)->dict` and the composer
  (`ComposedAnswer`) with a benign, number-free answer so the prose-audit never retries.

Covers: a fact whose retrieved atom carries an entailing verbatim quote → VerifiedClaim (cited);
a fact with only a tangential quote (span-gate rejects) → unverified_priors; a grounder abstention
({}) → unverified; a needs_freshness fact with no grounding → unverified; a grounder LLM error →
unverified (fail-closed); reasoning-kind claims are NOT verified. Plus: OFF (prior_draft=None) runs
the agentic loop byte-identically (reuses the ScriptedLLM harness).
"""
from __future__ import annotations

import asyncio

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.prior_draft import AssertedClaim, PriorDraft
from roster_kernel.research.react import AgentStep, ClaimOut, run_react
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


_BLOCK_TEXT = "Acme raised a $50 million Series B round led by a growth fund in 2024."


def _source(tenant="A") -> InMemoryRetrievalSource:
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id=tenant, text=_BLOCK_TEXT,
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
        document_title="Acme funding note", source_key="corpus",
    ))
    return src


class RoutingLLM:
    """One fake serving every run_react call site, dispatched by `response_format.__name__`.

    - `_GroundVerdict` (the directed grounder): calls the injected `ground_fn(user_content)` →
      a dict {"atom_id","quote"} or {} — and counts the invocation.
    - `ComposedAnswer` (the composer): a benign, number-free answer so the prose-audit never fires.
    - anything else: raises (no other call site is exercised by these tests).
    """

    def __init__(self, ground_fn, compose_fn=None):
        self._ground_fn = ground_fn
        self._compose_fn = compose_fn      # (content, call_idx)->answer str; default = benign number-free
        self.ground_calls = 0
        self.compose_calls = 0
        self.grounded_claims: list[str] = []
        self.compose_contents: list[str] = []   # captured compose_user prompts (addendum-presence assert)

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        name = getattr(response_format, "__name__", "")
        if name == "_GroundVerdict":
            self.ground_calls += 1
            content = messages[0]["content"]
            self.grounded_claims.append(content)
            verdict = self._ground_fn(content) or {}
            return LLMResult(parsed=response_format(**verdict), output_tokens=3, model="fake")
        if name == "ComposedAnswer":
            content = messages[0]["content"]
            self.compose_contents.append(content)
            idx = self.compose_calls
            self.compose_calls += 1
            answer = (self._compose_fn(content, idx) if self._compose_fn
                      else "The evidence supports the finding. [1]")
            return LLMResult(parsed=response_format(answer=answer), output_tokens=3, model="fake")
        raise AssertionError(f"unexpected response_format {name!r} in parametric verify test")


class RaisingGroundLLM(RoutingLLM):
    """RoutingLLM whose directed-grounder call RAISES — exercises the grounder's fail-closed path."""
    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        if getattr(response_format, "__name__", "") == "_GroundVerdict":
            self.ground_calls += 1
            raise RuntimeError("grounder model exploded")
        return await super().complete(system=system, messages=messages,
                                      response_format=response_format, max_tokens=max_tokens)


def _run_parametric(llm, source, draft, *, tenant="A", budget=None):
    return asyncio.run(run_react(
        question="how did Acme fund itself?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=source,
        tenant_id=tenant, budget=budget or BudgetState(max_calls=50),
        prior_draft=draft,
    ))


# ---------------------------------------------------------------------------
# T2 verify loop

def test_grounded_fact_becomes_verified_claim() -> None:
    """A drafted fact whose retrieved atom carries an entailing verbatim quote → a cited
    VerifiedClaim (indistinguishable from today's findings), and NOT an unverified prior."""
    quote = "Acme raised a $50 million Series B round"        # verbatim contiguous substring
    llm = RoutingLLM(lambda _c: {"atom_id": "a1", "quote": quote})
    draft = PriorDraft(outline="funding history", claims=[
        AssertedClaim(text="Acme raised a $50 million Series B",
                      kind="fact", verify_query="Acme Series B round"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert llm.ground_calls == 1
    assert len(res.verified_claims) == 1
    vc = res.verified_claims[0]
    assert vc.document_id == "d1" and vc.atom_id == "a1"
    assert vc.quote == quote                                   # the span-verified quote is carried
    assert res.unverified_priors == []
    assert res.stopped_reason == "answered"


def test_tangential_quote_fails_span_gate_unverified() -> None:
    """A grounder that returns a quote NOT present in the cited block is rejected by the UNTOUCHED
    span-gate → the fact lands in unverified_priors, never verified_claims (no laundering)."""
    llm = RoutingLLM(lambda _c: {"atom_id": "a1", "quote": "Acme raised a $99 million Series C"})
    draft = PriorDraft(claims=[
        AssertedClaim(text="Acme raised a Series C", kind="fact", verify_query="Acme funding"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert res.verified_claims == []
    assert len(res.unverified_priors) == 1
    assert res.unverified_priors[0]["text"] == "Acme raised a Series C"


def test_grounder_abstains_unverified() -> None:
    """Grounder returns {} (no atom UNEQUIVOCALLY proves the claim) → unverified, never grounded."""
    llm = RoutingLLM(lambda _c: {})
    draft = PriorDraft(claims=[
        AssertedClaim(text="Acme is profitable", kind="fact", verify_query="Acme profitability"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert res.verified_claims == []
    assert [p["text"] for p in res.unverified_priors] == ["Acme is profitable"]


def test_needs_freshness_without_grounding_stays_unverified() -> None:
    """A needs_freshness fact that fails to ground is NEVER shipped from the prior alone — it lands
    in unverified_priors carrying its freshness flag (retrieval is the only path to verified)."""
    llm = RoutingLLM(lambda _c: {})
    draft = PriorDraft(claims=[
        AssertedClaim(text="Acme's current valuation is $1B", kind="fact",
                      needs_freshness=True, verify_query="Acme valuation 2026"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert res.verified_claims == []
    assert len(res.unverified_priors) == 1
    assert res.unverified_priors[0]["needs_freshness"] is True


def test_grounder_llm_error_fail_closed() -> None:
    """The directed grounder's LLM call raising → the claim stays unverified (fail-closed), never
    grounded on a broken judge."""
    llm = RaisingGroundLLM(lambda _c: {})
    draft = PriorDraft(claims=[
        AssertedClaim(text="Acme raised a $50 million Series B", kind="fact",
                      verify_query="Acme Series B"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert llm.ground_calls == 1
    assert res.verified_claims == []
    assert len(res.unverified_priors) == 1


def test_reasoning_claims_are_not_verified() -> None:
    """Only kind=='fact' claims are verified; a kind=='reasoning' claim is skipped entirely — it is
    NOT retrieved for, NOT grounded, and appears in NEITHER verified_claims nor unverified_priors."""
    quote = "Acme raised a $50 million Series B round"
    llm = RoutingLLM(lambda _c: {"atom_id": "a1", "quote": quote})
    draft = PriorDraft(claims=[
        AssertedClaim(text="Acme raised a $50 million Series B", kind="fact",
                      verify_query="Acme Series B"),
        AssertedClaim(text="This suggests strong investor confidence", kind="reasoning"),
    ])
    res = _run_parametric(llm, _source(), draft)
    # exactly ONE grounder call — the reasoning claim never triggered retrieval/grounding
    assert llm.ground_calls == 1
    assert len(res.verified_claims) == 1
    texts = [vc.text for vc in res.verified_claims] + [p["text"] for p in res.unverified_priors]
    assert "This suggests strong investor confidence" not in texts


# ---------------------------------------------------------------------------
# OFF path — prior_draft is None runs the agentic loop unchanged

class ScriptedLLM:
    """Returns pre-scripted AgentStep/ComposedAnswer objects in order (ignores the prompt)."""
    def __init__(self, steps):
        self._steps = list(steps)

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        return LLMResult(parsed=self._steps.pop(0), output_tokens=5, model="scripted")


def test_off_path_runs_agentic_loop_unchanged() -> None:
    """prior_draft=None → the directed verify loop is skipped and the agentic search→answer loop
    runs exactly as today: a search step then an answer whose quote passes the span-gate grounds,
    and unverified_priors is never populated on the OFF path."""
    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A",
        text="The approved metric value was 9.8 percent for the term period.",
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
    ))
    llm = ScriptedLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent"),
        ]),
    ])
    res = asyncio.run(run_react(
        question="what was the metric value?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=10),
        prior_draft=None,
    ))
    assert res.stopped_reason == "answered"
    assert res.grounded and len(res.verified_claims) == 1
    assert res.unverified_priors == []


# ---------------------------------------------------------------------------
# T3 — compose from the outline, the unverified register, the corrective prose-audit

from roster_kernel.research.react import _PARAMETRIC_ADDENDUM
from roster_kernel.runtime.research import _render_unverified_priors


def test_parametric_addendum_with_outline_in_compose_prompt() -> None:
    """When prior_draft is present, the compose prompt carries the _PARAMETRIC_ADDENDUM with the
    drafted OUTLINE text interpolated — so the answer is steered to follow the model's structure."""
    quote = "Acme raised a $50 million Series B round"
    llm = RoutingLLM(lambda _c: {"atom_id": "a1", "quote": quote})
    draft = PriorDraft(outline="1) origins  2) the Series B  3) trajectory", claims=[
        AssertedClaim(text="Acme raised a $50 million Series B", kind="fact",
                      verify_query="Acme Series B"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert res.verified_claims, "need a compose to have run"
    joined = "\n".join(llm.compose_contents)
    assert "Structure the answer to follow this OUTLINE" in joined
    assert "1) origins  2) the Series B  3) trajectory" in joined     # the outline is interpolated


def test_off_path_compose_has_no_parametric_addendum() -> None:
    """OFF (prior_draft=None): the compose prompt is byte-identical to today — the parametric addendum
    is never appended, so its sentinel text is absent from the composer prompt."""
    class CapturingScriptedLLM(ScriptedLLM):
        def __init__(self, steps):
            super().__init__(steps)
            self.compose_contents: list[str] = []

        async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
            if getattr(response_format, "__name__", "") == "ComposedAnswer":
                self.compose_contents.append(messages[0]["content"])
            return await super().complete(system=system, messages=messages,
                                          response_format=response_format, max_tokens=max_tokens)

    src = InMemoryRetrievalSource()
    src.add(IndexedBlock(
        block_id="b1", document_id="d1", tenant_id="A",
        text="The approved metric value was 9.8 percent for the term period.",
        locator=Locator("block_span", "d1", {"block_id": "b1"}),
    ))
    llm = CapturingScriptedLLM([
        AgentStep(action="search", query="term metric value"),
        AgentStep(action="answer", claims=[
            ClaimOut(text="the metric value was 9.8 percent", atom_id="a1",
                     quote="the approved metric value was 9.8 percent"),
        ]),
        # the compose call (ComposedAnswer) — a number-free answer so no ref/audit retries fire
        __import__("roster_kernel.research.react", fromlist=["ComposedAnswer"]).ComposedAnswer(
            answer="The metric value is supported. [1]"),
    ])
    asyncio.run(run_react(
        question="what was the metric value?",
        llm=llm, embedder=FakeEmbedder(dim=8), source=src,
        tenant_id="A", budget=BudgetState(max_calls=10), prior_draft=None,
    ))
    assert llm.compose_contents, "compose should have run on the OFF path"
    joined = "\n".join(llm.compose_contents)
    assert "Structure the answer to follow this OUTLINE" not in joined
    assert _PARAMETRIC_ADDENDUM.split("<OUTLINE>")[0] not in joined


def test_render_unverified_priors_labeled_section() -> None:
    """_render_unverified_priors produces a clearly-labeled section for non-empty priors (with a
    '(may be outdated)' marker on needs_freshness ones) and "" for empty — never merged into prose."""
    section = _render_unverified_priors([
        {"text": "Acme is profitable", "needs_freshness": False},
        {"text": "Acme's current valuation is $1B", "needs_freshness": True},
    ])
    assert section.startswith("## Model's read — not yet verified")
    assert "not yet verified" in section
    assert "- Acme is profitable" in section
    assert "- Acme's current valuation is $1B _(may be outdated)_" in section
    # empty / all-blank → no section
    assert _render_unverified_priors([]) == ""
    assert _render_unverified_priors([{"text": "  ", "needs_freshness": False}]) == ""


def test_corrective_prose_audit_fires_in_parametric_mode() -> None:
    """The corrective prose-audit (widened to fire when prior_draft is present) catches a figure the
    model wrote into the prose that is NOT in the verified findings: it recomposes ONCE (a 2nd compose
    call) and the offending figure is dropped from the shipped answer — no laundering into prose."""
    quote = "Acme raised a $50 million Series B round"

    def compose_fn(_content, idx):
        # 1st compose: smuggles an unsupported figure (42%) into prose; recompose: clean, number-free.
        return "Acme grew 42% last year. [1]" if idx == 0 else "The evidence supports the finding. [1]"

    llm = RoutingLLM(lambda _c: {"atom_id": "a1", "quote": quote}, compose_fn=compose_fn)
    draft = PriorDraft(outline="growth", claims=[
        AssertedClaim(text="Acme raised a $50 million Series B", kind="fact",
                      verify_query="Acme Series B"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert res.verified_claims, "need a grounded finding so compose runs"
    assert llm.compose_calls >= 2, "the corrective audit must have recomposed once"
    assert "42%" not in res.composed_answer     # the unsupported figure was audited out of grounded prose


def test_unverified_register_appended_and_priors_never_in_verified() -> None:
    """End-to-end via the ResearchService compose wrapper is covered elsewhere; here assert the
    invariant at the render seam: an unverified prior renders in the labeled register only, and the
    grounded verified_claims never contain it (no laundering)."""
    llm = RoutingLLM(lambda _c: {})     # grounder abstains → the fact stays unverified
    draft = PriorDraft(outline="funding", claims=[
        AssertedClaim(text="Acme is profitable", kind="fact", verify_query="Acme profitability"),
    ])
    res = _run_parametric(llm, _source(), draft)
    assert res.verified_claims == []
    assert [p["text"] for p in res.unverified_priors] == ["Acme is profitable"]
    section = _render_unverified_priors(res.unverified_priors)
    assert "Acme is profitable" in section
    # the prior is NOT in any grounded claim text
    assert all("Acme is profitable" != vc.text for vc in res.verified_claims)
