"""Panel upgrade (P1+P2+P3) — offline scripted tests mirroring test_panel.py.

P2 (ROSTER_PANEL_DEDUP): pooled-claim dedup by (atom_id, normalized quote) + computed-convergence
annotation. P3 (ROSTER_PANEL_CONTRACT): ONE shared contract derived before the specialists (never
per specialist), scoped 'Ensure coverage of' lens lines, pooled slot-matching (entities /
exploratory axes), panel-level coverage_gaps. P1: synthesis-directive routing to the vertical's
enumerative/decision addendum (stage-4 pattern). Both flags OFF → byte-identical off paths
(exact findings-string assertions)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from roster_kernel.contract.dto import Locator
from roster_kernel.providers.embeddings import FakeEmbedder
from roster_kernel.providers.llm import LLMResult
from roster_kernel.research.panel import run_panel
from roster_kernel.research.react import AgentStep, ClaimOut, ComposedAnswer
from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource


@dataclass(frozen=True)
class _Spec:
    id: str
    specialty: str
    lens: str
    focus: str
    source_keys: tuple = ()


_DERIVE = "DERIVE THE PANEL CONTRACT"          # sentinel contract_prompt (routes the scripted LLM)
_ENUM_ADD = "ENUM-ADDENDUM-SENTINEL"
_DECISION_ADD = "DECISION-ADDENDUM-SENTINEL"

_BLOCK = "The approved dose is 5 mg once daily and the response rate was 53 percent."
_CLAIM = dict(text="the approved dose is 5 mg once daily", atom_id="a1",
              quote="The approved dose is 5 mg once daily")


def _source(text=_BLOCK):
    s = InMemoryRetrievalSource()
    s.add(IndexedBlock(block_id="b1", document_id="d1", tenant_id="A", text=text,
                       locator=Locator("block_span", "d1", {"block_id": "b1"}), source_key="corpus"))
    return s


def _specialists():
    return [_Spec("pharm", "Clinical Pharmacology", "You are a pharmacologist.", "dosing, interactions"),
            _Spec("ebm", "Evidence-Based Medicine", "You are an EBM methodologist.", "evidence quality")]


class _LLM:
    """Content-routing scripted LLM (mirrors test_panel.py) that RECORDS what it saw: derivation
    calls, every specialist planner prompt, and the panel synthesis prompt."""

    def __init__(self, contract_out=None, claim=None):
        self.contract_out = contract_out       # dict for the derivation response (None → exploratory/empty)
        self.claim = dict(claim or _CLAIM)
        self.derivations = 0
        self.planner_contents: list[str] = []
        self.synth_prompts: list[str] = []

    async def complete(self, *, system, messages, response_format, max_tokens=2048, temperature=None):
        c = messages[0]["content"]
        if system == _DERIVE:                                  # the shared-contract derivation
            self.derivations += 1
            return LLMResult(parsed=response_format(**(self.contract_out or {})), output_tokens=5)
        if "VERIFIED PANEL FINDINGS" in c:                     # the panel synthesis
            self.synth_prompts.append(c)
            return LLMResult(parsed=ComposedAnswer(answer="The panel agrees [1]."), output_tokens=5)
        if "VERIFIED FINDINGS" in c:                           # a specialist's own compose
            return LLMResult(parsed=ComposedAnswer(answer="Dose is 5 mg once daily [1]."), output_tokens=5)
        self.planner_contents.append(c)
        if "no evidence yet" in c:                             # first planner step → search
            return LLMResult(parsed=AgentStep(action="search", query="dose"), output_tokens=5)
        return LLMResult(parsed=AgentStep(action="answer", claims=[ClaimOut(**self.claim)]),
                         output_tokens=5)


def _run(llm, *, src=None, **kw):
    src = src if src is not None else _source()
    return asyncio.run(run_panel(
        question="What is the dose?", specialists=_specialists(), llm=llm,
        embedder=FakeEmbedder(dim=8), make_retrievers=lambda k: (src, None), tenant_id="A",
        synthesis_directive="Synthesize.", **kw))


# ---- P2: dedup ------------------------------------------------------------------------------------

def test_dedup_merges_lenses_and_annotates_convergence():
    llm = _LLM()
    r = _run(llm, panel_dedup=True)
    findings = llm.synth_prompts[0]
    # both specialists claimed the SAME (atom_id, quote) → ONE pooled survivor, both lenses named
    assert ("[1] (Clinical Pharmacology) the approved dose is 5 mg once daily  "
            "(quote: \"The approved dose is 5 mg once daily\" — corpus)  "
            "(found independently by 2 lenses: Clinical Pharmacology, Evidence-Based Medicine)") in findings
    assert "[2] (" not in findings                       # the duplicate is GONE, not renumbered
    # survivors carry lens_count + lens names (computed convergence, plumbed to the UI/session)
    assert len(r.claims) == 1
    assert r.claims[0]["lens_count"] == 2
    assert r.claims[0]["lenses"] == ["Clinical Pharmacology", "Evidence-Based Medicine"]
    # the chair is told what the annotation MEANS (only because a convergent finding exists)
    assert "COMPUTED convergence" in findings
    # per-specialist takes keep their OWN full claim lists (dedup is pooling-level only)
    assert all(t.n_verified == 1 for t in r.takes)


def test_dedup_off_findings_byte_identical():
    llm = _LLM()
    r = _run(llm, panel_dedup=False)
    findings = llm.synth_prompts[0]
    # EXACT off-path findings block: two lines, no lens annotation, no convergence note
    assert ("[1] (Clinical Pharmacology) the approved dose is 5 mg once daily  "
            "(quote: \"The approved dose is 5 mg once daily\" — corpus)\n"
            "[2] (Evidence-Based Medicine) the approved dose is 5 mg once daily  "
            "(quote: \"The approved dose is 5 mg once daily\" — corpus)") in findings
    assert "found independently" not in findings and "COMPUTED convergence" not in findings
    assert len(r.claims) == 2 and "lens_count" not in r.claims[0] and "lenses" not in r.claims[0]


# ---- P3: shared contract --------------------------------------------------------------------------

def test_contract_derived_once_and_scopes_each_lens():
    llm = _LLM(contract_out=dict(mode="exploratory", axes=["dosing safety", "evidence quality"]))
    r = _run(llm, panel_contract=True, contract_prompt=_DERIVE)
    assert llm.derivations == 1                          # ONE derivation for the panel, not per specialist
    # each lens's focus gained a scoped, kernel-generic coverage line (axes scoped by word overlap
    # with the specialist's own focus/lens text)
    pharm = [c for c in llm.planner_contents if "Clinical Pharmacology" in c]
    ebm = [c for c in llm.planner_contents if "Evidence-Based Medicine" in c]
    assert pharm and all("Ensure coverage of: dosing safety" in c for c in pharm)
    assert ebm and all("Ensure coverage of: evidence quality" in c for c in ebm)
    # both axes are evidenced by the pooled claim? "dosing safety"/"evidence quality" do NOT appear
    # in the claim text → both are honest panel-level gaps
    assert r.coverage_gaps == ["No specialist retrieved evidence for dosing safety",
                               "No specialist retrieved evidence for evidence quality"]


def test_enumerative_slots_gap_and_no_routing_under_two_covered():
    src = _source("The alpha-drug dose is 5 mg once daily; it is an approved option.")
    llm = _LLM(contract_out=dict(mode="enumerative", entities=["alpha-drug", "beta-drug"]),
               claim=dict(text="alpha-drug dose is 5 mg once daily", atom_id="a1",
                          quote="alpha-drug dose is 5 mg once daily"))
    r = _run(llm, src=src, panel_contract=True, contract_prompt=_DERIVE,
             panel_enumerative_addendum=_ENUM_ADD, panel_decision_addendum=_DECISION_ADD)
    # entity slot-matching over POOLED claims: alpha covered, beta a panel-level gap
    assert r.coverage_gaps == ["No specialist retrieved evidence for beta-drug"]
    # only 1 covered entity → NO addendum routed (stage-4 pattern: never on the contract alone)
    assert _ENUM_ADD not in llm.synth_prompts[0] and _DECISION_ADD not in llm.synth_prompts[0]
    # entities go to EVERY lens's coverage line
    assert all("Ensure coverage of: alpha-drug, beta-drug" in c for c in llm.planner_contents
               if "Panel focus" in c)


def test_enumerative_two_covered_routes_enumerative_addendum():
    src = _source("The alpha-drug dose is 5 mg and the beta-drug dose is 10 mg; both are approved.")
    llm = _LLM(contract_out=dict(mode="enumerative", entities=["alpha-drug", "beta-drug"]),
               claim=dict(text="alpha-drug and beta-drug are both approved", atom_id="a1",
                          quote="the beta-drug dose is 10 mg"))
    r = _run(llm, src=src, panel_contract=True, contract_prompt=_DERIVE,
             panel_enumerative_addendum=_ENUM_ADD, panel_decision_addendum=_DECISION_ADD)
    assert r.coverage_gaps == []
    assert _ENUM_ADD in llm.synth_prompts[0]             # enumerative + ≥2 covered → enumerative addendum
    assert _DECISION_ADD not in llm.synth_prompts[0]     # never both
    assert "Synthesize." in llm.synth_prompts[0]         # the base directive is untouched (additive)


def test_exploratory_axes_two_covered_routes_decision_addendum():
    src = _source("The recommended dose is 5 mg once daily and monitoring of renal function is required.")
    llm = _LLM(contract_out=dict(mode="exploratory", axes=["dosing", "monitoring"]),
               claim=dict(text="the dosing is 5 mg and monitoring is required", atom_id="a1",
                          quote="monitoring of renal function is required"))
    r = _run(llm, src=src, panel_contract=True, contract_prompt=_DERIVE,
             panel_enumerative_addendum=_ENUM_ADD, panel_decision_addendum=_DECISION_ADD)
    # exploratory contracts use AXES as the slot list (containment on claim text+title)
    assert r.coverage_gaps == []
    assert _DECISION_ADD in llm.synth_prompts[0]         # exploratory + ≥2 covered axes → decision addendum
    assert _ENUM_ADD not in llm.synth_prompts[0]


def test_coverage_gaps_live_on_panel_result_even_with_no_pooled_claims():
    empty = InMemoryRetrievalSource()                    # nothing retrievable → zero pooled claims
    llm = _LLM(contract_out=dict(mode="enumerative", entities=["alpha-drug", "beta-drug"]))
    r = _run(llm, src=empty, panel_contract=True, contract_prompt=_DERIVE)
    assert "could not ground" in r.synthesis
    assert r.coverage_gaps == ["No specialist retrieved evidence for alpha-drug",
                               "No specialist retrieved evidence for beta-drug"]


# ---- OFF paths byte-identical ---------------------------------------------------------------------

def test_off_paths_byte_identical_to_default_run():
    # a run with NO new kwargs at all (pre-upgrade call shape) …
    base_llm = _LLM()
    base = _run(base_llm)
    # … and a run with every new kwarg EXPLICITLY at its off/None default plus live addenda strings
    off_llm = _LLM()
    off = _run(off_llm, panel_dedup=False, panel_contract=False, contract_prompt=_DERIVE,
               panel_enumerative_addendum=_ENUM_ADD, panel_decision_addendum=_DECISION_ADD)
    assert off_llm.derivations == 0                      # contract never derived when the flag is off
    assert off_llm.synth_prompts[0] == base_llm.synth_prompts[0]   # synthesis prompt byte-identical
    assert sorted(off_llm.planner_contents) == sorted(base_llm.planner_contents)  # lens prompts too
    assert off.coverage_gaps == [] and base.coverage_gaps == []
    assert off.claims == base.claims
