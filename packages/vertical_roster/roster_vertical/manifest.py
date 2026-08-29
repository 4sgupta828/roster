"""Assemble the tech VerticalManifest — the single object this vertical exposes.

Everything domain-specific (connectors, persona, authority pyramid, answer format, lenses,
sector profiles, UI, eval gold) is passed in HERE; the kernel only threads opaque strings and
duck-typed policies. Sectors are the `sector_profiles` map — one deployment answers across
AI/fintech/biotech… (see sectors.py). Analytical angles are `extraction_lenses` (see lenses.py).
"""
from __future__ import annotations

import os

from roster_kernel.contract.manifest import VerticalManifest

from . import discovery, entities, evidence_kind
from .answer_format import (TECH_ANSWER_FORMAT, TECH_DILIGENCE_SYNTHESIS_FORMAT,
                            TECH_VISUAL_GUIDANCE, TECH_CHART_GUIDANCE, TECH_REASONING_FORMAT,
                            TECH_AUTHORITY_BASIS_DIRECTIVE)
from .prior_draft import PRIOR_DRAFT_PROMPT
from .intelligence_draft import INTELLIGENCE_DRAFT_PROMPT
from .golden_answer import (GOLDEN_ANSWER_DIRECTIVE, GOLDEN_VOICE, CONTRACT_SHAPES,
                            SHAPE_DEFAULT)
from .terms import TECH_TERMS_PROMPT
from .visuals import TECH_VISUALS_PROMPT
from .authority import TechAuthorityPolicy
from .connectors import (ArxivConnector, CompaniesHouseConnector, CrossrefConnector, EdgarConnector,
                         EngBlogConnector,
                         ExpertFeedConnector, GdeltConnector, GithubConnector, HackerNewsConnector,
                         HuggingFaceConnector, LobstersConnector, NihReporterConnector, NsfConnector,
                         OpenAlexConnector,
                         OpenReviewConnector, PatentsViewConnector, PodcastConnector, RedditConnector,
                         SemanticScholarConnector, StackExchangeConnector, UsptoConnector,
                         WikidataConnector, WikipediaConnector, YcConnector)
from .use_case_lenses import USE_CASE_LENSES
from .answer_contract import (ANSWER_PROFILES, TECH_CONTRACT_PROMPT, TECH_CONTRACT_PROMPT_ENTITY,
                              TECH_CONTRACT_COMPOSE_PROMPT,
                              TECH_LANDSCAPE_CONTRACT_PROMPT, TECH_LANDSCAPE_CONTRACT_PROMPT_ENTITY,
                              TECH_REFLECTION_ADDENDUM, TECH_PROBE_ENTITIES_ADDENDUM)
from .company_reader import COMPANY_READER
from .person_reader import PERSON_READER
from .reasoned import (TECH_ADAPTIVE_ANSWER_FORMAT, TECH_ADAPTIVE_SCAFFOLD_PROMPT,
                       TECH_ADAPTIVE_SCAFFOLD_PROMPT_DEEP,
                       TECH_ANSWER_360_BLOCK, tech_closing_block, TECH_LANDSCAPE_COMPOSE_BLOCK,
                       answer_close_on, TECH_DEEP_SYNTHESIS_FORMAT, TECH_REASONED_ANSWER_FORMAT,
                       TECH_REASONED_SCAFFOLD_PROMPT, TECH_UNDERSTANDING_ANSWER_FORMAT,
                       TECH_UNDERSTANDING_QUERY_HINT, adaptive_format_on, answer_360_on,
                       landscape_coverage_on)
from .eval_gold import GOLD
from .freshness import TECH_FRESHNESS_POLICY
from .fixtures import sample_filings, sample_papers
from .gaps import TECH_GAP_PROMPT
from .ma import MA_DIRECTIVE
from .gating import TechGatingPolicy
from .lenses import EXTRACTION_LENSES
from .persona import TechPersona
from .scope import SCOPE_DIMENSIONS
from .sectors import SECTOR_PROFILES
from .source import TechRetrievalSource
from .suggest import tech_suggest_prompt
from .ui import TechUI
from .web_domains import TRUSTED_WEB_DOMAINS, WEB_DOMAIN_FACETS
from .web_quality import WEB_QUALITY_PROMPT


