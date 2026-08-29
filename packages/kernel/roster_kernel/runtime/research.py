"""ResearchService — the runnable multi-source research agent.

Composes an LLM + embedder + a set of named RetrievalSources (corpus, web,
workspace, …) and answers a question over any chosen combination, with the
vertical's gating policy + persona driving the loop. This is what the API calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from roster_kernel.contract.protocols import Connector, GatingPolicy, RetrievalSource
from roster_kernel.providers.embeddings import Embedder
from roster_kernel.providers.llm import LLMClient
from roster_kernel.research.budget import BudgetState
from roster_kernel.research.react import AnswerResult, run_react
from roster_kernel.retrieval.multi import MultiSourceRetriever
from pydantic import BaseModel


class _PlainAnswer(BaseModel):
    text: str




# Reasoning & ideas: GROUP the derivations by epistemic kind under readable sub-headings (most-grounded
# first) instead of a flat list that interleaves "Inference / Hypothesis / Idea" line-by-line (which read
# as a confusing label soup). Each group's heading conveys the kind, so the per-line label is dropped.
_DERIVE_GROUPS = (
    ("inference", "What the evidence implies"),
    ("hypothesis", "Plausible reads"),
    ("speculation", "💡 Ideas worth exploring"),
)


def _render_derivations(ds: list) -> str:
    """Render gated derivations as a readable 'Reasoning & ideas' section, GROUPED by epistemic kind
    (implications → plausible reads → ideas). Each line shows the conclusion, its finding basis
    (auditable), and — for non-inferences — what would make it wrong."""
    by_kind: dict[str, list] = {}
    for d in ds:
        by_kind.setdefault((getattr(d, "label", "") or "").strip(), []).append(d)
    blocks: list[str] = []
    for kind, heading in _DERIVE_GROUPS:
        items = by_kind.get(kind) or []
        if not items:
            continue
        rows = []
        for d in items:
            basis = ", ".join(str(b) for b in getattr(d, "basis", ()) or ())
            row = f"- {getattr(d, 'conclusion', '').strip()}"
            if basis:
                row += f"  _(from {basis})_"
            fals = (getattr(d, "falsifier", "") or "").strip()
            if fals and kind != "inference":
                row += f" — wrong if: {fals}"
            rows.append(row)
        blocks.append(f"**{heading}**\n" + "\n".join(rows))
    if not blocks:
        return ""
    return ("## Reasoning & ideas\n"
            "_Grounded reads derived from the findings above — what they imply, plausible interpretations, "
            "and ideas worth exploring. Each cites the findings it rests on and, where it's a guess, what "
            "would make it wrong._\n\n" + "\n\n".join(blocks))


def _render_unverified_priors(priors: list) -> str:
    """Render the parametric-led UNVERIFIED register (ROSTER_PARAMETRIC_LED, T3): the model's OWN asserted
    facts that retrieval could NOT confirm, in a clearly-LABELED section kept visibly SEPARATE from the
    grounded prose — never merged into a cited finding. `priors` is a list of {"text","needs_freshness"}
    (dicts, or objects with those attrs). A `needs_freshness` prior is marked '(may be outdated)'. Empty
    or all-blank → "" (no section) — so an OFF run, whose `unverified_priors` is always empty, is a no-op."""
    rows: list[str] = []
    for p in priors or []:
        text = ((p.get("text") if isinstance(p, dict) else getattr(p, "text", "")) or "").strip()
        if not text:
            continue
        fresh = bool(p.get("needs_freshness") if isinstance(p, dict)
                     else getattr(p, "needs_freshness", False))
        rows.append(f"- {text}" + (" _(may be outdated)_" if fresh else ""))
    if not rows:
        return ""
    return ("## Model's read — not yet verified\n"
            "_These are the model's own assertions that retrieval could NOT confirm. Treat as "
            "unverified — not established fact._\n\n" + "\n".join(rows))


def _render_cruxes(cruxes: list) -> str:
    """Render the intelligence CRUX register (ROSTER_INTELLIGENCE_CORE, T3): the falsifier(s) of the
    competing hypotheses — the concrete observation(s) that would flip the preferred read. `cruxes` is a
    list of strings (the model's falsifier text). This is labeled as what-would-CHANGE-the-read, NOT a
    fact (it never enters the grounded prose or a cited finding). Empty / all-blank → "" (no section), so
    an OFF run — whose `intelligence_cruxes` is always empty — is a byte-identical no-op."""
    rows = [f"- {c.strip()}" for c in (cruxes or []) if isinstance(c, str) and c.strip()]
    if not rows:
        return ""
    lead = ("_The concrete observation(s) that would flip the read above. Each is the model's stated "
            "falsifier — what would change this conclusion, not an established fact._")
    return "## What would change this read\n" + lead + "\n\n" + "\n".join(rows)


def _render_undertested(undertested: list) -> str:
    """Render the intelligence UNDER-TESTED register (ROSTER_INTELLIGENCE_CORE, T-B): the competing
    hypotheses whose red-team DISCONFIRMING search surfaced NO evidence — disconfirmation was attempted
    but nothing was found, so the hypothesis is NOT confirmed, only not-yet-refuted. `undertested` is a
    list of {"index","claim"}. Empty / no valid entries → "" (no section), so an OFF run — whose
    `intelligence_undertested` is always empty — is a byte-identical no-op."""
    rows = []
    for u in (undertested or []):
        if not isinstance(u, dict):
            continue
        idx = u.get("index")
        if idx is None:                      # a well-formed entry always carries its hypothesis index
            continue
        claim = (u.get("claim") or "").strip()
        rows.append(f"- H{idx}: {claim} — not yet disconfirmed by any evidence found; treat with caution."
                    if claim else
                    f"- H{idx}: not yet disconfirmed by any evidence found; treat with caution.")
    if not rows:
        return ""
    lead = ("_The red-team disconfirming search found NO evidence against these — disconfirmation was "
            "attempted and nothing turned up. That is NOT confirmation; treat them as unresolved._")
    return "## Under-tested — not yet disconfirmed\n" + lead + "\n\n" + "\n".join(rows)


def build_history_context(history, *, answer_focus: bool = False) -> str | None:
    """Prior conversation turns → a compact context block (a follow-up can be elliptical). Context ONLY —
    it frames search/interpretation and NEVER becomes a citable claim. Shared by ask() and ask_panel() so
    both thread history identically. `history` is a list of {question, answer, claims?}."""
    if not history:
        return None
    turns = []
    for t in history:
        qy = (t.get("question") or "").strip()
        an = (t.get("answer") or "").strip()
        if not qy:
            continue
        block = f"Q: {qy}\nA: {an[:1200]}" if an else f"Q: {qy}"
        if answer_focus:   # EPISODIC MEMORY: surface prior structured findings so the loop builds on them
            cl = t.get("claims") or []
            estab = "; ".join((c.get("text") or "").strip()
                              for c in cl[:8] if isinstance(c, dict) and c.get("text"))
            if estab:
                block += f"\n[already established: {estab[:1000]}]"
        turns.append(block)
    return "\n\n".join(turns) or None


@dataclass
class FollowupResolution:
    """Structured resolution of a conversational follow-up (the Conversation-Manager output)."""
    core_query: str                     # the follow-up rewritten as a self-contained question
    subject: str = ""                   # the carried subject/entity (transparency)
    needs_clarification: bool = False   # the follow-up is genuinely ambiguous → ask, don't guess
    clarification: str = ""             # the clarifying question to put to the user
    operate_on_prior: bool = False      # transform the PREVIOUS answer (summarize/shorten/…), not new research


@dataclass
class ResearchService:
    llm: LLMClient
    embedder: Embedder
    sources: dict[str, RetrievalSource]
    planner_llm: LLMClient | None = None     # fast model for ReAct planning steps (compose uses llm)
    gating: GatingPolicy | None = None
    persona_prompt: str = "You are an evidence-grounded research agent."
    panel_selection_prompt: str | None = None  # vertical panel auto-selection chair prompt (opaque)
    answer_format: str | None = None        # vertical answer-structure directive (opaque, clinician)
    patient_answer_format: str | None = None # vertical PATIENT-audience compose directive (opaque)
    vision_prompt: str | None = None        # vertical image-description directive (opaque)
    report_prompt: str | None = None        # vertical NATIVE document-reading directive (opaque)
    layman_prompt: str | None = None        # vertical layman-rephrasing directive (opaque)
    gap_prompt: str | None = None           # vertical gap-fill-planner directive (opaque)
    suggest_prompt: str | None = None       # vertical suggested-follow-ups directive (opaque)
    terms_prompt: str | None = None         # vertical key-term-explanation directive (opaque)
    visuals_prompt: str | None = None        # vertical post-hoc answer-visualization directive (opaque)
    refine_prompt: str | None = None         # vertical pre-answer question-refinement directive (opaque)
    triage_prompt: str | None = None         # vertical guided-intake/triage directive (opaque)
    triage_prompt_v2: str | None = None      # vertical intake-v2 directive (opaque; None → v2 falls back to v1)
    reasoned_scaffold_prompt: str | None = None  # alternate-engine scaffold directive (coverage as QUESTIONS)
    reasoned_answer_format: str | None = None    # alternate-engine compose directive (decision-gated answer)
    integrative_prompt: str | None = None        # opt-in complementary/integrative answer-section directive
    integrative_query_hint: str | None = None    # retrieval-steering hint appended when the user opts in
    alt_directive: str | None = None             # Alternative-modality compose directive (CAM-centered + labeling)
    alt_query_hint: str | None = None            # retrieval-steering hint for Alternative mode
    understanding_answer_format: str | None = None  # UNDERSTANDING engine: causal-model compose contract
    understanding_query_hint: str | None = None     # UNDERSTANDING engine: mechanism-steering hint
    source_routing: bool = False                     # SOURCE ROUTING (flag ROSTER_SOURCE_ROUTING): let the
    #                                                 agent name source TYPES to ALSO target per query
    #                                                 (additive scoped leg, never a filter). False → off.
    retrieval_source_cap: float | None = None       # source-diversity cap (flag ROSTER_RETRIEVAL_DIVERSITY):
    #                                                 cap any one source_key to ceil(k*frac) of the top-k
    #                                                 fused pool so a volume-skewed source can't crowd out
    #                                                 others on broad queries. None → byte-identical.
    max_calls: int = 80                     # 40 → 80: stage-2 BudgetState-honesty re-plan — the
    #                                         claims-first/binding/fallback/frame-repair calls are
    #                                         now charged (see budget.py DEFAULT_MAX_LLM_CALLS)
    vertical_name: str = ""
    ui: object | None = None                # the vertical's UIContract (for /config)
    connectors: dict[str, Connector] = field(default_factory=dict)  # for /ingest
    corpus_source_key: str = ""             # the pg-backed corpus key (if any)
    aux_source_keys: tuple[str, ...] = ("web",)   # queried once/step (no variant fan-out) — e.g. web
    claims_first: bool = False              # run the comprehensive extraction pipeline (flag)
    extraction_lenses: tuple[str, ...] = () # vertical lenses for claims-first extraction
    evidence_select: bool = False           # rank claims by relevance before the cap + wider atom window (flag)
    atom_cap: int = 1600                    # per-atom char window for the extractor (raised under evidence_select)
    reasoning_read: bool = False            # surface the validated interpretation + confidence layer (flag)
    readable_prose: bool = False            # ROSTER_READABLE_PROSE: plain-language style over compose (flag)
    golden_answer: bool = False             # ROSTER_GOLDEN_ANSWER: collapse the 8-layer answer-shaping stack to
    #                                         ONE golden compose directive (wired as answer_format at the app
    #                                         boundary with every other layer flag OFF); this field only
    #                                         re-binds the two prose grounding audits in run_react so the
    #                                         freer golden prose stays policed. OFF → byte-identical.
    answer_layout: bool = False             # ROSTER_ANSWER_LAYOUT: grounding-safe presentation pass — reflow
    #                                         the final answer into a scannable whiteboard layout (short
    #                                         paragraphs / bullets / tables / arrow-flows) as a dedicated
    #                                         second pass. Fail-closed (citations preserved, no new hard
    #                                         token) → keeps original on any violation. OFF → byte-identical.
    layout_prompt: str | None = None        # optional override for the reflow directive (kernel default used
    #                                         when None); domain-free presentation instruction.
    layout_llm: LLMClient | None = None     # optional CHEAP model for the layout reflow (mechanical
    #                                         reformat — doesn't need the compose model). None → self.llm.
    axis_complete: bool = False             # ROSTER_ANSWER_AXES: compose addresses each asked aspect + synthesizes
    tech_synthesis: bool = False            # ROSTER_TECH_SYNTHESIS: add a strategic 'how it works' technical synthesis
    deep_synthesis: bool = False            # ROSTER_DEEP_SYNTHESIS: synthesis-first grounded analysis for non-lookup Qs
    deep_answer_format: str | None = None   # the vertical's deep-synthesis compose format (inert until T2/T3 consume it)
    parametric_led: bool = False            # ROSTER_PARAMETRIC_LED: model-integrated-knowledge LEADS a
    #                                         parametric-eligible answer; retrieval VALIDATES every fact.
    #                                         T1 gates the pre-retrieval PriorDraft; OFF → byte-identical.
    prior_draft_prompt: str | None = None   # the vertical's parametric-draft directive (inert data;
    #                                         the flag + routing predicate gate the draft_prior call)
    intelligence_core: bool = False         # ROSTER_INTELLIGENCE_CORE: the model OWNS the inquiry on an
    #                                         eligible answer — drafts competing HYPOTHESES + a frame;
    #                                         (T2) retrieval tests each FOR-and-AGAINST. T1 gates the
    #                                         pre-retrieval draft + inert threading; OFF → byte-identical.
    intelligence_draft_prompt: str | None = None  # the vertical's intelligence-draft directive (inert
    #                                         data; the flag + routing predicate gate draft_intelligence)
    deep_company: bool = False              # ROSTER_DEEP_COMPANY_READER: additive first-step web dossier leg
    company_reader: dict | None = None      # vertical-supplied deep-company templates + compose addendum
    deep_person: bool = False               # ROSTER_DEEP_PEOPLE_READER: first-step person dossier leg
    person_reader: dict | None = None       # vertical-supplied deep-person templates + compose addendum
    entity_open_web: bool = False           # ROSTER_WEB_ENTITY_OPEN: entity-scoped open-web probe (screened) on step 0
    web_open_denoise: bool = False          # ROSTER_WEB_OPEN_DENOISE: open the aux web leg to the FULL web + denoise-screen ALL hits
    web_quality_prompt: str | None = None   # vertical-supplied LLM page-quality screen prompt for the open-web leg
    derive: bool = False                   # ROSTER_DERIVE: gated, labeled derivations over verified claims
    derive_ideas: bool = False              # ROSTER_DERIVE_IDEAS: also generate grounded 'opportunity' ideas
    derive_judge_llm: object | None = None  # optional cross-family validity judge (else reuses self.llm)
    collect_diagnostics: bool = False       # capture a troubleshooting trace (turns/tools/retries/failures) (flag)
    classify_evidence: object | None = None # vertical structural evidence-tier classifier (source_key, facets) -> kind
    evidence_fitness: bool = False          # boost stronger evidence tiers into the compose cap (flag)
    authority_basis: bool = False           # ROSTER_AUTHORITY_BASIS (flag): unconditional stable partition
    #                                         pushing low-basis (rank<=1) claims to the back of the pool +
    #                                         append the authority-basis compose directive. OFF → byte-identical.
    authority_basis_directive: str | None = None  # vertical compose floor directive (opaque; inert data —
    #                                         the flag gates whether the kernel appends it)
    evidence_ranker: object | None = None   # vertical authority pyramid: evidence_kind -> int rank
    freshness: dict | None = None           # vertical freshness policy {min_rank,weight,horizon_years}
    #                                         (flag ROSTER_FRESHNESS_RANKING): recency re-order + as-of
    #                                         disclosure. None → byte-identical to today.
    answer_profiles: dict | None = None     # ANSWER-CONTRACT (flag ROSTER_ANSWER_CONTRACT): {stance:
    #                                         profile} — one classification customizes retrieval+
    #                                         ranking+compose per question. None → byte-identical.
    evidence_identity: bool = False         # render each atom's document identity ⟨title — source⟩ on
    #                                         every LLM-visible surface (Evidence Contract stage 1, flag)
    claim_congruence: bool = False          # unified batched BINDING judge over loop/claims-first/
    #                                         fallback claims: {entailed, on_subject, kind_ok} per
    #                                         claim (Evidence Contract stage 2, flag)
    question_contract: str = ""             # Evidence Contract stage 3 (flag MODE): "" off;
    #                                         "shadow" → derive + log the QuestionContract/legs,
    #                                         change nothing; "steer" → per-entity legs late-merged
    #                                         + slot-aware compose selection + loop coverage gaps
    reflection: str = ""                    # Reflection pass (ROSTER_REFLECTION flag): "" off; "shadow"
    #                                         → derive enriched reflection + log the web-coverage legs it
    #                                         WOULD fire, change nothing; "steer" → intent steer + ON-DEMAND
    #                                         web coverage fan-out (the "muted: didn't look" fix)
    contract_prompt: str | None = None      # vertical QuestionContract derivation directive
    #                                         (opaque — ALL domain vocabulary lives in the vertical)
    explore_legs: bool = False              # exploratory-legs extension (flag): EXPLORATORY
    #                                         contracts' axes become axis-only retrieval legs
    #                                         (cap 4, steer-gated, late-merged); OFF → exploratory
    #                                         behavior byte-identical to today
    answer_mode_routing: bool = False       # Evidence Contract stage 4 (flag): append the vertical's
    #                                         enumerative-compose addendum when the derived contract
    #                                         is enumerative AND ≥2 entities hold slot-matched claims
    enumerative_compose_addendum: str | None = None  # vertical enumerative-compose addendum (opaque)
    # ROSTER_CONTRACT_COMPOSE (voice ⟂ shape): when `contract_compose` is on, compose renders the derived
    # contract — the directive is VOICE + the SHAPE for the contract's mode (enumerative shape gets the
    # concrete items/dimensions appended by the kernel). Replaces the flat golden directive. All three
    # None/off → byte-identical.
    contract_compose: bool = False
    contract_compose_voice: str | None = None
    contract_compose_shapes: dict | None = None
    contract_compose_default: str | None = None
    # ROSTER_ENUM_ENTITY_PROBE: for an enumerative "table of the main X" ask with no user-named items, the
    # derivation proposes `probe_entities` that SEED targeted entity×axis retrieval (never rows). OFF →
    # axis-only (byte-identical). Forwarded to run_react.
    enum_entity_probe: bool = False
    # ROSTER_WEB_ONLY: research every question thoroughly from the web (no corpus fallback).
    web_only: bool = False
    graph_expander: object | None = None    # A9: async (question) -> {"legs":[{query,note}],"shadow":bool}|None
    #                                         — app-injected relationship-graph hook; None → byte-identical
    panel_specialists: tuple = ()           # Ask-Panel roster (vertical-supplied specialist configs)
    panel_default_ids: tuple = ()           # ids the default panel runs
    panel_synthesis_directive: str | None = None
    panel_examples: tuple = ()              # sample multi-specialty cases seeded into the panel intake
    panel_dedup: bool = False               # P2 (flag): dedup pooled panel claims by (atom_id,
    #                                         normalized quote); survivors carry lens_count + names
    panel_contract: bool = False            # P3+P1 (flag): ONE shared QuestionContract per panel run
    #                                         (+1 call) → scoped lens coverage, pooled slot-matching,
    #                                         panel coverage_gaps, decision-synthesis routing
    panel_enumerative_addendum: str | None = None  # vertical panel enumerative addendum (opaque)
    panel_decision_addendum: str | None = None     # vertical panel decision-grid addendum (opaque)

    def _retriever(self, source_keys: list[str] | None) -> MultiSourceRetriever:
        chosen = {k: v for k, v in self.sources.items()
                  if source_keys is None or k in source_keys} or self.sources
        return MultiSourceRetriever(chosen)

    def _split_retriever(self, source_keys, extra_sources: dict | None = None):
        """Corpus (vector, multi-query) and AUX (web, single-query per step) retrievers. Web is
        split out so it's queried ONCE per step on the original query — not fanned out per
        reformulation — which keeps web latency bounded while still adding breadth.
        `extra_sources` are PER-REQUEST additions (e.g. the user's uploaded report as a citable
        in-memory source) — merged into the corpus leg, never into the shared service dict."""
        chosen = {k: v for k, v in self.sources.items()
                  if source_keys is None or k in source_keys} or self.sources
        chosen = {**chosen, **(extra_sources or {})}
        aux = {k: v for k, v in chosen.items() if k in self.aux_source_keys}
        corpus = {k: v for k, v in chosen.items() if k not in self.aux_source_keys}
        if not corpus:                       # web-only selection → treat it as the primary source
            return MultiSourceRetriever(chosen), None
        return MultiSourceRetriever(corpus), (MultiSourceRetriever(aux) if aux else None)

    async def ask_reasoned(self, route: bool = True, force_kind: str | None = None, **kw):
        """REASONED engine with DYNAMIC per-question routing. The single scaffold call does double duty
        (zero extra LLM calls): it first CLASSIFIES the question — management/case (differential, workup,
        treatment choice) vs pure evidence LOOKUP (trial results, pharmacokinetics, definitions) — then:
          - management → coverage scaffold (QUESTIONS only, never conclusions — grounding-safe) steers
            retrieval + the decision-gated compose directive;
          - lookup → falls through to the STANDARD adaptive engine (a decision frame is the wrong shape
            for an evidence summary).
        `route=False` (the duel's explicit "reasoned" arm) FORCES the reasoned pipeline so A/B contrast
        stays honest. Follow-ups run the standard adaptive compose (narrower asks; the scaffold's brief
        would interfere with follow-up resolution). Fail-open everywhere — errors → reasoned-no-brief."""
        if not (self.reasoned_scaffold_prompt and self.reasoned_answer_format):
            return await self.ask(**kw)
        if force_kind == "understanding" and self.understanding_answer_format:
            # EXPLICIT hop to the understanding engine (interlock chip) — no classification needed;
            # works mid-thread too (the causal lens on the same question).
            kw = dict(kw)
            if self.understanding_query_hint:
                kw["graph_question"] = kw.get("question", "")
                kw["question"] = (kw.get("question", "") + "\n\n[" + self.understanding_query_hint + "]")
            kw["answer_format_override"] = self.understanding_answer_format
            kw["kind"] = "understanding"     # thread kind for DEEP SYNTHESIS gating in run_react
            oe = kw.get("on_event")
            if oe is not None:
                try:
                    await oe({"type": "engine", "engine": "understanding", "why": "user-selected"})
                except Exception:
                    pass
            return await self.ask(**kw)
        # History counts as a CONVERSATION only if some prior turn was actually ANSWERED — a thread of
        # error/retry turns is a fresh question (else a retry-after-failure silently demoted the engine
        # to the standard format: the "where did Do-now go?" bug).
        _hist = kw.get("history") or []
        if any((t.get("answer") or "").strip() for t in _hist if isinstance(t, dict)):
            if not route:   # explicitly forced reasoned (duel arm / hop chip) → keep the decision-gated format
                kw = dict(kw)
                kw["answer_format_override"] = self.reasoned_answer_format
            return await self.ask(**kw)
        question = kw.get("question", "")
        on_event = kw.get("on_event")
        from typing import Literal
        from pydantic import BaseModel, Field

        class _Scaffold(BaseModel):
            # kind = the routing judgment (LLM-owned, Rule 18). Lists are QUESTIONS/topics to cover.
            kind: Literal["management", "lookup", "understanding"] = "management"
            likely_causes: list[str] = Field(default_factory=list)
            cant_miss: list[str] = Field(default_factory=list)
            key_decisions: list[str] = Field(default_factory=list)
            explicit_asks: list[str] = Field(default_factory=list)   # the audit contract: what was literally asked

        async def _emit(ev):
            if on_event is not None:
                try:
                    await on_event(ev)
                except Exception:
                    pass
        _reasoned_kind = ""    # classified kind, threaded to run_react for DEEP SYNTHESIS gating
        try:
            comp = await self.llm.complete(
                system=self.reasoned_scaffold_prompt,
                messages=[{"role": "user", "content": question}],
                response_format=_Scaffold, max_tokens=1200)
            s = comp.parsed
            _reasoned_kind = s.kind
            # PARAMETRIC-LED (flag ROSTER_PARAMETRIC_LED, T1): when ON, derive the question's stance +
            # subject_kind (same contract flow run_react uses) and, if the routing predicate holds,
            # produce a pre-retrieval PriorDraft and thread it INERTLY into whichever ask() fires
            # (kw is copied — never mutated in place — so OFF / not-eligible stays byte-identical). The
            # draft is UNUSED by compose in T1; T2/T3 consume it. Fail-safe: any error → no draft, today's
            # path. NOTE: stance/subject_kind are NOT available in ask_reasoned (run_react derives them),
            # so this runs its OWN derive_contract — one extra charged call, ONLY when the flag is on and
            # the vertical supplies contract_prompt (run_react re-derives; de-duping is a T2 concern).
            # INTELLIGENCE-CORE (flag ROSTER_INTELLIGENCE_CORE, T1): parallel to the parametric block and
            # sharing its eligibility predicate (stance=established + kind∈{understanding,management} +
            # subject!=specific_entity). When ON + eligible, draft competing HYPOTHESES + an analytical
            # FRAME and, if >=2 well-formed hypotheses parse, thread them INERTLY into whichever ask()
            # fires (kw is copied — OFF / not-eligible / <2 stays byte-identical). The hypotheses are
            # UNUSED by compose in T1; T2 (adversarial retrieval) + T3 (compose) consume them. Fail-safe:
            # any error → no threading, today's path. Preferred over parametric when BOTH are somehow on
            # (they shouldn't overlap in practice) — `_intel_threaded` skips the parametric block below.
            # BUDGET NOTE (honest): the REQUEST budget is constructed downstream in ask() (it doesn't
            # exist yet here), so — exactly like the parametric block — the draft charges a LOCAL
            # BudgetState; this spend is not yet reflected in the request budget. That's the least-bad
            # option for T1; threading the real budget/reserve is a T2 concern (same limitation the
            # parametric draft carries).
            _intel_threaded = False
            if self.intelligence_core:
                # Observability (Rule 13): the intelligence block used to swallow every non-engage path
                # (ineligible routing / degenerate draft / exception) into a silent fall-back, so a prod
                # answer that "didn't engage" was indistinguishable from one that engaged-but-errored.
                # `_skip_reason` records WHY it did not thread; emitted as an `intelligence_skip` trace and
                # logged. This does NOT change the engage decision — the threading logic is byte-identical;
                # only the non-engaged branches gain a trace.
                _skip_reason = ""
                try:
                    from roster_kernel.research.budget import BudgetState
                    from roster_kernel.research.contract import derive_contract
                    from roster_kernel.research.intelligence_draft import (draft_intelligence,
                                                                          parse_hypotheses)
                    _c = await derive_contract(question, self.llm, self.contract_prompt)
                    _stance = getattr(_c, "stance", "") if _c else ""
                    _subject = getattr(_c, "subject_kind", "") if _c else ""
                    _imode = getattr(_c, "mode", "") if _c else ""
                    # ENUMERATION routes to parametric-led, NOT here: a "best-X / list the tools / map the
                    # landscape" ask (mode=="enumerative") wants the model to DRAFT the candidate set and
                    # retrieval to VERIFY each — competing-hypotheses is the wrong frame for it. So an
                    # enumerative contract is ineligible for intelligence-core, freeing it for the
                    # parametric-led block below (which no longer requires stance=='established').
                    # Eligible stances = {established, balanced}. `balanced` (genuinely contested /
                    # multi-sided) is the PRIME case for competing-hypotheses + adversarial for/against
                    # retrieval, and the contract flips a strategy Q between 'established' and 'balanced'
                    # nondeterministically (the management-moat flake: local='established', prod='balanced'
                    # for the same Q) — accepting both makes engagement consistent. `current` stays
                    # retrieval-led (recency wants fresh facts, not a hypothesis frame); the recency
                    # control Q also stays out via the specific_entity guard.
                    _intel_eligible = (_stance in ("established", "balanced")
                                       and s.kind in ("understanding", "management")
                                       and _subject != "specific_entity"
                                       and _imode != "enumerative")
                    if not _intel_eligible:
                        _skip_reason = (f"ineligible(kind={s.kind!r},stance={_stance!r},"
                                        f"subject={_subject!r})")
                    else:
                        _draft = await draft_intelligence(
                            question, self.llm, self.intelligence_draft_prompt,
                            budget=BudgetState(max_calls=self.max_calls))
                        _hyps = parse_hypotheses(getattr(_draft, "hypotheses_text", "")) if _draft else []
                        # Require >=2 genuinely-competing hypotheses; a degenerate draft can't drive the
                        # adversarial for/against retrieval, so fall back to today's retrieval-led path.
                        if len(_hyps) >= 2:
                            kw = dict(kw)
                            kw["hypotheses"] = _hyps
                            kw["intelligence_frame"] = (getattr(_draft, "frame", "") or "").strip()
                            _intel_threaded = True
                            await _emit({"type": "intelligence_core", "hypotheses": len(_hyps)})
                        else:
                            _skip_reason = (f"draft_hyps={len(_hyps)}"
                                            f"(draft={'none' if _draft is None else 'ok'})")
                except Exception as _e:   # noqa: BLE001 — the intelligence draft is an enhancer; never blocks the answer
                    _skip_reason = f"exception:{type(_e).__name__}:{str(_e)[:160]}"
                if not _intel_threaded and _skip_reason:
                    await _emit({"type": "intelligence_skip", "reason": _skip_reason})
                    import logging
                    logging.getLogger(__name__).info("intelligence_core skipped: %s", _skip_reason)
            if self.parametric_led and not _intel_threaded:
                try:
                    from roster_kernel.research.budget import BudgetState
                    from roster_kernel.research.contract import derive_contract
                    from roster_kernel.research.prior_draft import draft_prior
                    _c = await derive_contract(question, self.llm, self.contract_prompt)
                    _stance = getattr(_c, "stance", "") if _c else ""
                    _subject = getattr(_c, "subject_kind", "") if _c else ""
                    _mode = getattr(_c, "mode", "") if _c else ""
                    # Parametric-led is the model-integrated-knowledge LEAD: the model drafts the answer's
                    # structure + candidate facts (which it KNOWS — e.g. the actual OSS tools of a stack),
                    # then retrieval VERIFIES each fact and the span-gate grounds it (only verified facts
                    # survive). It's the winning path for BREADTH questions where pure grounding-first
                    # retrieval misses the well-known entities: ENUMERATION / "best-X" / landscape
                    # (mode=="enumerative") AND understanding/management analysis. NOT for a single-entity
                    # (specific_entity) diligence — retrieval should lead there — nor a pure lookup. Any
                    # stance (the earlier established-only gate excluded exactly the enumeration questions
                    # that need this most). Grounding is unchanged: a drafted fact that fails to verify is
                    # dropped, never emitted.
                    _parametric_eligible = (
                        self.parametric_led
                        and _subject != "specific_entity"
                        and s.kind != "lookup"
                        and (s.kind in ("understanding", "management") or _mode == "enumerative"))
                    if _parametric_eligible:
                        _pd = await draft_prior(question, self.llm, self.prior_draft_prompt,
                                                budget=BudgetState(max_calls=self.max_calls))
                        # A claim-less (degenerate) draft can't drive parametric verification — engaging it
                        # would skip the agentic loop and abstain (the empty-answer failure). Require at
                        # least one claim; else fall back to today's retrieval-led path (prior_draft unset).
                        if _pd is not None and getattr(_pd, "claims", None):
                            kw = dict(kw)
                            kw["prior_draft"] = _pd
                            await _emit({"type": "parametric_led", "claims": len(_pd.claims)})
                except Exception:   # noqa: BLE001 — parametric draft is an enhancer; never blocks the answer
                    pass
            if route and s.kind == "lookup":
                # pure evidence lookup → the standard adaptive engine fits better; say so in the trace
                await _emit({"type": "engine", "engine": "standard", "why": "evidence lookup"})
                return await self.ask(**{**kw, "kind": "lookup"})
            if route and s.kind == "understanding" and self.understanding_answer_format:
                # WHY/HOW question → the UNDERSTANDING engine: mechanism-steered retrieval + the
                # causal-model compose contract (per-link evidence-status labels)
                kw = dict(kw)
                if self.understanding_query_hint:
                    kw["graph_question"] = question
                    kw["question"] = question + "\n\n[" + self.understanding_query_hint + "]"
                kw["answer_format_override"] = self.understanding_answer_format
                kw["kind"] = "understanding"
                await _emit({"type": "engine", "engine": "understanding", "why": "why/how question"})
                return await self.ask(**kw)
            lines = ([f"- explicitly asked: {x}" for x in s.explicit_asks[:8]]
                     + [f"- likely/common: {x}" for x in s.likely_causes[:6]]
                     + [f"- can't-miss: {x}" for x in s.cant_miss[:6]]
                     + [f"- decision: {x}" for x in s.key_decisions[:6]])
            if lines:
                kw = dict(kw)
                kw["graph_question"] = question     # expander anchors on the ASKED subject, not brief branches
                kw["question"] = (question + "\n\n[Coverage brief — branches this answer must "
                                  "INVESTIGATE and address (these are questions to research, not facts):\n"
                                  + "\n".join(lines) + "\n]")
                await _emit({"type": "engine", "engine": "reasoned", "why": "management question"})
                await _emit({"type": "scaffold", "branches": len(lines)})
        except Exception:   # noqa: BLE001 — scaffold is an enhancer; its failure never blocks the answer
            pass
        kw["answer_format_override"] = self.reasoned_answer_format
        kw["kind"] = _reasoned_kind      # management (or route=False lookup/understanding); "" if scaffold failed
        return await self.ask(**kw)

    async def ask(
        self,
        *,
        question: str,
        tenant_id: str,
        workspace_id: str | None = None,
        source_keys: list[str] | None = None,
        images: list[dict] | None = None,
        documents: list[dict] | None = None,
        pdf_docs: list[dict] | None = None,  # raw PDFs → NATIVE model reading (layout-faithful digests)
        history: list[dict] | None = None,
        on_event=None,                       # async callback(dict) for live progress (SSE)
        facets: dict | None = None,          # hard retrieval facet filter (e.g. source_country scope)
        exclude_facets: dict | None = None,  # EXCLUSION filter (e.g. keep modality=alternative out of default)
        country_boost=None,                  # set of country codes to BOOST (surface region evidence, no filter)
        max_steps: int = 8,
        effort: float = 1.0,                 # research-effort multiplier (1.0 = baseline no-op)
        audience: str = "clinician",         # "clinician" (default) | "patient" — selects the compose directive ONLY
        answer_focus: bool = False,          # condense elliptical follow-ups + ANSWER-scope compose (flag)
        clarify: bool = False,               # ask a clarifying question when a follow-up is ambiguous (flag)
        answer_format_override: str | None = None,   # per-call compose directive (alternate engine); None → default
        extra_directive: str | None = None,          # per-call ADDENDUM appended to the selected directive
        suppress_authority: bool = False,            # per-call: neutralize the authority tier-boost in
        #                                              ranking (a use-case lens for opinion/foresight
        #                                              queries, so expert/discussion evidence isn't
        #                                              demoted below filings). False → today's behavior.
        graph_question: str | None = None,   # PRISTINE user question for the graph expander — callers
        #                                      that augment `question` (reasoned coverage brief, engine
        #                                      hints) MUST pass the original here, or topic matching
        #                                      anchors on brief-mentioned branches instead of the asked
        #                                      subject (the Parkinson-leg prod bug)
        question_context: str | None = None, # caller-supplied PLANNER-ONLY framing (e.g. a vertical's
        #                                      structural brand→generic mapping, Noesis IN D-3) — rides
        #                                      the attachment-context channel, so it frames search and
        #                                      NEVER reaches compose or becomes citable
        kind: str = "",                       # question kind (management/lookup/understanding) from the
        #                                      reasoned scaffold — threads into run_react so DEEP SYNTHESIS
        #                                      keeps lookups crisp. "" → treated as non-lookup (best-effort).
        prior_draft=None,                     # ROSTER_PARAMETRIC_LED (T1): the pre-retrieval PriorDraft
        #                                      when the question is parametric-eligible — threaded INERTLY
        #                                      to run_react (declared-but-unused until T2/T3 consume it).
        #                                      None → today's retrieve-first path (byte-identical).
        hypotheses=None,                      # ROSTER_INTELLIGENCE_CORE (T1): the parsed competing
        #                                      Hypotheses when the question is intelligence-eligible —
        #                                      threaded INERTLY to run_react (declared-but-unused until
        #                                      T2 adversarial retrieval / T3 compose). None → byte-identical.
        intelligence_frame=None,              # ROSTER_INTELLIGENCE_CORE (T1): the drafted analytical frame
        #                                      (prose) paired with `hypotheses`; inert until T3. None → today.
    ) -> AnswerResult:
        # ANSWER-FOCUS (flag): resolve a conversational FOLLOW-UP ("what dose?") into a self-contained
        # question carrying the subject from the conversation ("dose of TMP-SMX for PCP prophylaxis"),
        # BEFORE retrieval — so the query, the relevance ranking, AND compose all inherit the subject
        # (they all key off `question`). Only fires with history present; off/no-history → no-op. The
        # resolver sees ONLY the conversation (never the corpus), so it can't inject retrieved content;
        # the ORIGINAL question is preserved as `question_original` for echo/persistence. With `clarify`,
        # a genuinely ambiguous follow-up short-circuits into a clarifying question (no research run).
        question_original = question
        resolved_question = ""
        if answer_focus and history:
            r = await self._resolve_followup(question, history, allow_clarify=clarify)
            # OPERATE-ON-PRIOR (#5): "summarize that / shorten / explain point 2" → transform the
            # previous answer with NO new retrieval (adds no new facts). Fall through to normal
            # research if there's no prior answer or the transform fails.
            if r.operate_on_prior:
                prior = next((t.get("answer") for t in reversed(history)
                              if (t.get("answer") or "").strip()), "")
                if prior:
                    transformed = await self._transform_prior(question, prior)
                    if transformed:
                        if on_event is not None:
                            try:
                                await on_event({"type": "operate_prior"})
                            except Exception:
                                pass
                        out = AnswerResult(stopped_reason="operate_prior")
                        out.composed_answer = transformed
                        out.derived_from_prior = True
                        return out
            if r.needs_clarification and r.clarification:
                if on_event is not None:
                    try:
                        await on_event({"type": "clarify", "question": r.clarification})
                    except Exception:
                        pass
                out = AnswerResult(stopped_reason="clarify")
                out.clarification = r.clarification   # ask the user; skip the research run entirely
                return out
            if r.core_query and r.core_query != question:
                resolved_question = r.core_query
                question = r.core_query
                if on_event is not None:
                    try:
                        await on_event({"type": "resolved_question", "question": question})
                    except Exception:
                        pass
        # Audience changes ONLY the compose directive — same retrieval, same persona/system_prompt,
        # same span/entailment gates. "patient" uses the vertical's patient directive when it supplies
        # one; anything else (incl. an unknown value) falls back to the clinician directive → the
        # default path is byte-identical.
        if audience == "patient" and self.patient_answer_format:
            directive = self.patient_answer_format     # patient view always wins (per-audience contract)
        else:
            directive = answer_format_override or self.answer_format
        if extra_directive:   # opt-in addendum (e.g. integrative section) — appended to WHICHEVER directive won
            directive = (directive + "\n\n" + extra_directive) if directive else extra_directive
        if self.deep_company and self.company_reader:
            _dcr = (self.company_reader.get("attribution_addendum") or "").strip()
            if _dcr:
                directive = (directive + "\n\n" + _dcr) if directive else _dcr
        if self.deep_person and self.person_reader:
            _dpr = (self.person_reader.get("attribution_addendum") or "").strip()
            if _dpr:
                directive = (directive + "\n\n" + _dpr) if directive else _dpr
        # Effort scales STRUCTURAL search knobs only (turns, results, context, citations, budget) —
        # never the grounding gates. At effort<=1.0 every value round-trips to today's exact defaults,
        # so this is a byte-identical no-op when the caller passes 1.0 (flag OFF).
        from roster_kernel.research.effort import scale_research_effort
        sc = scale_research_effort(
            effort, base_max_steps=max_steps, base_atom_cap=self.atom_cap, base_max_calls=self.max_calls)
        budget = BudgetState(max_calls=sc.max_calls)
        # Attachment context (never corpus evidence, never a verified claim):
        #  - images/scans → a labeled DESCRIPTIVE vision observation (vision pre-step),
        #  - uploaded documents (e.g. a paper PDF) → their extracted TEXT.
        # Both are combined into attachment_context that only frames the search.
        visual_obs = ""
        if images and self.vision_prompt:
            from roster_kernel.research.vision import observe_images
            try:
                visual_obs = await observe_images(
                    llm=self.llm, vision_prompt=self.vision_prompt,
                    images=images, budget=budget)
            except Exception:
                visual_obs = ""             # a failed vision read must not break research
        parts: list[str] = []
        att_texts: list[tuple[str, str]] = []   # (name, text) → per-request CITABLE attachment source
        if visual_obs:
            parts.append("IMAGE (automated visual description):\n" + visual_obs)
        # NATIVE PDF reading: the model sees the original file (page layout intact), so report
        # tables keep their analyte/value/unit/range associations — the fix for scrambled
        # text-layer extraction on lab reports. Falls back to the text layer per document.
        if pdf_docs and self.report_prompt:
            from roster_kernel.research.vision import read_documents
            try:
                reads = await read_documents(llm=self.llm, report_prompt=self.report_prompt,
                                             pdfs=pdf_docs, budget=budget)
            except Exception:   # noqa: BLE001
                reads = []
            read_names = {r["name"] for r in reads}
            for r in reads:
                parts.append(f"DOCUMENT — {r['name']} (structured digest, read natively from the file):\n{r['digest']}")
                att_texts.append((r["name"], r["digest"]))
            for pdf in pdf_docs:
                fb = (pdf.get("text_fallback") or "").strip()
                if pdf.get("name") not in read_names and fb:
                    parts.append(f"DOCUMENT — {pdf.get('name') or 'document'} (text-layer extraction):\n{fb[:20000]}")
                    att_texts.append((pdf.get("name") or "document", fb[:20000]))
        elif pdf_docs:
            for pdf in pdf_docs:
                fb = (pdf.get("text_fallback") or "").strip()
                if fb:
                    parts.append(f"DOCUMENT — {pdf.get('name') or 'document'} (text-layer extraction):\n{fb[:20000]}")
                    att_texts.append((pdf.get("name") or "document", fb[:20000]))
        for d in documents or []:
            txt = (d.get("text") or "").strip()
            if txt:
                name = d.get("name") or "document"
                parts.append(f"DOCUMENT — {name} (user-provided text):\n{txt}")
                att_texts.append((name, txt))
        if question_context:
            parts.append(question_context.strip())
        attachment_context = "\n\n".join(parts) or None

        # Prior conversation turns → a compact context block (a follow-up can be elliptical). This
        # only frames search/interpretation; it never becomes a grounded claim (like attachments).
        history_context = build_history_context(history, answer_focus=answer_focus)

        # THE ATTACHMENT AS A CITABLE SOURCE: for "analyze this report" questions the user's
        # document IS the primary content — its digest/text becomes a per-request in-memory
        # retrieval source, so claims can CITE the report with span-verified quotes (clearly
        # labeled "(user upload)"), while the corpus supplies the interpretive evidence.
        att_source = None
        if att_texts:
            import asyncio as _aio
            from roster_kernel.contract.dto import Locator as _Loc
            from roster_kernel.ingestion.storage import content_key as _ck
            from roster_kernel.retrieval.memory import IndexedBlock, InMemoryRetrievalSource
            from roster_kernel.retrieval.web import _chunk_text as _chunk
            chunks: list[tuple[str, str]] = []
            for _nm, _txt in att_texts:
                for _c in _chunk(_txt, max_chars=900)[:40]:
                    chunks.append((_nm, _c))
            if chunks:
                _embs = await _aio.to_thread(lambda: self.embedder.embed([c for _, c in chunks]))
                att_source = InMemoryRetrievalSource()
                att_source.key = "attachment"
                for (_nm, _c), _e in zip(chunks, _embs):
                    _bid = _ck(f"attachment:{_nm}|{_c}".encode())
                    _did = f"attachment:{_nm}"
                    att_source.add(IndexedBlock(
                        block_id=_bid, document_id=_did, text=_c, tenant_id=tenant_id,
                        embedding=tuple(_e), facets={"source_kind": "user_attachment"},
                        locator=_Loc("block_span", _did, {"block_id": _bid}),
                        document_title=f"{_nm} (user upload)", content_type="text/plain",
                        source_key="attachment"))
        if att_texts:
            # COMPLETENESS HINT (attachment-triggered, per-request — the validated compose
            # directive is untouched): an "analyze this" over a document means ALL of it.
            question = (question + "\n\n[The user attached document(s). Analyze them COMPLETELY: "
                        "address EVERY section/panel the document digest lists (see SECTIONS "
                        "PRESENT), searching for interpretive evidence per section — partially "
                        "analyzing an attached report is a failure.]")
        corpus_src, web_src = self._split_retriever(
            source_keys, extra_sources={"attachment": att_source} if att_source else None)
        # A9 graph-guided evidence legs (app-injected hook; best-effort — a graph failure never
        # delays or breaks the answer path). The hook decides legs AND mode (shadow vs merged).
        graph_legs, graph_shadow, graph_late = None, False, False
        if self.graph_expander is not None:
            try:
                # anchor topic matching on the ASKED subject: explicit pristine question from the
                # caller, else the follow-up-resolved question, else what ask() received —
                # never the augmented `question` (brief/hints name branch topics, not the subject)
                _gx = await self.graph_expander(
                    graph_question or resolved_question or question_original)
                if _gx and _gx.get("legs"):
                    graph_legs = list(_gx["legs"])
                    graph_shadow = bool(_gx.get("shadow"))
                    graph_late = bool(_gx.get("late"))
            except Exception:   # noqa: BLE001
                graph_legs = None
        res = await run_react(
            question=question, llm=self.llm, embedder=self.embedder,
            source=corpus_src, aux_source=web_src,
            tenant_id=tenant_id, workspace_id=workspace_id,
            budget=budget, gating=self.gating,
            system_prompt=self.persona_prompt, answer_format=directive,
            attachment_context=attachment_context, history_context=history_context,
            planner_llm=self.planner_llm, on_event=on_event,
            claims_first=self.claims_first, extraction_lenses=self.extraction_lenses,
            evidence_select=self.evidence_select, atom_cap=sc.atom_cap,
            facets=facets or {}, exclude_facets=exclude_facets or {},
            max_steps=sc.max_steps, k=sc.k, planner_atom_window=sc.planner_atom_window,
            compose_claim_cap=sc.compose_claim_cap, extract_collect=sc.extract_collect,
            answer_focus=answer_focus, reasoning_read=self.reasoning_read,
            readable_prose=self.readable_prose, country_boost=country_boost,
            golden_answer=self.golden_answer,   # ROSTER_GOLDEN_ANSWER: re-bind the two prose grounding audits
            axis_complete=self.axis_complete, tech_synthesis=self.tech_synthesis,
            deep_synthesis=self.deep_synthesis, deep_answer_format=self.deep_answer_format,
            prior_draft=prior_draft,   # ROSTER_PARAMETRIC_LED (T1): inert; consumed by T2/T3
            hypotheses=hypotheses, intelligence_frame=intelligence_frame,  # ROSTER_INTELLIGENCE_CORE (T1): inert; T2/T3
            kind=kind, derive_ideas=self.derive_ideas, derive_judge_llm=self.derive_judge_llm,
            deep_company=self.deep_company, company_reader=self.company_reader,
            deep_person=self.deep_person, person_reader=self.person_reader,
            entity_open_web=self.entity_open_web, web_open_denoise=self.web_open_denoise,
            web_quality_prompt=self.web_quality_prompt,
            collect_diagnostics=self.collect_diagnostics,
            classify_evidence=self.classify_evidence,
            evidence_fitness=self.evidence_fitness, evidence_ranker=self.evidence_ranker,
            authority_basis=self.authority_basis, authority_basis_directive=self.authority_basis_directive,
            retrieval_source_cap=self.retrieval_source_cap, suppress_authority=suppress_authority,
            source_routing=self.source_routing,
            freshness=self.freshness, answer_profiles=self.answer_profiles,
            evidence_identity=self.evidence_identity, claim_congruence=self.claim_congruence,
            question_contract=self.question_contract, contract_prompt=self.contract_prompt,
            reflection=self.reflection,
            explore_legs=self.explore_legs,
            answer_mode_routing=self.answer_mode_routing,
            enumerative_compose_addendum=self.enumerative_compose_addendum,
            contract_compose=self.contract_compose,
            enum_entity_probe=self.enum_entity_probe,
            web_only=self.web_only,
            contract_compose_voice=self.contract_compose_voice,
            contract_compose_shapes=self.contract_compose_shapes,
            contract_compose_default=self.contract_compose_default,
            graph_legs=graph_legs, graph_shadow=graph_shadow, graph_late=graph_late,
        )
        res.visual_observation = visual_obs      # surface the image reading (UI panel)
        res.effort = sc.effort                   # echo the resolved multiplier (observability)
        res.resolved_question = resolved_question # condensed question if it differed (observability)

        # GROUNDED REASONING (flag): derive labeled, checked conclusions FROM the verified findings —
        # a second trust regime on top of the fact gate. Additive: adds no fact, only reasons over the
        # findings run_react already verified; a failure never breaks the answer.
        # SKIP when DEEP SYNTHESIS already WOVE derivations into the answer (deep + non-lookup): run_react
        # ran derive pre-compose and folded it into the prose spine, so appending the post-compose
        # "## Reasoning & ideas" section here would duplicate it. Non-deep / lookup → unchanged.
        _deep_wove = self.deep_synthesis and kind != "lookup"
        if self.derive and getattr(res, "verified_claims", None) and not _deep_wove:
            try:
                from roster_kernel.research.reason import derive as _derive
                ds = await _derive(
                    resolved_question or question, res.verified_claims, self.llm,
                    generate_ideas=self.derive_ideas, judge_llm=self.derive_judge_llm)
                res.derivations = ds
                section = _render_derivations(ds)
                if section:
                    res.composed_answer = (res.composed_answer or "").rstrip() + "\n\n" + section
            except Exception:   # noqa: BLE001 — reasoning is additive; never sink the grounded answer
                pass

        # PARAMETRIC-LED (flag ROSTER_PARAMETRIC_LED, T3): append the UNVERIFIED register — the model's own
        # asserted facts retrieval could NOT ground — as a visibly SEPARATE labeled section, AFTER the
        # grounded prose (and after the derivations section), never merged into a cited finding. Rendered
        # the SAME way derivations are (a labeled post-compose section). `unverified_priors` is populated
        # ONLY on a parametric run (prior_draft set) and is empty otherwise, so this reduces to a no-op
        # and the OFF composed_answer stays byte-identical.
        _uv = _render_unverified_priors(getattr(res, "unverified_priors", None) or [])
        if _uv:
            res.composed_answer = (res.composed_answer or "").rstrip() + "\n\n" + _uv

        # INTELLIGENCE-CORE (flag ROSTER_INTELLIGENCE_CORE, T3): append the CRUX register — the falsifier(s)
        # of the drafted competing hypotheses (the observation that would flip the preferred read) — as a
        # labeled post-compose "## What would change this read" section, the SAME shape as the derivations /
        # unverified-priors sections. `intelligence_cruxes` is populated ONLY on an intelligence run
        # (hypotheses present) and empty otherwise, so this reduces to a no-op and the OFF composed_answer
        # stays byte-identical.
        _cx = _render_cruxes(getattr(res, "intelligence_cruxes", None) or [])
        if _cx:
            res.composed_answer = (res.composed_answer or "").rstrip() + "\n\n" + _cx

        # INTELLIGENCE-CORE (flag ROSTER_INTELLIGENCE_CORE, T-B): append the UNDER-TESTED register — the
        # competing hypotheses whose red-team disconfirming search found NO evidence — as a labeled
        # post-compose section (same shape as the crux register). `intelligence_undertested` is
        # populated ONLY on an intelligence run whose against-search came up empty, and is empty
        # otherwise, so this reduces to a no-op and the OFF composed_answer stays byte-identical.
        _ut = _render_undertested(getattr(res, "intelligence_undertested", None) or [])
        if _ut:
            res.composed_answer = (res.composed_answer or "").rstrip() + "\n\n" + _ut

        # ANSWER LAYOUT (flag ROSTER_ANSWER_LAYOUT): grounding-safe PRESENTATION pass. The compose call
        # drops layout under load, so long grounded answers come out as a wall of text; this reflows the
        # FULLY-ASSEMBLED answer into a scannable whiteboard layout (short paragraphs, bullets, tables,
        # arrow-flows) as a dedicated second pass. Fail-closed: reflow_for_scannability returns None (keep
        # the original) unless every [n] citation is preserved and no new hard token appears. OFF → never
        # called → composed_answer byte-identical.
        if self.answer_layout and (res.composed_answer or "").strip():
            try:
                # BudgetState is imported at module level; do NOT re-import locally here — a local import
                # would make BudgetState method-local across `ask` and break the earlier budget = BudgetState(...).
                from roster_kernel.research.layout import reflow_for_scannability
                _reflow = await reflow_for_scannability(
                    res.composed_answer, (self.layout_llm or self.llm), self.layout_prompt,
                    budget=BudgetState(max_calls=self.max_calls))
                if _reflow:
                    res.composed_answer = _reflow
            except Exception:   # noqa: BLE001 — presentation-only; never break the grounded answer
                pass
        return res

    def panel_roster(self) -> list[dict]:
        """The available specialists + their lens/expertise (for the UI roster view)."""
        return [{"id": getattr(s, "id", ""), "specialty": getattr(s, "specialty", ""),
                 "lens": getattr(s, "lens", ""), "focus": getattr(s, "focus", ""),
                 "default": getattr(s, "id", "") in set(self.panel_default_ids)}
                for s in self.panel_specialists]

    async def plan_panel(self, *, question: str) -> dict:
        """Phase 1: auto-select the specialists for this case (LLM triage) + return the full roster so
        the UI can show the proposed panel and let the user adjust. Fail-safe → the default set."""
        from roster_kernel.research.panel import plan_panel as _plan
        roster = self.panel_roster()
        selected = await _plan(question=question, roster=roster, llm=self.llm,
                               selection_prompt=self.panel_selection_prompt) if roster else []
        if not selected:   # triage empty/failed → default set (never leave the panel empty)
            by_id = {r["id"]: r for r in roster}
            selected = [{"id": i, "specialty": by_id[i]["specialty"],
                         "rationale": "core panel lens"} for i in self.panel_default_ids if i in by_id]
        return {"selected": selected, "roster": roster}

    async def ask_panel(self, *, question: str, tenant_id: str, workspace_id: str | None = None,
                        specialist_ids: list[str] | None = None, source_keys: list[str] | None = None,
                        history: list[dict] | None = None, rationales: dict | None = None,
                        images: list[dict] | None = None,
        pdf_docs: list[dict] | None = None, documents: list[dict] | None = None, on_event=None):
        """Ask-Panel (Alpha): run the selected specialists (or the default set) as parallel grounded
        loops and synthesize their pooled findings. Provides the domain-free orchestrator with a
        source-scoping callback so each specialist can prefer its own sources. `history` threads in as
        context for a follow-up; `images`/`documents` (uploaded attachments) are read ONCE (vision +
        document text) and shared as context across all specialists — same contract as ask()."""
        from roster_kernel.research.panel import run_panel
        roster = {getattr(s, "id", ""): s for s in self.panel_specialists}
        ids = [i for i in (specialist_ids or list(self.panel_default_ids)) if i in roster]
        specialists = [roster[i] for i in ids] or list(self.panel_specialists)
        # ATTACHMENT CONTEXT (parity with ask()): describe images ONCE + gather document text, then share
        # that context with every specialist (never re-describe per lens; never a citable finding).
        visual_obs = ""
        if images and self.vision_prompt:
            from roster_kernel.research.vision import observe_images
            try:
                visual_obs = await observe_images(llm=self.llm, vision_prompt=self.vision_prompt,
                                                  images=images, budget=BudgetState(max_calls=4))
            except Exception:   # noqa: BLE001 — a failed vision read must not break the panel
                visual_obs = ""
        _parts = []
        if visual_obs:
            _parts.append("IMAGE (automated visual description):\n" + visual_obs)
        if pdf_docs and self.report_prompt:
            from roster_kernel.research.vision import read_documents
            try:
                _reads = await read_documents(llm=self.llm, report_prompt=self.report_prompt,
                                              pdfs=pdf_docs, budget=BudgetState(max_calls=4))
            except Exception:   # noqa: BLE001
                _reads = []
            _rn = {r["name"] for r in _reads}
            for r in _reads:
                _parts.append(f"DOCUMENT — {r['name']} (structured digest, read natively from the file):\n{r['digest']}")
            for pdf in pdf_docs:
                fb = (pdf.get("text_fallback") or "").strip()
                if pdf.get("name") not in _rn and fb:
                    _parts.append(f"DOCUMENT — {pdf.get('name') or 'document'} (text-layer extraction):\n{fb[:20000]}")
        elif pdf_docs:
            for pdf in pdf_docs:
                fb = (pdf.get("text_fallback") or "").strip()
                if fb:
                    _parts.append(f"DOCUMENT — {pdf.get('name') or 'document'} (text-layer extraction):\n{fb[:20000]}")
        for d in documents or []:
            txt = (d.get("text") or "").strip()
            if txt:
                _parts.append(f"DOCUMENT — {d.get('name') or 'document'} (user-provided text):\n{txt}")
        attachment_context = "\n\n".join(_parts)
        if visual_obs and on_event is not None:
            try:
                await on_event({"type": "visual_observation", "text": visual_obs})
            except Exception:   # noqa: BLE001
                pass
        # FOLLOW-UP RESOLUTION (same as ask()): rewrite an elliptical follow-up ("what if they also have
        # gout?") into a SELF-CONTAINED question carrying the case subject, BEFORE the specialists run — so
        # every specialist's retrieval + reasoning inherits the full context, not just a raw fragment.
        if history:
            try:
                r = await self._resolve_followup(question, history, allow_clarify=False)
                if r and r.core_query and r.core_query != question:
                    question = r.core_query
                    if on_event is not None:
                        try:
                            await on_event({"type": "resolved_question", "question": question})
                        except Exception:   # noqa: BLE001
                            pass
            except Exception as e:   # noqa: BLE001 — fail-safe: keep the original follow-up
                import logging
                logging.getLogger(__name__).warning("panel follow-up resolution failed: %r", e)
        # episodic memory on: a follow-up should build on findings the panel already established
        history_context = build_history_context(history, answer_focus=True) or ""

        def make_retrievers(spec_source_keys):
            # a specialist's preferred sources ∩ the request's chosen sources (None = all)
            keys = spec_source_keys if spec_source_keys else source_keys
            return self._split_retriever(keys)

        return await run_panel(
            question=question, specialists=specialists, llm=self.llm, embedder=self.embedder,
            make_retrievers=make_retrievers, tenant_id=tenant_id, workspace_id=workspace_id,
            synthesis_directive=self.panel_synthesis_directive or "", history_context=history_context,
            attachment_context=attachment_context, rationales=rationales, chair_system_prompt=self.persona_prompt,
            classify_evidence=self.classify_evidence, evidence_ranker=self.evidence_ranker,
            evidence_fitness=self.evidence_fitness, evidence_identity=self.evidence_identity,
            claim_congruence=self.claim_congruence,
            panel_dedup=self.panel_dedup, panel_contract=self.panel_contract,
            contract_prompt=self.contract_prompt,
            panel_enumerative_addendum=self.panel_enumerative_addendum,
            panel_decision_addendum=self.panel_decision_addendum,
            on_event=on_event)

    async def _resolve_followup(self, question: str, history: list[dict],
                                *, allow_clarify: bool) -> "FollowupResolution":
        """Resolve a conversational FOLLOW-UP against the conversation in ONE structured LLM call
        (factra's Conversation-Manager pattern). Returns a FollowupResolution with:
          - core_query: the follow-up rewritten as a SELF-CONTAINED question (subject made explicit);
          - subject: the carried subject/entity (transparency + future gating);
          - needs_clarification + clarification: when the follow-up is genuinely ambiguous or has
            multiple plausible subjects (only honored when allow_clarify) — ask, don't guess.
        Sees ONLY the conversation (never the corpus), so it can inject no retrieved content —
        resolving the referent is a coreference judgment, the LLM's job (Rule 18). Fail-safe: any
        error → the original question, no clarification (never worse than today)."""
        from pydantic import BaseModel

        class _Resolution(BaseModel):
            core_query: str
            subject: str = ""
            needs_clarification: bool = False
            clarification: str = ""
            operate_on_prior: bool = False

        convo = []
        for t in (history or []):
            qy = (t.get("question") or "").strip()
            an = (t.get("answer") or "").strip()
            if qy:
                convo.append(f"Q: {qy}\nA: {an[:800]}" if an else f"Q: {qy}")
        if not convo:
            return FollowupResolution(core_query=question)
        clause = (
            " (4) If the follow-up is genuinely AMBIGUOUS — the subject is unclear OR there are MULTIPLE "
            "plausible subjects it could refer to — set needs_clarification=true and put ONE short, "
            "specific clarifying question in `clarification` (naming the candidate options), and set "
            "core_query to your best-guess standalone question anyway."
            if allow_clarify else
            " (4) Do NOT ask for clarification; always return your best-guess standalone core_query.")
        sys = ("You resolve a user's LATEST question in a medical research chat into a single, "
               "SELF-CONTAINED question, using the prior conversation to fill in the subject the latest "
               "question leaves implicit. You never add facts — you only make the question standalone.")
        user = (
            "CONVERSATION SO FAR:\n" + "\n\n".join(convo) + "\n\n"
            f"LATEST QUESTION: {question}\n\n"
            "Set core_query to LATEST QUESTION rewritten as ONE standalone question naming its subject "
            "explicitly (e.g. 'What dose?' after establishing co-trimoxazole for PCP prophylaxis → "
            "'What is the dose of co-trimoxazole for Pneumocystis pneumonia prophylaxis?'), and `subject` "
            "to the carried subject/entity. RULES: (1) If LATEST QUESTION is ALREADY self-contained, set "
            "core_query to it VERBATIM. (2) If it CHANGES the topic (a new subject), set core_query "
            "VERBATIM — do not graft on the old subject. (3) Carry ONLY the subject the latest question is "
            "implicitly about; do NOT add facts, answers, doses, or details from the prior answers. "
            "(3b) DO carry any patient CONSTRAINT the conversation established that still applies — age/"
            "pediatric/elderly, pregnancy, renal or hepatic impairment, an allergy, a comorbidity — into "
            "core_query (e.g. after 'in a patient with renal impairment', a later 'what about the dose?' → "
            "'... dose ... in a patient with renal impairment'), unless the latest question overrides it. "
            "(5) If LATEST QUESTION asks to TRANSFORM the PREVIOUS answer itself rather than seek new "
            "information — e.g. 'summarize that', 'shorten it', 'explain the second point', 'put it in a "
            "table', 'in one sentence' — set operate_on_prior=true (and core_query VERBATIM)."
            + clause)
        planner = self.planner_llm or self.llm
        try:
            res = await planner.complete(system=sys, messages=[{"role": "user", "content": user}],
                                         response_format=_Resolution, max_tokens=800)
            r = res.parsed
            cq = (r.core_query or "").strip()
        except Exception:
            return FollowupResolution(core_query=question)   # fail-safe → original
        # Structural guards (code owns structure): drop an empty/runaway rewrite back to the original.
        if not cq or len(cq) > max(160, 5 * len(question)):
            cq = question
        clar = (r.clarification or "").strip()
        return FollowupResolution(
            core_query=cq, subject=(r.subject or "").strip(),
            needs_clarification=bool(allow_clarify and r.needs_clarification and clar),
            clarification=clar if (allow_clarify and r.needs_clarification) else "",
            operate_on_prior=bool(r.operate_on_prior))

    async def _transform_prior(self, request: str, prior_answer: str) -> str:
        """Apply a user's TRANSFORM request ('summarize that', 'shorten', 'explain point 2', 'as a
        table') to the PREVIOUS answer, adding NO new facts. The prior answer was already grounded;
        this only reshapes it, so it is provenance-safe by construction (no retrieval, no new claims).
        Returns "" on failure → the caller falls back to normal research."""
        sys = ("You reshape a previous answer per the user's request. Use ONLY information already in "
               "the previous answer — add NO new facts, numbers, drugs, or claims. If the request asks "
               "for something not in the previous answer, say that briefly rather than inventing it.")
        user = (f"PREVIOUS ANSWER:\n{prior_answer[:6000]}\n\n"
                f"USER REQUEST: {request}\n\n"
                "Produce the reshaped answer as clean prose (no [n] citation markers — the sources are "
                "shown with the previous answer).")
        try:
            comp = await self.llm.complete(system=sys, messages=[{"role": "user", "content": user}],
                                           response_format=_PlainAnswer,
                                           max_tokens=min(8000, max(2000, len(prior_answer) // 2)))
            return (comp.parsed.text or "").strip()
        except Exception:
            return ""

    async def explain(self, *, question: str, answer: str) -> str:
        """On-demand plain-language rephrasing of a grounded answer (adds no new facts)."""
        if not self.layman_prompt:
            return ""
        from roster_kernel.research.explain import explain_for_layperson
        return await explain_for_layperson(
            llm=self.llm, layman_prompt=self.layman_prompt, question=question, answer=answer)

    async def plan_gaps(self, *, question: str, answer: str, coverage_gaps: list[str]):
        """On-demand plan of what to ADD to the corpus so an under-evidenced question could be
        answered — actionable ingest jobs (over THIS deployment's connectors) + gold-source
        recommendations. Returns None when gap-healing isn't configured for the vertical."""
        if not self.gap_prompt or not self.connectors:
            return None
        from roster_kernel.research.gap_planner import plan_gap_fill
        return await plan_gap_fill(
            llm=self.llm, gap_prompt=self.gap_prompt, question=question, answer=answer,
            coverage_gaps=coverage_gaps, available_connectors=list(self.connectors.keys()))

    async def suggest(self, *, question: str, answer: str, history: str = "") -> list[str]:
        """On-demand suggested follow-up questions that deepen discovery. [] when unavailable."""
        if not self.suggest_prompt:
            return []
        from roster_kernel.research.suggest import suggest_followups
        return await suggest_followups(
            llm=self.llm, suggest_prompt=self.suggest_prompt,
            question=question, answer=answer, history=history)

    async def explain_terms(self, *, question: str, answer: str) -> list:
        """On-demand key-term explanations for an answer (definitional, with related-term
        edges for the vocabulary web). [] when the vertical supplies no terms directive."""
        if not self.terms_prompt:
            return []
        from roster_kernel.research.terms import explain_key_terms
        return await explain_key_terms(
            llm=self.llm, terms_prompt=self.terms_prompt, question=question, answer=answer)

    async def visualize(self, *, question: str, answer: str) -> list:
        """On-demand conceptual visuals (flow/tree/timeline) restructuring a grounded answer, every
        element quote-anchored to the answer. [] when the vertical supplies no visuals directive or
        nothing qualifies."""
        if not self.visuals_prompt:
            return []
        from roster_kernel.research.visuals import visualize_answer
        return await visualize_answer(
            llm=self.llm, visuals_prompt=self.visuals_prompt, question=question, answer=answer)

    async def explain_term(self, *, term: str, context: str = ""):
        """On-demand single-term explanation (glossary navigation). None when unavailable."""
        if not self.terms_prompt:
            return None
        from roster_kernel.research.terms import explain_single_term
        return await explain_single_term(
            llm=self.llm, terms_prompt=self.terms_prompt, term=term, context=context)

    async def triage(self, *, transcript: list[dict], force_ready: bool = False,
                     v2: bool = False) -> dict:
        """Guided-intake / triage: run ONE clarifying turn over the transcript ([{role, text}]) and return
        either the next question (status="ask") or a crisp refined question + recommended route
        (status="ready"). `force_ready` (caller's turn cap) coerces a route. {} when the vertical has no
        triage prompt (feature effectively off). `v2` selects the vertical's intake-v2 directive + the
        TriageTurnV2 schema (register/case_facts/retrieval_terms); without a v2 directive it falls back
        to v1 behavior, byte-identical. Never answers the medical question — only narrows + routes."""
        if not self.triage_prompt:
            return {}
        from roster_kernel.research.triage import run_triage_turn
        use_v2 = bool(v2 and self.triage_prompt_v2)
        roster = ", ".join(f"{s.get('specialty','')}" for s in self.panel_roster()) if self.panel_specialists else ""
        turn = await run_triage_turn(
            llm=self.llm,
            triage_prompt=(self.triage_prompt_v2 if use_v2 else self.triage_prompt),
            transcript=transcript, roster_summary=roster, force_ready=force_ready,
            schema_v2=use_v2)
        return turn.model_dump()

    async def refine(self, *, question: str) -> list[str]:
        """Pre-answer refinements: 0, or a few DISTINCT sharper standalone questions to choose from.
        [] when the vertical has no refine prompt, the question is already precise, or on error. Uses
        the FAST planner model — it only proposes questions the user picks; it never enters a gate."""
        if not self.refine_prompt:
            return []
        from roster_kernel.research.refine import refine_question
        return await refine_question(
            llm=self.planner_llm or self.llm, refine_prompt=self.refine_prompt, question=question)

    async def search(
        self,
        *,
        question: str,
        tenant_id: str,
        workspace_id: str | None = None,
        source_keys: list[str] | None = None,
        k: int = 8,
        facets: dict | None = None,
    ):
        """Retrieval only — no LLM. Returns ranked evidence blocks. Works with just
        the embedder (OpenAI), so it's available even when the answer LLM isn't.
        `facets` is an optional HARD filter (generic dimension→allowed-values), e.g.
        `{"source_kind": ("paper","preprint")}` to retrieve only research. Omitted → no filter."""
        from roster_kernel.contract.dto import RetrievalRequest
        qv = list(self.embedder.embed([question])[0])
        return await self._retriever(source_keys).search(RetrievalRequest(
            query=question, tenant_id=tenant_id, workspace_id=workspace_id,
            query_embedding=qv, k=k, facets=facets or {}))
