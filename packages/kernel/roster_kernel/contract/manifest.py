"""VerticalManifest — the single object a vertical package exposes.

A deployment activates exactly one vertical (O3). The kernel discovers installed
verticals via the `roster.verticals` entry-point group and builds its registries
from the manifest — no kernel edits per vertical.

Slots are TYPED against contract/protocols.py (not `Any`), so `VerticalConformance`
can assert structural conformance and a new domain cannot be bolted on with a
mis-shaped component. Slots are Optional/empty so a partial manifest is valid in
early phases; conformance enforces completeness per phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .protocols import (
    CitationVerifier,
    Connector,
    GatingPolicy,
    Parser,
    Persona,
    RetrievalSource,
    UIContract,
)


@dataclass(frozen=True)
class VerticalManifest:
    # Identity
    name: str

    # Taxonomy / scope (declared vocabulary — plain data)
    entity_types: tuple[str, ...] = ()          # P1
    scope_dimensions: tuple[str, ...] = ()      # P2 (facet keys the vertical uses)

    # Acquisition (P1)
    connectors: dict[str, Connector] = field(default_factory=dict)
    parsers: tuple[Parser, ...] = ()

    # Retrieval + policy (P2)
    retrieval_sources: dict[str, RetrievalSource] = field(default_factory=dict)
    gating_policy: GatingPolicy | None = None
    citation_verifier: CitationVerifier | None = None

    # Language + authority (P3)
    persona: Persona | None = None
    authority_policy: object | None = None      # typed in P3 (authority contract)
    structured_tools: dict[str, object] = field(default_factory=dict)
    extraction_schema: object | None = None

    # Presentation (P4)
    ui: UIContract | None = None
    deliverable_kinds: dict[str, object] = field(default_factory=dict)
    # Optional vertical-supplied directive that shapes the synthesized answer's
    # STRUCTURE (e.g. markdown sections a domain audience expects). Generic, opaque
    # prose — the kernel only threads it into the grounded-compose step; all domain
    # wording lives in the vertical. None → the kernel's default flat-prose answer.
    answer_format: str | None = None
    # Optional ENHANCED answer_format used only when the clinical-synthesis flag is ON (Rule 20 A/B
    # seam). A sharper, still-adaptive variant of `answer_format` — same section set, tighter
    # in-section discipline (scope, evidence-status, surrogate-vs-clinical, no citation stacking).
    # Opaque prose; the kernel threads it in exactly like `answer_format`. None → fall back to
    # `answer_format` (so the flag is a no-op for verticals that don't supply this).
    clinical_answer_format: str | None = None
    # Optional PATIENT-audience answer_format, selected per request when the asker chooses the
    # patient view (flag ROSTER_PATIENT_MODE). Composes from the SAME verified findings — same gates —
    # but in plain patient-facing language with the accuracy guardrails baked in. Opaque prose threaded
    # exactly like `answer_format`. None → the vertical has no patient view (patient mode falls back to
    # the clinician directive), so the flag is a safe no-op for verticals that don't supply this.
    patient_answer_format: str | None = None
    # Optional SINGLE golden-answer compose directive. When the golden-answer flag is ON, the app boundary
    # REPLACES `answer_format` with this one directive and forces every other answer-shaping layer OFF, so
    # the answer is one clean freeform brief with no narrated scaffolding. Opaque prose threaded exactly
    # like `answer_format` (all domain vocabulary lives here — kernel litmus). None → the flag is a no-op
    # for verticals that don't supply it.
    golden_answer_directive: str | None = None
    # CONTRACT-RENDERED COMPOSE (ROSTER_CONTRACT_COMPOSE) — voice ⟂ shape. The successor to the flat
    # golden directive: instead of ONE fixed directive imposing one shape, compose is assembled as the
    # universal VOICE (`contract_compose_voice`) PLUS the SHAPE the derived contract asks for
    # (`contract_compose_shapes[mode]`, else the '' / default key). The kernel selects by the contract
    # mode (a structural key) and, for an enumerative shape, appends the concrete items+dimensions from
    # the contract — it never parses the opaque prose. Both None → the flag is a no-op (byte-identical).
    contract_compose_voice: str | None = None
    contract_compose_shapes: dict | None = None   # {mode: directive}; missing mode → contract_compose_default
    contract_compose_default: str | None = None   # the shape for decision/analytical/narrow (unmapped modes)
    # Derivation prompt used ONLY on the contract-compose path — classifies the ANSWER SHAPE (can emit
    # "enumerative", which the base prompt cannot). None → fall back to the base contract_prompt.
    contract_compose_prompt: str | None = None
    # Optional VISUALIZATION guidance appended to the compose directive when the answer-visuals flag is
    # on — pushes the answer toward comparison tables / ranked options / pros-cons, strictly from the
    # verified findings (never fabricated structure). Opaque prose threaded like `answer_format`.
    visual_guidance: str | None = None
    # Optional CHART-emission guidance appended to the compose directive when the answer-charts flag is
    # on — lets compose populate a grounded bar chart (validated in code). Opaque prose.
    chart_guidance: str | None = None
    # Optional REASONING-READ guidance appended to the compose directive when the reasoning-read flag is
    # on — lets compose emit a typed interpretation layer (tension/gap/assumption/implication/what-would-
    # change) + a 3-dimension confidence read, both validated in code (no new facts). Opaque prose.
    reasoning_format: str | None = None
    # Optional PATIENT-facing variant of `reasoning_format`, appended to the patient directive when the
    # reasoning-read flag is on AND audience=patient — same structured fields + code validation, plain
    # language. None → patient answers reuse no reasoning layer (safe no-op).
    patient_reasoning_format: str | None = None
    # Optional PRE-ANSWER refinement directive: propose sharper standalone versions of a fresh question
    # for the user to pick from (express refinement). Opaque prose; kernel owns the mechanics.
    refine_prompt: str | None = None
    # Optional GUIDED-INTAKE / triage directive: a short clarifying conversation that converges on a crisp
    # question and recommends a route (Q&A vs Panel). Opaque prose; kernel owns the turn mechanics + cap.
    triage_prompt: str | None = None
    # Optional Guided Intake v2 directive (register choice + structured case intake + clinical-register
    # rewrite). Selected only when the caller requests v2; None → v2 request falls back to v1. Opaque.
    triage_prompt_v2: str | None = None
    # Optional ALTERNATE "reasoned" engine (A/B duel arm): a pre-retrieval scaffold directive (coverage
    # as QUESTIONS, never conclusions) + a decision-gated compose directive. Opaque prose.
    reasoned_scaffold_prompt: str | None = None
    reasoned_answer_format: str | None = None
    # Optional OPT-IN complementary/integrative section: a compose addendum + a retrieval-steering hint,
    # applied only when the user explicitly opts in for a question. Opaque prose.
    integrative_prompt: str | None = None
    integrative_query_hint: str | None = None
    # Optional ALTERNATIVE-modality directive + retrieval hint (flag ROSTER_MODALITY_MODE): the
    # compose/query layer for an answer centered on complementary & alternative medicine, with
    # responsible per-indication evidence labeling. The app supplies the modality=alternative corpus
    # scope; the vertical supplies this prose. None → the Alternative mode has no compose steering.
    alt_directive: str | None = None
    alt_query_hint: str | None = None
    # Optional UNDERSTANDING engine (Discover·Understand·Act middle): causal-model compose contract +
    # mechanism-steering retrieval hint, selected by the dynamic router for WHY/HOW questions.
    understanding_answer_format: str | None = None
    understanding_query_hint: str | None = None
    # Optional DEEP-SYNTHESIS compose format (flag ROSTER_DEEP_SYNTHESIS): a synthesis-first directive
    # (core thesis → tensions → second-order implications → mechanism, grounded-derivation-woven) selected
    # for non-lookup questions when the app's deep_synthesis flag is on. Domain-free opaque slot the kernel
    # only threads into compose (via the service's deep_answer_format); ALL domain wording lives in the
    # vertical. Always safe to set as inert data — the flag + question kind gate its use. None → unavailable.
    deep_answer_format: str | None = None
    # Optional AUTHORITY-BASIS compose directive (flag ROSTER_AUTHORITY_BASIS): a floor instruction telling
    # the composer to ground facts in the highest-tier source and treat opinion/blog/social/unknown as
    # supplementary signal, never the sole basis for a stated fact. Domain-free opaque slot the kernel only
    # threads into compose (via the service's authority_basis_directive); ALL domain wording lives in the
    # vertical. Always safe to set as inert data — the flag gates its use. None → nothing appended.
    authority_basis_directive: str | None = None
    # Optional PARAMETRIC-LED draft directive (flag ROSTER_PARAMETRIC_LED): the system prompt for the
    # pre-retrieval `draft_prior` call — the model drafts an answer OUTLINE + separates checkable FACTS
    # (verified against retrieval in T2) from its REASONING. Domain-free opaque slot the kernel only
    # threads into draft_prior (via the service's prior_draft_prompt); ALL domain wording lives in the
    # vertical. Always safe to set as inert data — the flag + routing gate its use. None → unavailable.
    prior_draft_prompt: str | None = None
    # Optional INTELLIGENCE-CORE draft directive (flag ROSTER_INTELLIGENCE_CORE): the system prompt for the
    # pre-retrieval `draft_intelligence` call — the model states a short analytical FRAME + emits 2-3
    # COMPETING hypotheses in a flat line protocol (each with a for/against search query + a falsifier).
    # Domain-free opaque slot the kernel only threads into draft_intelligence (via the service's
    # intelligence_draft_prompt); ALL domain wording lives in the vertical. Always safe to set as inert
    # data — the flag + routing gate its use. None → unavailable.
    intelligence_draft_prompt: str | None = None
    # Optional vertical-supplied instruction for the VISION pre-step: how to DESCRIBE a
    # user-uploaded image (color/shape/borders/texture/distribution), producing a labeled
    # visual observation — never a diagnosis. Opaque prose; the kernel only threads it into
    # the vision call. None → no vision pre-step (images ignored).
    vision_prompt: str | None = None
    # Optional vertical-supplied instruction for the on-demand LAYMAN re-explanation (rephrase
    # a grounded answer for a non-expert, adding no new facts). None → feature unavailable.
    layman_prompt: str | None = None
    # Optional vertical-supplied instruction for the GAP-FILL planner: given a question the corpus
    # could not fully answer + its coverage gaps + the available connector KEYS, propose concrete
    # ingest jobs ({connector, query, limit}) plus gold-standard sources to recommend. Describes
    # what each connector fetches + what high-quality evidence looks like in this domain. Opaque
    # prose; the kernel only threads it into the planner call. None → self-healing unavailable.
    gap_prompt: str | None = None
    # Optional vertical-supplied instruction for SUGGESTED FOLLOW-UP questions: given a Q&A, propose
    # a few next questions that deepen discovery, understanding, and action for this domain. Opaque
    # prose; the kernel only threads it into the suggest call. None → no suggestions surfaced.
    suggest_prompt: str | None = None
    # Optional vertical-supplied instruction for KEY-TERM explanations: extract the specialist
    # terms an answer used and explain each (plain definition, purpose, application) plus its
    # RELATED terms — the edges of the domain's vocabulary web. Definitional only, never new
    # claims about the user's case. Opaque prose; the kernel only threads it into the terms
    # call. None → the term-glossary feature is unavailable.
    terms_prompt: str | None = None
    # Optional vertical-supplied instruction for POST-HOC answer VISUALIZATION: restructure a finished
    # grounded answer into conceptual/structural visual primitives (flow/tree/timeline), every element
    # carrying a verbatim quote from the answer (no new facts). Spatial/structural only — never numeric
    # charts (the inline path) or prose tables (visual_guidance). Opaque prose; the kernel owns the
    # schema + grounding validation. None → the add-visuals feature is unavailable.
    visuals_prompt: str | None = None
    # Optional whitelist of TRUSTED web-search domains (peer-reviewed journals, guideline bodies,
    # authoritative gov/db sources). When set, web search is restricted to these — the corpus is
    # augmented only with high-quality sources, never the open web. Empty → open web.
    web_domains: tuple[str, ...] = ()
    # Optional domain → facets map stamped on web-retrieved blocks (venue authority as structural
    # metadata: e.g. a guideline body's pages carry pub_type "practice guideline"), so the vertical's
    # evidence classifier and authority pyramid grade web evidence like corpus evidence. Empty → none.
    web_domain_facets: dict = field(default_factory=dict)
    # Optional OPEN-WEB quality-screen prompt (flag ROSTER_WEB_ENTITY_OPEN): the system prompt for the
    # ONE batched LLM judge that keeps usable/relevant open-web pages and drops junk when a leg reaches
    # past `web_domains`. Opaque prose — ALL domain vocabulary (what "official/reputable/technical/junk"
    # means) lives here; the kernel only threads it into `screen_open_web_hits`. None → no open-web
    # screen is available (the entity-open leg falls closed), a safe no-op.
    web_quality_prompt: str | None = None
    # Optional vertical-supplied deep company reader config: opaque facet query templates, domain
    # resolution prompt, bounded retrieval knobs, and compose attribution addendum. The kernel treats
    # it as data and never interprets the vertical vocabulary inside it.
    company_reader: dict = field(default_factory=dict)
    # Optional vertical-supplied deep person reader config: opaque name-based facet query templates,
    # profile-link preferences, bounded retrieval knobs, and compose attribution addendum. The kernel
    # treats it as data and never interprets the vertical vocabulary inside it.
    person_reader: dict | None = None
    # Optional FRESHNESS policy (flag ROSTER_FRESHNESS_RANKING): how strongly recency re-orders the
    # verified-claim pool for THIS vertical. Domain-free opaque dict the kernel reads by key:
    #   {"min_rank": int, "weight": float, "horizon_years": int}
    # `min_rank` = the lowest evidence tier the recency term applies to (0 = ALL tiers; a fast-moving
    # vertical wants 0 so a 2026 paper/repo/news outranks a 2024 one; medical wants the controlling
    # tier so a landmark trial never loses to a newer small one). `weight` = bounded additive recency
    # boost; `horizon_years` = linear decay-to-zero age. Empty → recency stays the kernel default
    # (controlling-tier only, 0.10 / 12yr), i.e. byte-identical to today when the flag is off.
    freshness_policy: dict = field(default_factory=dict)
    # Optional ANSWER-CONTRACT profiles (flag ROSTER_ANSWER_CONTRACT): the per-question evidence REGIME
    # map. The vertical's `contract_prompt` classifies a question into a `stance` string; this map
    # supplies each stance's opaque policy dict the kernel threads generically:
    #   {stance: {"recency": {min_rank,weight,horizon_years}|None, "suppress_authority": bool,
    #             "web_recency_days": int|None, "planner_steer": str, "answer_directive": str}}
    # so ONE classification customizes retrieval + ranking + compose (e.g. "current" → recency-first
    # news; "established" → authority-first benchmarked/reviewed). The kernel interprets NONE of the
    # stance names — a legal/biotech vertical supplies its own. Empty → the contract sets no regime.
    answer_profiles: dict = field(default_factory=dict)
    # Optional Evidence Pulse watch-topic prompts (LLM-owned judgment, Rule 18): suggest watchable
    # subjects for a Q&A / canonicalize a free-text topic — both against the stable topic registry
    # (repeated runs must converge on the same canonical strings, never variants). None → the
    # watch picker falls back to raw free-text only.
    # Optional NATIVE document-reading directive (uploaded PDFs → faithful structured digest;
    # the model reads the raw file so report tables keep their associations). None → text-layer only.
    report_prompt: str | None = None
    watch_topic_prompt: str | None = None
    watch_canonize_prompt: str | None = None
    watch_suggest_prompt: str | None = None    # cross-session watch suggestions (recurring subjects)
    # Optional supersession-judge prompt (shadow-mode edition detection; approval-gated per spec A4)
    supersession_judge_prompt: str | None = None
    # Optional seed vocabulary for the canonical topic registry (e.g. the vertical's covered-
    # condition names) — loaded once into the registry on first Pulse topic use.
    watch_topic_seed: tuple = ()
    # Optional CURATOR-DECLARED document lineage (Evidence Pulse P0): a tuple of
    # {old_document_id, new_document_id, relation, subjects} dicts in the kernel currency
    # vocabulary (superseded_by · retracted · amended_by · clarified_by). Highest-confidence,
    # zero-LLM change source; the kernel's CurrencyStore sweeps it into approved events + stamps.
    lineage: tuple = ()
    # Optional Grounded Relationship Graph vocabulary + curated edges (learnings/knowledgegraph.md
    # P0): `graph_relations` is the typed-edge vocabulary the kernel validates writes against;
    # `graph_edges` are curator-declared {subject, relation, object, context_topic?, label,
    # confidence, note?} dicts (endpoints = canonical registry labels), born ACTIVE on sync.
    # Empty → no graph for this vertical.
    graph_relations: tuple = ()
    graph_edges: tuple = ()
    # Optional LLM question→graph-topic mapping directive (v3-P1): shown the closed edge-topic
    # vocabulary; used ONLY when structural containment matches nothing. None → containment-only.
    graph_map_prompt: str | None = None
    # Optional COUNTRY PROFILES (kernel-neutral; e.g. an IN locale): {code: {"context_fn":
    # callable(question)->planner-only context str, "directive": compose addendum str}}.
    # The app resolves the active profile per user; the kernel just threads the strings.
    country_profiles: dict = field(default_factory=dict)
    # Optional SECTOR PROFILES (kernel-neutral; same opaque shape as country_profiles): a per-question
    # SUBJECT-scope map {code: {"context_fn": callable(question)->planner-only context str, "directive":
    # compose addendum str, "connectors": tuple?, "web_domains": tuple?, "vocab_seed": tuple?}}. Lets a
    # single vertical answer across sub-domains (the app resolves the active sector per request and threads
    # the strings). The kernel never parses it. Empty → no sector scoping. Names NO domain concept here.
    sector_profiles: dict = field(default_factory=dict)
    # Optional ANSWER MODES (kernel-neutral): {mode_name: compose_directive_str} — an analytical LENS
    # the caller selects per request (e.g. investor vs acquirer), threaded as an extra compose addendum.
    # Opaque prose; the kernel never parses it. Empty → only the default answer directive. Names no
    # domain concept here.
    answer_modes: dict = field(default_factory=dict)
    # Optional extraction LENSES (domain vocabulary) for the claims-first pipeline: the aspects the
    # extractor should cover per atom (e.g. interventions, outcomes, safety). Passed as a checklist
    # in ONE extraction call (not fanned out). Empty → generic "extract every fact". Kernel-neutral.
    extraction_lenses: tuple[str, ...] = ()

    # Ask-Panel (Alpha): the vertical's specialist roster (duck-typed configs with .id/.specialty/.lens/
    # .focus/.source_keys) + the grounded-synthesis directive. Empty → no panel for this vertical.
    panel_specialists: tuple = ()
    panel_default_ids: tuple = ()          # which specialists the default panel runs
    panel_synthesis_directive: str | None = None
    # Optional PANEL AUTO-SELECTION system prompt: the instruction shown to the chair-LLM that picks
    # WHICH specialists a free-text case needs. Opaque prose; ALL domain vocabulary lives here. None →
    # the kernel uses a domain-neutral default ("chair of an expert research panel"). Lifting this out
    # of the kernel is what keeps plan_panel domain-free.
    panel_selection_prompt: str | None = None
    panel_examples: tuple = ()             # sample multi-specialty cases seeded into the panel intake
    # Optional PANEL synthesis addenda (flag ROSTER_PANEL_CONTRACT — P1 decision synthesis).
    # APPENDED by the kernel to the panel synthesis directive ONLY when the panel's shared
    # QuestionContract fires the matching route (enumerative + ≥2 covered entities → the
    # enumerative addendum; exploratory + ≥2 covered axes → the decision addendum). Opaque prose —
    # ALL domain vocabulary (grid shape, columns, attribution rules) lives here; the kernel never
    # parses it. None → that route never fires for this vertical.
    panel_enumerative_addendum: str | None = None
    panel_decision_addendum: str | None = None

    # Optional QUESTION-CONTRACT derivation directive (Evidence Contract stage 3, flag
    # ROSTER_QUESTION_CONTRACT): instructs ONE small LLM call to decide whether a question demands
    # ENUMERATING candidate items, and if so to name the concrete candidate entities a practitioner
    # would consider (including reasonable defaults the asker didn't name) plus the REQUIRED
    # evidence axes (safety/risk and interaction axes where applicable). ALL domain vocabulary
    # lives HERE; the kernel derives, expands to retrieval legs, and slot-matches generically.
    # None → no contract is ever derived (the flag is a safe no-op for this vertical).
    contract_prompt: str | None = None
    # LANDSCAPE-COVERAGE derivation prompt (flag ROSTER_LANDSCAPE_COVERAGE). Same one-call contract, but
    # it is ALLOWED to return mode="enumerative" with the conceptual CATEGORIES as `entities` for a
    # "map the landscape / examine all X / cluster" ask — so the kernel fans retrieval out per category
    # (entity×axis legs) instead of a few narrow searches. The app swaps `contract_prompt` to this and
    # forces question_contract="steer" only when the flag is on. None → not offered (byte-identical).
    landscape_contract_prompt: str | None = None
    # REFLECTION addendum (flag ROSTER_REFLECTION). Opaque text the app appends to whichever contract prompt
    # is active so the ONE derivation call also returns the question's heart-of-intent (intent /
    # intent_confidence / answer_brief) used to steer retrieval + compose. None → not offered (byte-identical).
    reflection_contract_addendum: str | None = None
    # Optional ENUM-PROBE addendum (flag ROSTER_ENUM_ENTITY_PROBE): APPENDED by the app to the active
    # contract prompt so the derivation ALSO proposes `probe_entities` (candidate row instances that
    # SEED targeted retrieval, never rows) for an enumerative "table of the main X" ask. Domain-free
    # slot (opaque string). None → never appended (byte-identical).
    probe_entities_contract_addendum: str | None = None
    # Optional ENUMERATIVE-COMPOSE addendum (Evidence Contract stage 4, flag
    # ROSTER_ANSWER_MODE_ROUTING): APPENDED by the kernel to the active compose directive ONLY when
    # the derived QuestionContract says enumerative AND ≥2 contract entities hold slot-matched
    # verified claims (never on the pre-retrieval contract alone — panel A3). Opaque prose — ALL
    # domain vocabulary (per-item table shape, safety-pairing rules, what counts as "context")
    # lives here; the kernel never parses it. None → stage-4 routing never fires for this vertical.
    enumerative_compose_addendum: str | None = None
    # Optional STRUCTURAL evidence-tier classifier: (source_key, facets) -> evidence_kind str (Rule 18 —
    # maps computable per-source metadata onto the authority pyramid, no semantic judgment). Used to
    # stamp each verified claim's evidence tier (for evidence-fitness ranking + the eval's evidence_floor).
    # None → tiers unavailable (evidence_kind stays ""), a safe no-op.
    evidence_classifier: object | None = None

    # Optional DISCOVERY entity resolver: callable(hit) -> (entity_name, entity_type) | None, telling
    # the kernel's aggregate_entities how to read WHICH entity a retrieved block is about (e.g. a
    # document's issuer, a project's owner). Powers the "who is working on X" scouting surface. Names no domain concept
    # (it's an opaque callable); None → discovery is unavailable for this vertical.
    discovery_entity_of: object | None = None

    # Held-out eval gold + vocab
    eval_gold: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("VerticalManifest.name must be a non-empty string")