def web_entity_open_on() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_WEB_ENTITY_OPEN gates the entity-scoped open-web probe. When
    ON, the manifest swaps in the `subject_kind`-emitting contract prompt so the kernel can tell a
    single-entity diligence question apart. OFF → the original TECH_CONTRACT_PROMPT (byte-identical)."""
    return os.environ.get("ROSTER_WEB_ENTITY_OPEN", "").lower() in ("1", "true", "yes")


def company_reader_on() -> bool:
    """Flag (default OFF): ROSTER_DEEP_COMPANY_READER needs the entity-aware contract prompt so the
    kernel can limit the additive web dossier to single-company diligence questions."""
    return os.environ.get("ROSTER_DEEP_COMPANY_READER", "").lower() in ("1", "true", "yes")


def people_reader_on() -> bool:
    """Flag (default OFF): ROSTER_DEEP_PEOPLE_READER needs the entity-aware contract prompt so the
    kernel can limit the additive web dossier to single-person diligence questions."""
    return os.environ.get("ROSTER_DEEP_PEOPLE_READER", "").lower() in ("1", "true", "yes")


def deep_synthesis_on() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_DEEP_SYNTHESIS turns a non-lookup answer into a synthesis-first
    grounded analysis (core thesis → tensions → second-order implications → mechanism) built around
    grounded derivations, and appends the deep-analyst persona clause. The deep format is exposed to the
    kernel as inert data (`deep_answer_format`, always set); this flag + the question kind gate whether
    the kernel ever selects it (T2/T3). OFF → byte-identical (persona + format unchanged)."""
    return os.environ.get("ROSTER_DEEP_SYNTHESIS", "").lower() in ("1", "true", "yes")


def build_manifest() -> VerticalManifest:
    return VerticalManifest(
        name="roster",
        entity_types=entities.ENTITY_TYPES,
        scope_dimensions=SCOPE_DIMENSIONS,
        # Connectors are fixture-injected so the offline pipeline + tests run without network.
        connectors={
            "edgar": EdgarConnector(filings=sample_filings()),
            "arxiv": ArxivConnector(papers=sample_papers()),
            "openalex": OpenAlexConnector(),
            "semantic_scholar": SemanticScholarConnector(),
            "crossref": CrossrefConnector(),
            "wikidata": WikidataConnector(),
            "hackernews": HackerNewsConnector(),
            "reddit": RedditConnector(),
            "lobsters": LobstersConnector(),
            "stackoverflow": StackExchangeConnector(),
            "huggingface": HuggingFaceConnector(),
            "openreview": OpenReviewConnector(),
            "companies_house": CompaniesHouseConnector(),
            "uspto": UsptoConnector(),
            "wikipedia": WikipediaConnector(),
            "nsf": NsfConnector(),
            "nih_reporter": NihReporterConnector(),
            "expert_feed": ExpertFeedConnector(),
            "eng_blog": EngBlogConnector(),
            "podcast": PodcastConnector(),
            "github": GithubConnector(),
            "patentsview": PatentsViewConnector(),
            "gdelt": GdeltConnector(),
            # YC company directory — the startup POPULATION seed (name/batch/founders/desc), public Algolia.
            "yc": YcConnector(),
        },
        retrieval_sources={"corpus": TechRetrievalSource()},
        gating_policy=TechGatingPolicy(),
        citation_verifier=None,       # block_span handled by the kernel
        persona=TechPersona(),
        authority_policy=TechAuthorityPolicy(),
        evidence_classifier=evidence_kind.classify,   # structural facets → evidence tier (Rule 18)
        discovery_entity_of=discovery.entity_of,       # "who is working on X" scouting (M&A/corp-dev)
        # Analytical modes / USE-CASE LENSES: acquirer (M&A) + the deep-tech-intelligence lenses
        # (foresight/wisdom/genesis/market/whitespace/moat) that re-mix + re-posture the same grounded
        # evidence per what the user is doing. Selected via the request `mode`.
        answer_modes={"acquirer": MA_DIRECTIVE, **USE_CASE_LENSES},
        ui=TechUI(),
        # FLAG ROSTER_ANSWER_CLOSE: append a required LANDING (fitted synthesis + 2-4 grounded next
        # questions) so an answer concludes instead of stopping on a sources/gaps line. OFF → byte-identical.
        answer_format=(TECH_ANSWER_FORMAT + (tech_closing_block() if answer_close_on() else "")),
        # ROSTER_GOLDEN_ANSWER: the single golden compose directive that replaces the whole answer-shaping
        # stack when the flag is ON (one clean freeform brief, no narrated scaffolding). None → no-op.
        golden_answer_directive=GOLDEN_ANSWER_DIRECTIVE,
        # ROSTER_CONTRACT_COMPOSE (voice ⟂ shape): the successor to the flat golden directive — compose is
        # assembled as the universal VOICE + the SHAPE the derived contract asks for (enumerate / explore /
        # decision). Shape follows the QUESTION, not a deployment flag; the enumerative shape is what a
        # "build me a table of all X" ask needs and the flat golden directive suppressed.
        contract_compose_voice=GOLDEN_VOICE,
        contract_compose_shapes=CONTRACT_SHAPES,
        contract_compose_default=SHAPE_DEFAULT,
        contract_compose_prompt=TECH_CONTRACT_COMPOSE_PROMPT,   # classifies enumerative shape
        # Enhanced A/B synthesis variant (reuses the kernel's enhanced-answer slot; same section set).
        clinical_answer_format=TECH_DILIGENCE_SYNTHESIS_FORMAT,
        # Concept/term glossary + grounded conceptual VISUALS (diagrams) + inline visual/chart/reasoning
        # guidance — the noesis answer-augmentation features, targeted for tech diligence (flag-gated).
        terms_prompt=TECH_TERMS_PROMPT,
        visuals_prompt=TECH_VISUALS_PROMPT,
        visual_guidance=TECH_VISUAL_GUIDANCE,
        chart_guidance=TECH_CHART_GUIDANCE,
        reasoning_format=TECH_REASONING_FORMAT,
        gap_prompt=TECH_GAP_PROMPT,
        suggest_prompt=tech_suggest_prompt(),
        web_domains=TRUSTED_WEB_DOMAINS,
        web_domain_facets=WEB_DOMAIN_FACETS,
        # FLAG ROSTER_WEB_ENTITY_OPEN: the LLM page-quality screen prompt for the entity-scoped open-web
        # probe. Inert data — always set; the T3 flag gates whether the kernel ever consults it.
        web_quality_prompt=WEB_QUALITY_PROMPT,
        # Fast-moving tech: recency re-orders ALL tiers over a short horizon (flag ROSTER_FRESHNESS_RANKING).
        freshness_policy=TECH_FRESHNESS_POLICY,
        # Question-driven evidence regime (flag ROSTER_ANSWER_CONTRACT): one classification →
        # current/established/balanced → customizes retrieval+ranking+compose per question.
        # FLAG ROSTER_WEB_ENTITY_OPEN: swap in the variant that ALSO emits `subject_kind` so the kernel
        # can gate the entity-scoped open-web probe. OFF → the original prompt (byte-identical).
        contract_prompt=(TECH_CONTRACT_PROMPT_ENTITY
                         if (web_entity_open_on() or company_reader_on() or people_reader_on())
                         else TECH_CONTRACT_PROMPT),
        # FLAG ROSTER_LANDSCAPE_COVERAGE: the app swaps to this enumerative-categories contract (+ forces
        # question_contract="steer") so a "map the landscape / examine all X" ask fans retrieval out per
        # category instead of a few narrow searches. Off → not used (byte-identical). When a deep reader
        # or entity-open is ALSO on, use the variant that additionally emits `subject_kind` — otherwise the
        # landscape swap would strip subject_kind and silently disable the deep company/person readers for
        # every question (the muted "Tell me everything about <X> Founders" / person answers).
        landscape_contract_prompt=(TECH_LANDSCAPE_CONTRACT_PROMPT_ENTITY
                                   if (web_entity_open_on() or company_reader_on() or people_reader_on())
                                   else TECH_LANDSCAPE_CONTRACT_PROMPT),
        # REFLECTION addendum (flag ROSTER_REFLECTION): the app appends this to whichever contract prompt is
        # active so the ONE derivation call also returns the heart-of-intent. Off → not appended.
        reflection_contract_addendum=TECH_REFLECTION_ADDENDUM,
        probe_entities_contract_addendum=TECH_PROBE_ENTITIES_ADDENDUM,
        answer_profiles=ANSWER_PROFILES,
        # REASONED engine (flag ROSTER_REASONED_DEFAULT, default OFF — the noesis clinical-decision mode
        # re-homed to diligence): one scaffold call classifies the question (decision/lookup/understanding)
        # → decision-shaped questions get a coverage-brief-steered retrieval + a decision-first, grounding-
        # safe compose contract; lookups fall through to the standard engine; why/how → causal-model engine.
        # OFF → the engine param is ignored and answers are byte-identical to today.
        # FLAG ROSTER_ADAPTIVE_FORMAT: swap the ported clinical/VC decision memo for the general-audience,
        # question-adaptive format + de-VC scaffold (the persona de-VCs in lockstep, in persona.py). OFF →
        # legacy (byte-identical). Read at manifest-build (process start), so flip = redeploy.
        # FLAG ROSTER_DEEP_SYNTHESIS: on the adaptive path, use the scaffold variant that routes an
        # enumerate-and-compare / landscape question to "management" (→ deep compose) instead of
        # "lookup". OFF → the original adaptive scaffold (byte-identical classification).
        reasoned_scaffold_prompt=(
            ((TECH_ADAPTIVE_SCAFFOLD_PROMPT_DEEP if deep_synthesis_on() else TECH_ADAPTIVE_SCAFFOLD_PROMPT)
             if adaptive_format_on() else TECH_REASONED_SCAFFOLD_PROMPT)),
        # FLAG ROSTER_ANSWER_360 (rides adaptive): append the multi-perspective '## Perspectives' +
        # '## Related questions' sections to the ONE integrated answer. OFF → plain adaptive format.
        # FLAG ROSTER_LANDSCAPE_COVERAGE (rides adaptive): also append the market-map compose block so a
        # landscape answer is a clustered, grounded map with a "## Coverage basis" honesty section.
        reasoned_answer_format=(
            ((TECH_ADAPTIVE_ANSWER_FORMAT
              + (TECH_ANSWER_360_BLOCK if answer_360_on() else "")
              + (TECH_LANDSCAPE_COMPOSE_BLOCK if landscape_coverage_on() else ""))
             if adaptive_format_on() else TECH_REASONED_ANSWER_FORMAT)
            + (tech_closing_block() if answer_close_on() else "")),
        understanding_answer_format=(TECH_UNDERSTANDING_ANSWER_FORMAT
                                     + (tech_closing_block() if answer_close_on() else "")),
        understanding_query_hint=TECH_UNDERSTANDING_QUERY_HINT,
        # FLAG ROSTER_DEEP_SYNTHESIS: the synthesis-first compose format (core thesis → tensions →
        # second-order implications → mechanism, derivation-woven). ALWAYS set — inert data; the flag
        # (deep_synthesis) + question kind gate whether the kernel selects it (T2/T3). OFF → never used.
        deep_answer_format=TECH_DEEP_SYNTHESIS_FORMAT,
        company_reader=COMPANY_READER,
        person_reader=PERSON_READER,
        # FLAG ROSTER_AUTHORITY_BASIS: the compose FLOOR directive (ground facts in the highest-tier source;
        # opinion/blog/social are supplementary signal). ALWAYS set — inert data; the flag gates whether the
        # kernel appends it. OFF → never used (byte-identical).
        authority_basis_directive=TECH_AUTHORITY_BASIS_DIRECTIVE,
        # FLAG ROSTER_PARAMETRIC_LED: the pre-retrieval parametric-draft directive (outline + fact/reasoning
        # decomposition). ALWAYS set — inert data; the flag (parametric_led) + routing predicate gate whether
        # the kernel calls draft_prior (T2/T3 consume the draft). OFF → never used (byte-identical).
        prior_draft_prompt=PRIOR_DRAFT_PROMPT,
        # FLAG ROSTER_INTELLIGENCE_CORE: the pre-retrieval adversarial-draft directive (analytical frame +
        # 2-3 competing hypotheses in a flat line protocol). ALWAYS set — inert data; the flag
        # (intelligence_core) + routing predicate gate whether the kernel calls draft_intelligence (T2/T3
        # consume the hypotheses). OFF → never used (byte-identical).
        intelligence_draft_prompt=INTELLIGENCE_DRAFT_PROMPT,
        # Sub-vertical seam: sectors as a per-question subject scope (AI seeded), NOT separate verticals.
        sector_profiles=SECTOR_PROFILES,
        # Analytical lenses (the orthogonal axis): angles applied within the active sector.
        extraction_lenses=EXTRACTION_LENSES,
        eval_gold=dict(GOLD),
    )
