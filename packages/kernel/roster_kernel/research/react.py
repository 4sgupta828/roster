"""Generic ReAct research loop — domain-free mechanics.

search → observe → … → answer, bounded by the cost governor, with the provenance
hard gate applied to every emitted claim: a claim survives only if its verbatim
`quote` exists at its cited atom's locator (else it's rejected — no fabrication).

The LLM decides each step via a structured `AgentStep` (the kernel's LLM port is
structured-output, so no bespoke tool-use protocol is needed). Domain vocabulary,
the system prompt, and richer gating (the 10th-seam policy) come from the vertical
in P3; here the mechanics are proven offline with a scripted FakeLLM.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Literal

from roster_kernel.research.deep_company import retrieve_deep_company
from roster_kernel.research.deep_person import retrieve_deep_person
from roster_kernel.research.web_coverage import build_coverage_queries, retrieve_web_coverage

_log = logging.getLogger(__name__)

# Compose is the user-facing DELIVERABLE (the prose answer), not discretionary enrichment — a
# transient LLM blip on that one call must not silently drop the answer while the verified evidence
# survives (the 'grounded, N claims, empty answer' bug). Retry a few times, then surface a note.
_COMPOSE_ATTEMPTS = 3
_COMPOSE_BACKOFF_S = 1.5          # base backoff between compose retries (tests patch to 0)
# Compose is the user-facing prose answer synthesizing up to ~60 findings (effort-scaled) with inline
# [n] citations — it needs far more room than a planner step. At the 2048 default the emit tool-call
# gets TRUNCATED mid-answer → the partial dict fails ComposedAnswer validation on EVERY retry (the
# deterministic 'couldn't be generated' bug). Only actually-generated tokens are billed, so a high
# ceiling adds no cost, only headroom.
_COMPOSE_MAX_TOKENS = 16000   # claude-sonnet-5 supports far more than the old ~8192 assumption; at 8000 the
# The ReAct step (AgentStep) emits an `action` plus, on the answer step, a list of claims (each with
# text + atom_id + a verbatim quote). On a broad, evidence-rich question the agent can emit MANY claims
# in one step, and at the 2048 default the emit tool-call TRUNCATES mid-JSON → a hard provider error
# surfaces as a 502 ("Couldn't reach the research service"). Give the step ample room — only
# actually-generated tokens are billed, so a high ceiling is headroom, not cost.
_PLANNER_MAX_TOKENS = 8000
_COMPOSE_FAIL_NOTE = (
    "_The written answer couldn't be generated just now, but the evidence below was retrieved and "
    "verified against its sources. Please retry the question._")

# READABILITY (flag): a WRITING-STYLE layer only. It changes HOW the prose reads, never WHAT the answer
# contains or its structure — the sections/headings/order stay exactly as the contract-chosen directive
# dictates, every [n] citation and every [[R]] label is kept. It only asks for plain, short-sentence
# prose so the answer reads like a sharp brief, not a research paper. Appended AFTER the directive so it
# governs style regardless of which structure the question-driven contract selected.
_READABILITY_STYLE = (
    "WRITING STYLE — read this LAST; it governs only HOW you write the prose inside the structure above. "
    "It does NOT change the sections, headings, their order, or which findings/citations you include, and "
    "you must keep EVERY [n] citation and EVERY [[R]] reasoning label exactly as required above:\n"
    "- Write for a smart reader — clear, well-structured, and thorough. Be COMPREHENSIVE where it helps, "
    "but stay easy to scan: depth is good, density (walls of text, run-on sentences) is not.\n"
    "- One idea per sentence. Keep sentences short (aim ~12–22 words). If a point needs a caveat, put the "
    "caveat in the NEXT sentence — do not chain it on with an em-dash.\n"
    "- Avoid em-dash pile-ups and nested parentheticals ((a)…(b)…). Say the point plainly first, then add "
    "detail in a following short sentence.\n"
    "- Lead each sentence with its point, then the support. Prefer plain words over jargon.\n"
    "- FORMAT TO SCAN. When you list two or more things — companies, options, factors, gaps, examples, "
    "steps — use a markdown bullet list with ONE item per line. Never pack a list into a comma-run or an "
    "(a)/(b)/(c) sentence. Put each distinct point on its own line.\n"
    "- Keep paragraphs SHORT: at most 2–3 sentences. If an explanation runs long, lead with one sentence, "
    "then break the specifics into a short bullet list underneath.\n"
    "- BALANCE — do not over-format: use a sentence or two for a single point or a narrative link; use "
    "bullets for enumerations. Do not bullet a lone statement, and do not fragment every sentence into its "
    "own bullet. No walls of text, and no confetti of one-line bullets either.\n"
    "- Do NOT shorten by dropping facts, citations, or [[R]] labels — only by writing them more simply. "
    "Same information, lighter and more scannable.")

# Compose sees only the verified findings, capped for cost + scannability. Default selection is
# first-come (retrieval/extraction order). Under the evidence-select flag we collect MORE candidates
# and keep the ones most RELEVANT to the question — so compose gets the BEST findings, not the first.
_COMPOSE_CLAIM_CAP = 30       # max verified findings sent to compose
_EXTRACT_COLLECT = 80         # under evidence-select, gather up to this many before ranking down
_PARAMETRIC_FACT_CAP = 12     # ROSTER_PARAMETRIC_LED (T2): max drafted FACT claims verified per run
#                               (bounds cost: one targeted retrieval + one grounding call per fact)
_INTELLIGENCE_HYP_CAP = 3     # ROSTER_INTELLIGENCE_CORE (T2): max hypotheses whose FOR/AGAINST legs
#                               pre-seed the atom pool (bounds cost: ≤2 targeted retrievals per hypothesis)
_REFUTER_AGAINST_CAP = 2      # T-B: max AGAINST legs per hypothesis when a cross-family red-team authors
#                               them — keeps hyp_cap*for + hyp_cap*against bounded (≤3×1 + 3×2 = 9 legs)
_REFUTER_N = 2                # T-B: disconfirming queries the red-team refuter is asked for per hypothesis

# Answer-axes addendum (flag `axis_complete`): appended after the base compose directive so the answer
# COVERS each aspect the reader asked about + synthesizes. Domain-neutral (the axes come from the
# contract). <AXES> is replaced with the derived axis list.
_AXIS_COMPLETE_ADDENDUM = (
    "[Completeness + synthesis — the reader explicitly asked about these aspects: <AXES>. Your answer "
    "MUST address EACH ONE: give it a short labeled section with the grounded evidence (cite [n]), or — "
    "if the findings do not cover it — ONE plain line saying so under that aspect's heading. NEVER "
    "silently skip a requested aspect. OPEN with a crisp 2-3 sentence synthesized TAKE that directly "
    "answers the question's intent, grounded in the findings (wrap any inference in [[R]]...[[/R]]; add "
    "no new facts). When sources CONFLICT on a figure, state the best-supported value in ONE line with a "
    "brief '(sources vary: ...)' note — do NOT build a reconciliation table or repeat the caveat. Keep "
    "any 'not covered' notes to a single tight line, never a long list.]")

# Technical-synthesis addendum (flag `tech_synthesis`): a STRATEGIC synthesis, FROM the evidence, of
# how the product's technology works end-to-end. Grounding-careful: disclosed → cited; the likely
# architecture → clearly-labeled grounded inference ([[R]]), no invented proprietary specifics.
_TECH_SYNTHESIS_ADDENDUM = (
    "[How it works — a STRATEGIC TECHNICAL SYNTHESIS from the evidence. When the subject is a product / "
    "technology / tech company, SYNTHESIZE from the findings how the technology actually works and how "
    "it comes together END-TO-END for users:\n"
    "- the core technical building blocks the product is built on (the models, data, systems, or methods "
    "it relies on) — cite [n] where the findings disclose them;\n"
    "- the end-to-end flow: how a user's input becomes the product's output, and what each part "
    "contributes;\n"
    "- where the findings do NOT disclose the architecture, SYNTHESIZE the LIKELY design by reasoning "
    "from the disclosed capabilities and how such systems are generally built — clearly labeled "
    "('likely', 'typically', 'expected') and wrapped in [[R]]...[[/R]]. NEVER present an inferred "
    "mechanism as a disclosed fact, and never invent a specific proprietary detail (a named model, "
    "benchmark, or patent) that is not in the findings;\n"
    "- tie it to STRATEGY: why each part matters, where the technical difficulty or defensibility lies, "
    "and what the design implies for the product and its moat.\n"
    "Concrete and technical — a sharp engineer's read synthesized from the evidence, not marketing and "
    "not ungrounded speculation. Separate DISCLOSED from LIKELY throughout. Skip this entirely if the "
    "subject has no technical product.]")

# Parametric-led addendum (ROSTER_PARAMETRIC_LED, T3): appended to the compose directive ONLY when the
# model's integrated knowledge LED this run (a `prior_draft` is present). The answer FOLLOWS the drafted
# OUTLINE for structure/reasoning, but every FACT must come from the VERIFIED FINDINGS and cite [n] —
# an unverifiable model claim is never restated as established fact. <OUTLINE> is replaced with the
# drafted outline text. OFF (prior_draft is None) → not appended → every compose prompt byte-identical.
_PARAMETRIC_ADDENDUM = (
    "[PARAMETRIC-LED. Structure the answer to follow this OUTLINE:\n<OUTLINE>\n\n"
    "The following is YOUR OWN REASONING for this question — build the answer AROUND it, as the analytical "
    "spine. Present each as labeled inference wrapped [[R]]...[[/R]], grounded in and consistent with the "
    "VERIFIED FINDINGS; NEVER assert a new fact from it (every figure, name, date, or event MUST come from "
    "a verified finding and cite [n]):\n<REASONING>\n\n"
    "RULES: lead with the reasoning/synthesis (that is the value); every FACT cites [n] from the VERIFIED "
    "FINDINGS; do NOT restate any unverifiable claim as established fact; where your reasoning outruns the "
    "findings, keep it clearly labeled [[R]] inference, never fact.]")

# Intelligence-core addendum (ROSTER_INTELLIGENCE_CORE, T3): appended to the compose directive ONLY when
# the model drafted competing HYPOTHESES + an analytical FRAME for this run. It structures the answer
# around WEIGHING the hypotheses against the retrieved evidence — but strictly as an analytical frame the
# evidence TESTS, never as a set of facts. Every FACT still comes from the VERIFIED FINDINGS and cites
# [n]; the synthesis is labeled [[R]] inference (and, on a deep run, flows through the derive-weave). The
# hypotheses/frame shape STRUCTURE, not asserted facts (the parametric post-mortem lesson: do NOT inject
# raw reasoning as the fact-spine). <FRAME> / <HYPOTHESES> are replaced with the drafted frame + the
# rendered hypothesis lines. OFF (hypotheses is None) → not appended → every compose prompt byte-identical.
_INTELLIGENCE_ADDENDUM = (
    "[INTELLIGENCE FRAME — organize the answer around these COMPETING HYPOTHESES, tested against the "
    "evidence. This FRAME (analytical world-model for the question) is: <FRAME>\n\n"
    "The competing hypotheses (each with the observation that would DISPROVE it):\n<HYPOTHESES>\n\n"
    "Weigh the VERIFIED FINDINGS both FOR and AGAINST each hypothesis, then resolve which the evidence "
    "BEST supports and WHY. STRICT RULES: this is an analytical FRAME the evidence TESTS, NOT a set of "
    "facts. Every FACT you state comes from the VERIFIED FINDINGS and cites [n]. Present your synthesis / "
    "judgment as labeled inference [[R]]...[[/R]] grounded in the findings; NEVER assert a hypothesis or "
    "mechanism as established fact. State plainly WHICH hypothesis the evidence best supports and the ONE "
    "concrete observation (its falsifier) that would change that read.]")

from pydantic import BaseModel, Field, field_validator

from roster_kernel.contract.dto import RetrievalRequest
from roster_kernel.contract.protocols import GatingPolicy, RetrievalSource
from roster_kernel.providers.embeddings import Embedder
from roster_kernel.providers.llm import LLMClient
from roster_kernel.research.atoms import IDENTITY_INSTRUCTION, AtomStore, identity_tag
from roster_kernel.research.budget import BudgetExceeded, BudgetState
from roster_kernel.research.provenance import BlockSpanVerifier
from roster_kernel.research.refuter import refute_hypothesis
from roster_kernel.research.web_liveness import drop_dead_urls
from roster_kernel.research.web_quality import screen_open_web_hits
from roster_kernel.retrieval.dispatch import multi_query_retrieve


# ---- the LLM's structured step + emitted claims --------------------------

class ClaimOut(BaseModel):
    text: str            # the claim
    atom_id: str         # the atom it cites
    quote: str           # a verbatim span from that atom supporting the claim


def _coerce_json_list(v):
    """PROVIDER-MALFORMATION REPAIR: models occasionally emit a tool arg as TEXT — the JSON
    list plus trailing XML tool syntax ('[...]</claims>\\n</invoke>') — which arrives here as
    a string and would hard-fail the whole research run on a stochastic flake. Repair: strip
    trailing garbage after the last ']', parse; a still-unparseable value degrades to [] —
    for an answer step that means the empty-claims RECOVERY re-ask runs (graceful), never a
    user-facing 'provider error'."""
    if not isinstance(v, str):
        return v
    import json as _json
    s = v.strip()
    for cand in (s[: s.rfind("]") + 1] if "]" in s else s, s):
        try:
            parsed = _json.loads(cand)
            if isinstance(parsed, list):
                return parsed
        except Exception:   # noqa: BLE001
            continue
    _log.warning("unparseable list-arg from provider (len=%d) — degrading to []", len(v))
    return []


class AgentStep(BaseModel):
    action: Literal["search", "answer"]
    query: str | None = None
    queries: list[str] = []     # optional reformulations → multi-query fusion (recall)
    # SOURCE ROUTING (flag ROSTER_SOURCE_ROUTING): the agent may name source TYPES (opaque vertical-
    # defined `source_kind` values) to ALSO target for this query. This adds a SCOPED retrieval leg ON
    # TOP OF the unchanged flat search — never a filter (a mis-route can't lose recall, the flat pass
    # still fires). Empty / flag off → byte-identical.
    source_kinds: list[str] = []
    claims: list[ClaimOut] = []

    @field_validator("queries", "claims", mode="before")
    @classmethod
    def _repair_lists(cls, v):
        return _coerce_json_list(v)


class ChartBar(BaseModel):
    """One datum of a chart. `value` is plotted; `value_str` is that figure EXACTLY as it appears in the
    cited finding (used to VERIFY it's grounded); `finding` is the 1-based finding index. `label` is the
    option/x-category/slice name depending on kind. `series` groups bars for a grouped chart (e.g.
    "Efficacy" vs "Adverse events") or names one line of a multi-line chart. `low`/`high` (+ their *_str)
    are the optional confidence-interval / range bounds for an INTERVAL (forest-plot) chart — each also
    grounded."""
    label: str
    value: float
    value_str: str = ""
    finding: int = 0
    series: str = ""
    low: float | None = None
    low_str: str = ""
    high: float | None = None
    high_str: str = ""


class ChartSpec(BaseModel):
    """A chart built ONLY from verified findings. Kinds: 'bar' (one value per option), 'grouped_bar'
    (2+ series per option — e.g. benefit vs risk), 'interval' (point estimate + CI/range per option, a
    forest plot), 'line' (a value over ordered x-categories — time/stages/doses; `label` is the
    x-category, `series` names each line when there are several), 'pie' (parts-of-a-whole shares;
    `label` is the slice name). EVERY plotted number (value, and low/high when present) must appear
    verbatim in its cited finding, or the whole chart is dropped. Meant for patterns hard to read from
    prose/tables."""
    kind: str = "bar"            # "bar" | "grouped_bar" | "interval" | "line" | "pie"
    title: str = ""
    unit: str = ""
    bars: list[ChartBar] = []


# ---- Reasoning Read: the interpretation layer (factra "Executive Read" discipline) -----------
# The answer already exposes span-verified FACTS. The Reasoning Read adds a SEPARATE, typed layer of
# INTERPRETATION on top — tensions, gaps, assumptions, implications, what-would-change-the-answer —
# each resting on specific findings and containing NO number/date/dose not already in those findings.
# It is validated in code (dangling-ref + no-new-facts drops), exactly like `charts`, so a fabricated
# inference can never ship. Populated only when the reasoning-read flag drives the compose directive.

# Closed set of interpretation kinds (Literal enforces it at parse; the guard re-checks defensively).
InterpretationKind = Literal["tension", "gap", "assumption", "implication", "what_would_change_this"]


class InterpretationItem(BaseModel):
    """ONE labeled piece of interpretation resting on specific verified findings. `kind` is drawn from a
    closed set; `basis_findings` are the 1-based finding indices it rests on (dangling refs are dropped);
    `text` may contain NO hard token (number/%/date/$/dose) absent from its basis findings (no-new-facts)."""
    text: str
    kind: InterpretationKind = "implication"
    basis_findings: list[int] = []


class ConfidenceDim(BaseModel):
    """One confidence dimension: a coarse LLM-owned band + a one-line rationale grounded in the evidence's
    character (e.g. how many/what tier of studies, whether it's causal vs associational)."""
    level: Literal["high", "moderate", "low", "unknown"] = "unknown"
    rationale: str = ""


class ConfidenceRead(BaseModel):
    """Three orthogonal confidence dimensions (feedback #14): FACTUAL (are the reported facts solid?),
    CAUSAL (does the evidence support a causal reading or only association?), GENERALIZATION (does it
    transfer beyond the studied population/setting?). Each is qualitative — it adds NO new fact."""
    factual: ConfidenceDim = ConfidenceDim()
    causal: ConfidenceDim = ConfidenceDim()
    generalization: ConfidenceDim = ConfidenceDim()


class ComposedAnswer(BaseModel):
    """A synthesized prose answer built ONLY from the verified findings, with
    inline [n] references to them so every statement stays traceable."""
    answer: str
    # Honesty signal (LLM-owned): does the evidence DIRECTLY address the asked question, or is it
    # only analogue/tangential? When false, `gap_note` names what direct evidence is missing — the
    # kernel surfaces it as a coverage gap so a "grounded-on-analogues" answer still flags the gap.
    directly_addresses: bool = True
    gap_note: str = ""
    # Optional charts (only when the answer-charts flag drives the directive to emit them). Each is
    # VALIDATED against the verified findings before it reaches the UI — an ungrounded bar drops the chart.
    charts: list[ChartSpec] = []
    # Reasoning Read (only when the reasoning-read flag drives the directive). Both are VALIDATED /
    # surfaced in the kernel; empty/None when the directive doesn't ask → byte-identical OFF path.
    interpretation: list[InterpretationItem] = Field(
        default=[], description="Typed interpretation of the evidence (tension/gap/assumption/"
        "implication/what_would_change_this) — populate when the directive asks for a Reasoning Read.")
    confidence: ConfidenceRead | None = Field(
        default=None, description="Three-dimension confidence read (factual/causal/generalization) — "
        "populate when the directive asks for a Reasoning Read.")
    # The Reasoning Read's FRAME: a purpose (the decision/outcome the reasoning serves, from the
    # question) and a conclusion (the informed judgment toward that purpose). These turn the typed
    # `interpretation` items from disconnected observations into a purpose-driven analysis that
    # CONVERGES on a decision. Both are grounded (no hard token absent from the findings).
    reasoning_purpose: str = Field(
        default="", description="ONE sentence naming the decision or outcome the reasoning serves, "
        "framed from the question (the north star the interpretation factors are organized around). "
        "Populate only for a Reasoning Read; adds no new fact.")
    reasoning_conclusion: str = Field(
        default="", description="The informed judgment TOWARD the purpose: given the factors and their "
        "strength, what the evidence supports concluding or doing (not individualized advice). 1–3 "
        "sentences, resting on the findings, no new fact. Populate only for a Reasoning Read.")


def _validate_charts(charts: list[ChartSpec], verified: list["VerifiedClaim"]) -> list[dict]:
    """Keep only charts whose EVERY plotted number is grounded: for each bar, the finding index is valid
    AND its `value_str` (and `low_str`/`high_str` when present) appears verbatim (case-insensitive) in
    that finding's text or quote. Fail-safe — any bad number drops the WHOLE chart (a partly-verified
    chart is worse than none). Also enforces a real comparison (>=2 groups) plus kind-specific shape
    rules: a 'pie' needs 2–6 non-negative slices (parts of a whole); a 'line' needs >=3 points per
    series (a trend) and <=3 series (readability). Returns dicts for the API."""
    def _grounded(s: str, finding: int) -> bool:
        s = (s or "").strip().lower()
        if not s or not (1 <= finding <= len(verified)):
            return False
        src = (verified[finding - 1].text + " " + verified[finding - 1].quote).lower()
        return s in src

    out: list[dict] = []
    for ch in charts or []:
        bars = ch.bars or []
        kind = (ch.kind or "bar").strip().lower()
        # a chart needs >=2 distinct groups (labels) to be a comparison worth showing
        if len({(b.label or "").strip() for b in bars}) < 2:
            continue
        if kind == "pie":
            # parts-of-a-whole: 2–6 slices, none negative (a negative share is meaningless)
            if not (2 <= len(bars) <= 6) or any(b.value < 0 for b in bars):
                _log.warning("chart dropped: pie must have 2-6 non-negative slices (title=%r)", ch.title)
                continue
        elif kind == "line":
            # a trend needs >=3 points per series; more than 3 lines is unreadable
            by_series: dict[str, int] = {}
            for b in bars:
                key = (b.series or "").strip()
                by_series[key] = by_series.get(key, 0) + 1
            if len(by_series) > 3 or any(n < 3 for n in by_series.values()):
                _log.warning("chart dropped: line needs >=3 points per series and <=3 series (title=%r)",
                             ch.title)
                continue
        ok = True
        for b in bars:
            if not _grounded(b.value_str, b.finding):
                ok = False; break
            # interval bounds, when given, must ALSO be grounded in the same cited finding
            if (b.low is not None or b.low_str) and not _grounded(b.low_str, b.finding):
                ok = False; break
            if (b.high is not None or b.high_str) and not _grounded(b.high_str, b.finding):
                ok = False; break
        if ok:
            out.append(ch.model_dump())
        else:
            _log.warning("chart dropped: a plotted figure not found in its cited finding (title=%r)", ch.title)
    return out


# Computable token classes (Rule 18: structural, not a semantic heuristic — the LLM still owns MEANING;
# this only checks a number/date/dose the model wrote also exists in the findings it cited). Matches
# percentages/decimals/integers, ISO and US dates, $ amounts, and dose-like "5 mg" / "10mg".
_HARD_TOKEN_RE = re.compile(
    r"""(?xi)
    (?<![A-Za-z0-9])                        # NOT letter/digit-adjacent → skip PCSK9, B12, COVID19, CoQ10
    (?:
      \d{4}-\d{2}-\d{2}                       # 2026-07-01 (longest first)
      | \$?\d{1,3}(?:,\d{3})+(?:\.\d+)?       # 1,234 / $1,234.56
      | \d+(?:\.\d+)?\s?(?:mg|mcg|µg|g|ml|kg|units?|iu)\b   # 5 mg / 10mg / 250 mcg (dose)
      | \$?\d+(?:\.\d+)?%?                     # 9.5 / 9.5% / $4.2 / 42
      | \d{1,2}/\d{1,2}/\d{2,4}              # 7/1/2026
    )
    """,
)


def _norm_token(tok: str) -> str:
    """Normalize a hard token for membership: lowercase, drop $ , % and internal whitespace so
    '5 mg' and '5mg' compare equal; keep digits, dots, dashes, slashes, unit letters."""
    return tok.strip().lower().lstrip("$").rstrip("%").replace(",", "").replace(" ", "")


def extract_hard_tokens(text: str) -> set[str]:
    """Extract computable numeric/date/dose tokens from prose (normalized). Structural extraction only
    (Rule 18) — used to enforce that interpretation adds no number/date/dose the findings don't state."""
    return {_norm_token(m.group(0)) for m in _HARD_TOKEN_RE.finditer(text or "")}


_REF_MARK_RE = re.compile(r"\[\d+\]")   # citation markers — not facts; stripped before token checks


def _frame_grounded(text: str, allowed: set[str]) -> str:
    """No-new-facts guard for a Reasoning-Read FRAME (purpose / conclusion): keep it only if every hard
    token it states (citation markers stripped) already appears in `allowed` — the union of the verified
    findings AND the grounded composed answer the frame summarizes. A figure in NEITHER drops the whole
    text (fail-safe against fabrication); a figure the answer already states no longer blanks a valid
    judgment. Returns the original text when grounded, else "" (Rule 6: provenance, not correctness)."""
    s = (text or "").strip()
    return s if (s and extract_hard_tokens(_REF_MARK_RE.sub(" ", s)).issubset(allowed)) else ""


def _validate_interpretation(items: list["InterpretationItem"],
                             verified: list["VerifiedClaim"]) -> list[dict]:
    """Keep only interpretation items that are (a) a valid kind, (b) resting on ≥1 real finding
    (dangling-ref: basis indices are clamped to 1..n; an item left with none is dropped), and (c)
    introduce NO hard token (number/%/date/$/dose) absent from the TEXT/QUOTE of their basis findings
    (no-new-facts). Fail-safe — any violation drops that item. This is PROVENANCE (Rule 6), not a
    correctness check: it proves the interpretation didn't fabricate a figure, not that it's the right
    reading. Returns dicts (with resolved 1-based `basis_findings`) for the API."""
    allowed = {"tension", "gap", "assumption", "implication", "what_would_change_this"}
    n = len(verified)
    out: list[dict] = []
    for it in items or []:
        kind = (it.kind or "").strip()
        text = strip_control_tags((it.text or "").strip())   # a bled control-tag serialization → truncate
        if kind not in allowed or not text:
            continue
        basis = [b for b in (it.basis_findings or []) if isinstance(b, int) and 1 <= b <= n]
        if not basis:            # dangling: interpretation must rest on ≥1 grounded finding
            _log.warning("interpretation dropped: no valid basis finding (kind=%s)", kind)
            continue
        # no-new-facts: every hard token in the item's text must appear in a basis finding's text/quote
        basis_src = " ".join((verified[b - 1].text + " " + verified[b - 1].quote) for b in basis)
        basis_tokens = extract_hard_tokens(basis_src)
        item_tokens = extract_hard_tokens(text)
        if not item_tokens.issubset(basis_tokens):
            _log.warning("interpretation dropped: hard token not in basis findings (kind=%s, extra=%s)",
                         kind, item_tokens - basis_tokens)
            continue
        out.append({"text": text, "kind": kind, "basis_findings": basis})
    return out


_CONTROL_TAG_RE = re.compile(
    r'</?\s*(?:answer|directly_addresses|gap_note|reasoning_purpose|reasoning_conclusion|'
    r'interpretation|confidence|charts|invoke|function_calls|parameter|antml:[\w:-]+)\b[^>]*>',
    re.IGNORECASE)


def strip_control_tags(text: str) -> str:
    """Defensive cleanup: some completions bleed the tool-call / structured-output serialization into the
    answer STRING (e.g. a trailing '… [1].</answer> <directly_addresses>true</directly_addresses> </invoke>').
    Truncate at the first such control tag — the real answer precedes it. No-op on a clean answer."""
    if not text:
        return text
    m = _CONTROL_TAG_RE.search(text)
    return (text[:m.start()] if m else text).rstrip()


def _authoritative_subset(hits: list) -> list:
    """Fail-safe fallback when the open-web quality screen cannot judge (LLM error / budget):
    keep only hits carrying a venue-authority facet (source_kind) — i.e. known/whitelisted
    venues. Structural (Rule 18: not a semantic quality guess), domain-free."""
    return [h for h in hits if (getattr(h, "facets", None) or {}).get("source_kind")]


def _refs_valid(text: str, n_findings: int) -> bool:
    """Domain-free provenance check on a composed answer: it must cite at least one
    finding and every inline [n] must resolve to a real finding (1..n_findings).

    This is structural validation of citation FORMAT (Rule 18: parsing/validating a
    format is code's job, not a semantic heuristic) — it guards against a structured
    directive tempting the model to over-cite or invent a reference number.
    """
    refs = [int(m) for m in re.findall(r"\[(\d+)\]", text)]
    if not refs:
        return False
    return all(1 <= r <= n_findings for r in refs)


# Evidence-fitness (flag): a small, BOUNDED tier boost added on top of the dense relevance score, so
# when two findings are similarly relevant the stronger tier (guideline/SR > RCT > cohort > case report)
# surfaces into the compose cap. Boost-only + small weight so cosine still dominates and an unknown tier
# (rank 0) is a no-op → never demotes a finding below its relevance rank. Max authority rank = 6.
_EVIDENCE_FITNESS_WEIGHT = 0.15
_EVIDENCE_MAX_RANK = 6
_COUNTRY_BOOST_WEIGHT = 0.12   # bounded, comparable to the tier boost; boost-only, never demotes
# Recency boost — CONTROLLING tiers only (guideline / systematic review, rank >= _CONTROLLING_RANK):
# for normative evidence the newest genuinely supersedes (KDIGO 2026 > KDIGO 2012), whereas a
# landmark RCT must never lose to a newer small trial — so lower tiers get NO recency term. Linear
# decay to zero at the horizon; unknown year is a no-op (absence never demotes). Rides the same
# evidence-fitness seam as the tier boost (only active when `evidence_ranker` is supplied).
_RECENCY_BOOST_WEIGHT = 0.10
_RECENCY_HORIZON_YEARS = 12
_CONTROLLING_RANK = 6
_LOW_YIELD_ATOMS = 2           # a search adding fewer than this many NEW atoms counts as diminishing-returns
#                               (two in a row → force an answer; catches the steady +1 grind, not just zero)
_LOW_YIELD_ATOMS = 2           # a search adding fewer than this many NEW atoms counts as diminishing-returns
#                               (two in a row → force an answer; catches the steady +1 grind, not just zero)


async def _rank_claims_by_relevance(question, claims, embedder, top, *,
                                    evidence_ranker=None, country_boost=None, rank_all=False,
                                    freshness=None):
    """Keep the `top` verified claims most RELEVANT to the question, by dense cosine similarity of
    claim↔question embeddings (Rule 18 — a computable relevance signal, not a keyword heuristic). When
    `evidence_ranker` is supplied (evidence-fitness on), a SMALL bounded evidence-tier boost is added so
    a stronger-tier finding wins ties. When `country_boost` (a set of country codes, e.g. {"IN"}) is
    supplied, findings whose `source_country` is in it get a bounded boost so region-specific evidence
    (e.g. Indian guidelines) SURFACES — WITHOUT filtering out the global evidence base. Both are
    boost-only, never demoting below the relevance baseline, and never touch the span/entailment gates.
    Fail-safe: any embedding error → the original order's first `top` (never worse than today)."""
    import asyncio
    import math

    # Corpus currency (Evidence Pulse C1/A3): superseded-source claims are stable-partitioned BELOW
    # current ones — a hard fact, not a boost (a negative additive term can't express it against
    # cosine in [-1,1]), and deliberately a documented break of this function's boost-only design.
    # Applied UNCONDITIONALLY (including the <= top early return, which skips scoring entirely).
    def _stale(c) -> bool:
        f = getattr(c, "facets", None) or {}
        return bool(f.get("superseded_by") or f.get("retracted"))
    claims = sorted(claims, key=_stale)                 # stable: preserves order within partitions

    # `rank_all` (stage-3 slot-aware selection): score EVERY claim even when the pool fits under
    # `top`, so the caller gets a FULL ranked ordering to allocate seats from (default False →
    # the early return below is byte-identical to today).
    if len(claims) <= top and not rank_all:
        return list(claims)
    try:
        vecs = await asyncio.to_thread(lambda: embedder.embed([question] + [c.text for c in claims]))
    except Exception:   # noqa: BLE001
        return list(claims)[:top]
    qv = vecs[0]
    qn = math.sqrt(sum(x * x for x in qv)) or 1.0
    cb = set(country_boost or ())

    import datetime
    this_year = datetime.date.today().year   # real-world currency: rankings age with the calendar

    def _boost(i: int) -> float:
        b = 0.0
        if evidence_ranker is not None:
            try:
                r = evidence_ranker(getattr(claims[i], "evidence_kind", "") or "")
                b += _EVIDENCE_FITNESS_WEIGHT * (max(0, int(r)) / _EVIDENCE_MAX_RANK)
                if not freshness and int(r) >= _CONTROLLING_RANK:
                    # controlling tier + known year → bounded recency term (newest guidance governs).
                    # Suppressed when a vertical freshness policy is active — the freshness block below
                    # owns recency then (uniform, all-tiers), so a claim is never double-counted.
                    yr = str((getattr(claims[i], "facets", None) or {}).get("year") or "")[:4]
                    if yr.isdigit():
                        age = max(0, this_year - int(yr))
                        b += _RECENCY_BOOST_WEIGHT * max(0.0, 1.0 - age / _RECENCY_HORIZON_YEARS)
            except Exception:   # noqa: BLE001 — ranking must never break selection
                pass
        if cb:
            try:
                if (getattr(claims[i], "facets", None) or {}).get("source_country") in cb:
                    # TIER-AWARE country boost (IN-spec D-4): a flat boost let a region-stamped
                    # case report displace a global systematic review (flat 0.12 vs the whole
                    # tier range 0.15). Scale by tier so region preference NEVER outweighs
                    # evidence quality: guideline gets the full weight, a case report ~1/6,
                    # unknown tier gets nothing (when the ranker is available) or a
                    # conservative half-weight (when tiering is off entirely).
                    if evidence_ranker is not None:
                        try:
                            r = max(0, int(evidence_ranker(
                                getattr(claims[i], "evidence_kind", "") or "")))
                        except Exception:   # noqa: BLE001
                            r = 0
                        b += _COUNTRY_BOOST_WEIGHT * (min(r, _EVIDENCE_MAX_RANK) / _EVIDENCE_MAX_RANK)
                    else:
                        b += _COUNTRY_BOOST_WEIGHT * 0.5
            except Exception:   # noqa: BLE001
                pass
        # FRESHNESS (flag ROSTER_FRESHNESS_RANKING): a vertical-supplied recency term that re-orders
        # claims across ALL tiers over a SHORT horizon (fast-moving verticals). Bounded + additive
        # (boost-only, never demotes below the relevance baseline), independent of evidence-fitness
        # (min_rank=0 needs no tier). `freshness` None → this block is skipped → byte-identical OFF.
        if freshness:
            try:
                yr = str((getattr(claims[i], "facets", None) or {}).get("year") or "")[:4]
                if yr.isdigit():
                    rank = 0
                    if evidence_ranker is not None:
                        try:
                            rank = max(0, int(evidence_ranker(getattr(claims[i], "evidence_kind", "") or "")))
                        except Exception:   # noqa: BLE001
                            rank = 0
                    _y = int(yr)
                    # future-dated records (bad "forthcoming" metadata: 2050/2114) must NOT earn a
                    # recency boost — only real past/current years count.
                    if _y <= this_year and rank >= int(freshness.get("min_rank", _CONTROLLING_RANK)):
                        age = this_year - _y
                        hz = max(1, int(freshness.get("horizon_years", _RECENCY_HORIZON_YEARS)))
                        b += float(freshness.get("weight", _RECENCY_BOOST_WEIGHT)) * max(0.0, 1.0 - age / hz)
            except Exception:   # noqa: BLE001 — ranking must never break selection
                pass
        return b

    def _score(i: int) -> float:
        v = vecs[1 + i]
        dot = sum(a * b for a, b in zip(qv, v))
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        return dot / (qn * vn) + _boost(i)          # cosine + bounded tier boost (boost-only)

    # partition primary (currency is a fact), relevance+boosts secondary within each partition
    order = sorted(range(len(claims)), key=lambda i: (_stale(claims[i]), -_score(i)))
    return [claims[i] for i in order[:top]]


# ---- results -------------------------------------------------------------

@dataclass
class VerifiedClaim:
    text: str
    atom_id: str
    quote: str
    source_key: str = ""
    document_title: str = ""
    document_id: str = ""
    # Evidence-fitness (Phase 1): the cited atom's facets + a vertical-classified evidence tier. Raw
    # data only — nothing consumes it unless the evidence-fitness flag is on (ranking) or an eval reads
    # it (evidence_floor). Domain-free: `evidence_kind` is filled by a vertical-supplied classifier.
    facets: dict = field(default_factory=dict)
    evidence_kind: str = ""
    # Evidence Contract stage 2 (claim-congruence flag): the binding judge's soft annotation.
    # "" = clean (or flag off); "kind_mismatch" = kept but demoted (the claim's kind of assertion
    # doesn't match its evidence's kind); "unjudged" = the binding judge couldn't rule (no key /
    # error / budget) — annotate, never drop (Rule 18 fail-safe). Hard verdicts (off-subject /
    # not-entailed) DROP the claim instead of annotating it.
    congruence_note: str = ""


@dataclass
class RejectedClaim:
    text: str
    atom_id: str
    quote: str
    reason: str          # "unknown_atom" | "quote_not_grounded"


@dataclass
class AnswerResult:
    # The synthesized prose answer (factra "living answer" model) — grounded in
    # the verified findings below; references them inline as [1], [2], …
    composed_answer: str = ""
    # A labeled, DESCRIPTIVE reading of any user-uploaded image (from the vision pre-step).
    # NOT a diagnosis, NOT a verified claim — surfaced separately so the UI can show it as
    # context; it only framed the search, it never entered the grounded answer/compose.
    visual_observation: str = ""
    verified_claims: list[VerifiedClaim] = field(default_factory=list)
    rejected_claims: list[RejectedClaim] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)   # vertical-signalled gaps
    # per-source contribution: which sources were retrieved vs. actually CITED in a
    # verified claim → shows what sources help answer (user-requested analytics).
    source_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    # SEARCH-SOURCE ATTRIBUTION: web engine -> {retrieved, cited, unique_cited} — which provider (exa/
    # brave/…) surfaced the web evidence that actually LANDED in the answer (relevance = cited/retrieved;
    # novelty = unique_cited, urls only that engine returned). Empty for non-web / single-provider runs.
    web_providers: dict[str, dict[str, int]] = field(default_factory=dict)
    steps: int = 0
    atoms_gathered: int = 0
    retried_empty: bool = False          # the extract recovery re-ask fired (observability)
    compose_failed: bool = False         # compose exhausted its retries → the answer is the fail note
    stopped_reason: str = "answered"     # "answered" | "budget" | "max_steps"
    effort: float = 1.0                  # the resolved effort multiplier this run used (observability)
    resolved_question: str = ""          # condensed self-contained question (set only if it differed)
    clarification: str = ""              # a clarifying question to ask instead of answering (ambiguous follow-up)
    charts: list = field(default_factory=list)   # validated grounded bar charts (dicts) for the UI
    derived_from_prior: bool = False     # answer is a transform of the PREVIOUS answer (no new retrieval)
    deep_synthesis_fell_back: bool = False   # DEEP SYNTHESIS: the prose-audit fell back to the non-deep
    #                                          compose because the deep prose kept an unsupported figure
    #                                          (observability; False unless deep_synthesis drove this run)
    # Grounded reasoning (flag ROSTER_DERIVE): gated, labeled derivations built FROM the verified claims —
    # each with a basis (finding indices), an epistemic label (inference/hypothesis/speculation) the gate
    # assigned, and a falsifier. Adds no fact; empty unless the derive flag is on (byte-identical OFF).
    derivations: list = field(default_factory=list)
    # Parametric-led (flag ROSTER_PARAMETRIC_LED, T2): asserted-model FACTS that could NOT be grounded
    # against retrieval — each {"text","needs_freshness"}. A labeled register (parity with `derivations`)
    # that T3 renders as "model asserts — not yet verified", NEVER merged into grounded prose. Empty on
    # every OFF (prior_draft is None) run, so the byte-identical OFF path never populates it.
    unverified_priors: list = field(default_factory=list)
    # Intelligence cruxes (flag ROSTER_INTELLIGENCE_CORE, T3): the falsifier(s) of the drafted competing
    # hypotheses — the concrete observable(s) that would flip the preferred read. A list of strings (the
    # model's falsifier text, labeled as "what would change this read", NOT a fact). Populated ONLY on an
    # intelligence run (hypotheses present) and empty otherwise, so the OFF answer stays byte-identical.
    intelligence_cruxes: list = field(default_factory=list)
    # Intelligence UNDER-TESTED (flag ROSTER_INTELLIGENCE_CORE, T-B): the competing hypotheses whose
    # disconfirming (AGAINST) search surfaced ZERO evidence — disconfirmation ATTEMPTED but not FOUND,
    # so the hypothesis is NOT confirmed, only not-yet-refuted (treat with caution). A list of
    # {"index": int, "claim": str}. Populated ONLY on an intelligence run (hypotheses present) whose
    # against-search came up empty; empty otherwise, so the OFF answer stays byte-identical.
    intelligence_undertested: list = field(default_factory=list)
    # Reasoning Read (flag): a purpose-driven analysis — a stated PURPOSE, the interpretation FACTORS
    # that bear on it, a converging CONCLUSION, and the 3-dimension confidence read. All empty/None
    # unless the reasoning-read flag drove the compose directive (byte-identical OFF).
    interpretation: list = field(default_factory=list)
    confidence: dict | None = None
    reasoning_purpose: str = ""
    reasoning_conclusion: str = ""
    # Troubleshooting trace (flag): per-turn steps, tool-call breakdown, the grounding funnel,
    # retries, and failures — None unless collect_diagnostics was requested (byte-identical OFF).
    diagnostics: dict | None = None
    # The derived QuestionContract, surfaced for SESSION persistence (schema-registry phase 0):
    # {"mode","entities","axes"} whenever a contract was derived (shadow OR steer), independent of
    # the diagnostics flag. None when no contract was derived (flag off / derivation failed).
    question_contract: dict | None = None
    # Freshness/currency disclosure (flag ROSTER_FRESHNESS_RANKING): {as_of, newest_year, oldest_year,
    # n_dated, n_total, stale_warning}. None unless a freshness policy drove this run — so the UI/answer
    # can show an as-of date and flag when the cited evidence predates the vertical's recency horizon.
    freshness: dict | None = None
    people_profiles: list = field(default_factory=list)
    # REFLECTION echo (flag ROSTER_REFLECTION=steer): {intent, answer_brief, confidence} when the pass
    # steered this answer — the app echoes it so the UI can show "here's what I understood" and so P2 is
    # prod-observable without the diag flag. Empty dict when reflection is off / low-confidence.
    reflection: dict = field(default_factory=dict)

    @property
    def grounded(self) -> bool:
        """True iff the delivered answer has ≥1 span-verified claim.

        Rejected (ungrounded) claims are caught by the gate and excluded from the
        answer — they're reported separately via `rejected_claims`, not a reason to
        call the surviving verified claims ungrounded. A pure refusal (0 verified)
        or an all-fabricated answer (0 verified, ≥1 rejected) is not grounded.
        """
        return bool(self.verified_claims)


async def run_react(
    *,
    question: str,
    llm: LLMClient,
    embedder: Embedder,
    source: RetrievalSource,
    tenant_id: str,
    workspace_id: str | None = None,
    budget: BudgetState,
    gating: GatingPolicy | None = None,
    system_prompt: str = "You are an evidence-grounded research agent.",
    answer_format: str | None = None,
    attachment_context: str | None = None,
    history_context: str | None = None,
    planner_llm: LLMClient | None = None,     # fast model for search-planning steps (compose uses `llm`)
    on_event=None,                            # optional async callback(dict) for live progress (SSE)
    aux_source: RetrievalSource | None = None,  # e.g. web: queried ONCE per step (no variant fan-out)
    claims_first: bool = False,               # run comprehensive extraction over ALL atoms (flag)
    extraction_lenses: tuple[str, ...] = (),  # vertical-supplied lenses for the extractor
    evidence_select: bool = False,            # rank claims by relevance before the cap + wider atom window
    atom_cap: int = 1600,                     # per-atom char window for the extractor (evidence-select raises it)
    facets: dict | None = None,               # hard retrieval facet filter (empty {} = no filter, byte-identical)
    max_steps: int = 8,
    max_extract_recoveries: int = 3,          # bound on the empty-answer forceful re-extract re-asks
    #                                           (default preserves behavior; panel lenses pass 1)
    compose_attempts: int = _COMPOSE_ATTEMPTS,  # bound on compose retries (default preserves behavior)
    k: int = 10,
    planner_atom_window: int = 60,            # atoms SHOWN to the planner per step (store keeps all)
    compose_claim_cap: int = _COMPOSE_CLAIM_CAP,  # max verified findings sent to compose (effort-scalable)
    extract_collect: int = _EXTRACT_COLLECT,      # candidate pool before relevance-ranking (effort-scalable)
    answer_focus: bool = False,               # ANSWER the question + scope to its subject (vs compile findings)
    reasoning_read: bool = False,             # surface the validated interpretation + confidence layer (flag)
    readable_prose: bool = False,             # plain-language WRITING-STYLE layer over compose (flag) —
    #                                           readability only; changes no section/structure/citation
    collect_diagnostics: bool = False,        # capture a troubleshooting trace (turns/tools/retries/failures)
    classify_evidence=None,                   # vertical hook (source_key, facets) -> evidence_kind str (Rule 18: structural)
    evidence_fitness: bool = False,           # boost stronger evidence tiers into the compose cap (flag)
    axis_complete: bool = False,              # flag: make compose ADDRESS EACH derived contract axis
    #                                           (aspect the reader asked about) + lead with a synthesized
    #                                           take, instead of surveying only what was found. Needs a
    #                                           derived question_contract with axes; otherwise a no-op.
    tech_synthesis: bool = False,             # flag: add a strategic 'how it works' TECHNICAL SYNTHESIS
    #                                           from the evidence (disclosed cited + labeled likely-design
    #                                           inference). Compose skips it for non-technical subjects.
    deep_synthesis: bool = False,             # flag ROSTER_DEEP_SYNTHESIS: synthesis-first grounded answer.
    #                                           When on for a NON-lookup question: the deep format is the
    #                                           compose base, the per-kind cap is raised, grounded
    #                                           derivations are woven into the compose spine, and a
    #                                           corrective prose grounding-audit runs post-compose.
    deep_answer_format: str | None = None,    # vertical's deep-synthesis compose format (the deep base).
    prior_draft=None,                         # ROSTER_PARAMETRIC_LED (T1): the pre-retrieval PriorDraft for
    #                                           a parametric-eligible question. DECLARED-BUT-UNUSED here in
    #                                           T1 (threaded inertly) — T2 verifies each asserted fact
    #                                           against the span-gate + binding, T3 composes from the
    #                                           verified + labeled-unverified register. None → today's path.
    hypotheses=None,                          # ROSTER_INTELLIGENCE_CORE (T1): the parsed competing
    #                                           Hypotheses for an intelligence-eligible question. INERT —
    #                                           declared-but-unused in T1 (zero body references); T2 adds
    #                                           the FOR/AGAINST adversarial retrieval legs and T3 the
    #                                           hypotheses-as-frame compose. None → today's path byte-identical.
    intelligence_frame=None,                  # ROSTER_INTELLIGENCE_CORE (T1): the drafted analytical frame
    #                                           (prose) paired with `hypotheses`. INERT until T3 compose.
    kind: str = "",                           # question kind (management/lookup/understanding); "" → treated
    #                                           as non-lookup by the deep gate (best-effort; a clear lookup
    #                                           carries kind="lookup" from the reasoned scaffold).
    derive_ideas: bool = False,               # deep-weave: also generate grounded 'opportunity' ideas in derive
    derive_judge_llm=None,                     # deep-weave: optional cross-family validity judge (else reuses llm)
    deep_company: bool = False,                # flag ROSTER_DEEP_COMPANY_READER: additive first-step web
    #                                           dossier leg driven by vertical-supplied templates. The
    #                                           kernel only handles bounded retrieval and BlockHit merge.
    company_reader: dict | None = None,        # vertical company-reader config (opaque templates/addenda).
    deep_person: bool = False,                 # flag ROSTER_DEEP_PEOPLE_READER: additive first-step web
    #                                           dossier leg for one person. The reader returns hits +
    #                                           profile links; only hits enter the atom store.
    person_reader: dict | None = None,         # vertical person-reader config (opaque templates/addenda).
    entity_open_web: bool = False,            # flag: fire ONE additive open-web Exa probe on step 0 for
                                              # single-entity questions (contract.subject_kind), quality-screened
    web_open_denoise: bool = False,           # flag ROSTER_WEB_OPEN_DENOISE: open the aux web leg to the FULL
                                              # web + screen ALL its hits through the denoising funnel
    web_quality_prompt: str | None = None,    # vertical-supplied judge prompt for the open-web quality screen
    evidence_ranker=None,                     # vertical hook: evidence_kind -> int rank (the authority pyramid)
    authority_basis: bool = False,            # ROSTER_AUTHORITY_BASIS (flag): UNCONDITIONAL stable partition
    #                                           of the verified-claim pool — low-basis claims (tier rank<=1:
    #                                           unattributed blog / social) pushed to the BACK so authoritative
    #                                           tiers fill the compose cap first; cosine/first-come order is
    #                                           preserved WITHIN each bucket. Reorder ONLY (never drops — breadth
    #                                           preserved). Runs regardless of pool-vs-cap, unlike the boost
    #                                           ranker. Gated `authority_basis and not _suppress_auth and
    #                                           evidence_ranker is not None`. OFF / suppress / no ranker →
    #                                           byte-identical claim order. Also appends the authority-basis
    #                                           compose directive (below) under the same gate.
    authority_basis_directive: str | None = None,  # vertical compose FLOOR directive (opaque prose): ground
    #                                           facts in the highest-tier source; opinion/blog/social are
    #                                           supplementary signal, never the sole basis for a fact. Appended
    #                                           to the compose directive only under the same gate. None → nothing
    #                                           to append (byte-identical).
    evidence_identity: bool = False,          # Evidence Contract stage 1: render each atom's document
    #                                           identity ⟨title — source⟩ on every LLM-visible surface
    #                                           (planner obs, extractor, entailment, compose, fallback
    #                                           grounder) + require subject-faithful attribution. OFF →
    #                                           every prompt string byte-identical to today.
    claim_congruence: bool = False,           # Evidence Contract stage 2: ONE unified batched BINDING
    #                                           judge over ALL THREE claim paths (loop-emitted,
    #                                           claims-first, fallback-grounder). Per claim it judges
    #                                           {entailed, on_subject, kind_ok}: off-subject or
    #                                           unentailed → DROP; kind-mismatch → keep + demote +
    #                                           annotate; judge unavailable → keep + "unjudged" (never
    #                                           drop on judge failure, never a keyword fallback). OFF →
    #                                           stage-1 prompts/enforcement, byte-identical.
    country_boost=None,                       # set of country codes to boost (surface region evidence, no filter)
    freshness: dict | None = None,            # vertical freshness policy {min_rank,weight,horizon_years}
    #                                           (flag ROSTER_FRESHNESS_RANKING). Re-orders the claim pool by
    #                                           recency across all tiers + drives the as-of/staleness
    #                                           disclosure below. None → byte-identical to today.
    answer_profiles: dict | None = None,      # ANSWER-CONTRACT (flag ROSTER_ANSWER_CONTRACT): {stance:
    #                                           profile} map. The derived contract's `stance` selects a
    #                                           profile whose knobs (recency / suppress_authority /
    #                                           web_recency_days / planner_steer / answer_directive)
    #                                           customize retrieval+ranking+compose PER QUESTION. When a
    #                                           profile supplies `recency` it OVERRIDES the static
    #                                           `freshness` above. None / no match → today's behavior.
    exclude_facets: dict | None = None,       # EXCLUSION facet filter applied to every retrieval leg
    graph_legs: list[dict] | None = None,     # A9 graph-guided evidence legs: [{query, note}] from the
    #                                           relationship graph (caller-computed). Run ONCE before the
    #                                           loop as extra retrieval; merged atoms flow through the
    #                                           SAME ranking/floors/span gate. Graph text NEVER enters
    #                                           any prompt — only real retrieved blocks do.
    graph_shadow: bool = False,               # shadow-counterfactual: run+log legs, merge NOTHING
    graph_late: bool = False,                 # LATE merge: stash leg hits during the loop (planner
    #                                           runs byte-identical to graph-off — no early-stop
    #                                           possible), merge them post-loop just before the
    #                                           claims-first extraction. Purely additive evidence.
    question_contract: str = "",              # Evidence Contract stage 3 (flag mode): "" off
    #                                           (byte-identical); "shadow" → derive the question's
    #                                           evidence contract + compute the per-entity legs +
    #                                           log them (diag/SSE) — NO leg retrieval, NO
    #                                           selection change (zero behavior change beyond +1
    #                                           small charged LLM call); "steer" → enumerative
    #                                           contracts execute the legs (cap 8, k=4 each,
    #                                           concurrent, LATE-merged like graph legs), compose
    #                                           selection reserves seats for slot-filling claims,
    #                                           and entities left with zero claims become honest
    #                                           loop-produced coverage gaps.
    contract_prompt: str | None = None,       # vertical-supplied contract-derivation directive
    #                                           (ALL domain vocabulary lives there — kernel litmus);
    #                                           None → no contract derived (flag effectively off)
    reflection: str = "",                     # reflection pass (ROSTER_REFLECTION flag): "" off
    #                                           (byte-identical); "shadow" → derive enriched reflection
    #                                           + LOG the on-demand web-coverage legs it WOULD fire, change
    #                                           nothing; "steer" → intent steer (confidence>=medium) + fan
    #                                           out BOUNDED web coverage legs for landscape/multi-entity
    #                                           questions (the "muted: didn't look" fix), late-merged +
    #                                           screened like the existing web:deep legs
    explore_legs: bool = False,               # exploratory-legs extension (flag, default OFF):
    #                                           EXPLORATORY contracts now carry axes (the vertical
    #                                           derives them) and, under this flag, get AXIS-ONLY
    #                                           retrieval legs (cap 4, each axis verbatim) executed
    #                                           under the SAME steer gate + late-merge seam as
    #                                           enumerative legs. OFF → exploratory legs are never
    #                                           built (diag/SSE/retrieval byte-identical to today
    #                                           even though the derived contract carries axes).
    #                                           No slot grid / coverage gaps / seat reservation
    #                                           for exploratory in this version (retrieval only).
    answer_mode_routing: bool = False,        # Evidence Contract stage 4 (flag): route ENUMERATIVE
    #                                           questions to an enumerative compose framing. Fires
    #                                           ONLY when (a) this flag is on, (b) the derived
    #                                           QuestionContract says mode=enumerative, AND (c) ≥2
    #                                           contract entities hold ≥1 slot-matched claim in the
    #                                           FINAL verified selection (panel A3: never trust the
    #                                           pre-retrieval contract alone for compose routing) —
    #                                           then the vertical's addendum below is APPENDED to
    #                                           the existing compose directive. The base directive
    #                                           is UNTOUCHED; OFF / not fired → compose prompt is
    #                                           byte-identical to today.
    enumerative_compose_addendum: str | None = None,  # vertical-owned enumerative-compose addendum —
    #                                           an OPAQUE caller-supplied string (manifest field;
    #                                           kernel litmus: zero domain vocabulary here).
    #                                           None/"" → stage-4 routing never fires.
    contract_compose: bool = False,            # ROSTER_CONTRACT_COMPOSE (voice ⟂ shape): when on, the
    #                                           compose directive is RENDERED FROM the derived contract —
    #                                           VOICE + the SHAPE for the contract's mode — instead of the
    #                                           flat golden directive. The enumerative shape gets the
    #                                           contract's concrete items+dimensions appended (structural).
    #                                           Replaces answer_format at compose. OFF → byte-identical.
    contract_compose_voice: str | None = None,
    contract_compose_shapes: dict | None = None,     # {mode: opaque directive}; missing → *_default
    contract_compose_default: str | None = None,     # shape for decision/analytical/unmapped modes
    web_only: bool = False,                    # ROSTER_WEB_ONLY: the web is the ONLY source (no corpus), so
    #                                           EVERY question must be researched thoroughly from the web —
    #                                           append a research-hard planner steer + raise the step floor
    #                                           so lookups/people/analytical questions don't stop after one
    #                                           shallow pass (the "answers are too short" fix). OFF → no-op.
    enum_entity_probe: bool = False,           # ROSTER_ENUM_ENTITY_PROBE: for an enumerative "table of the
    #                                           main X" ask with no user-named items, use the contract's
    #                                           model-proposed `probe_entities` to fire a TARGETED
    #                                           entity×axis retrieval leg per candidate (fixes well-covered
    #                                           flagships crowded out of axis-only retrieval). Seeds
    #                                           RETRIEVAL only — never rows. OFF → axis-only (byte-identical).
    suppress_authority: bool = False,          # per-call authority-neutralize (use-case lens): when True,
    #                                           the evidence-tier boost is dropped so opinion/discussion
    #                                           evidence isn't demoted below filings on foresight/wisdom
    #                                           queries. ORs with the stance profile's suppression. Default
    #                                           False → today's behavior (byte-identical).
    source_routing: bool = False,              # SOURCE ROUTING (flag ROSTER_SOURCE_ROUTING): when on, the
    #                                           planner may emit AgentStep.source_kinds to ADD a scoped
    #                                           retrieval leg (never a filter). Off → the field is ignored
    #                                           and no scoped leg runs (byte-identical).
    retrieval_source_cap: float | None = None,  # SOURCE-DIVERSITY cap (flag ROSTER_RETRIEVAL_DIVERSITY):
    #                                           per multi-query fusion, cap any one source_key to
    #                                           ceil(k*frac) of the top-k pool (backfill preserves
    #                                           recall) so a volume-skewed source can't crowd out
    #                                           other sources on broad queries. None → byte-identical.
    golden_answer: bool = False,               # ROSTER_GOLDEN_ANSWER (flag): the answer-shaping STACK is
    #                                           collapsed to ONE compose directive (the vertical's golden
    #                                           directive, wired as `answer_format` with all 8 layer flags
    #                                           OFF at the app boundary) so the prose is one clean freeform
    #                                           answer with no narrated scaffolding. This flag ONLY re-binds
    #                                           the two PROSE grounding audits (hard-token recompose + the
    #                                           cross-family semantic gate) so the freer golden prose is
    #                                           still policed even though deep_synthesis/hypotheses are OFF.
    #                                           OFF → both audits keep today's triggers (byte-identical).
) -> AnswerResult:
    import asyncio
    atoms = AtomStore()
    result = AnswerResult()

    # DEEP SYNTHESIS (flag) — per-kind compose cap. A synthesis-first answer needs a wider evidence
    # base than the default 30; RAISE the cap (never lower) so the higher value is in effect for both
    # the extraction-collection cap and the final claim-selection cap below. Only for non-lookup deep
    # runs — lookups (and OFF) keep the default cap, byte-identical.
    # GOLDEN also earns the wider cap: a golden answer synthesizes the full picture, so it should draw on
    # up to 48/60 grounded findings, not the default 30 (the thinness limiter when material is rich).
    if (deep_synthesis or deep_company or deep_person or golden_answer) and kind != "lookup":
        _deep_cap = 48 if kind == "understanding" else 60   # management / other-non-lookup → 60
        compose_claim_cap = max(int(compose_claim_cap), _deep_cap)

    def _atom_render(a) -> str:
        """Atom text as handed to claim-writing LLM surfaces (claims-first extractor, fallback
        grounder): identity-tag-prefixed under the evidence-identity flag. OFF (or no title) →
        the raw text, byte-identical to today."""
        if not evidence_identity:
            return a.text
        tag = identity_tag(a)
        return f"{tag} {a.text}" if tag else a.text

    notes: list[str] = []          # running coverage-gap / step notes for the agent
    # Troubleshooting trace (flag): built ONLY when requested, purely from data already flowing through
    # the loop (no extra LLM calls). None → byte-identical OFF path.
    _diag_t0 = time.monotonic() if collect_diagnostics else None
    diag = ({"trace": [], "retries": {"compose": 0, "compose_ref_retry": False, "extract_recovery": 0},
             "failures": [], "compose_calls": 0, "timing": {}} if collect_diagnostics else None)
    # factra-style per-call latency capture: every Anthropic complete() this request appends to this
    # list, so the diagnostics can attribute wall-clock to individual LLM calls (zero cost when off).
    _call_log_tok = None
    if collect_diagnostics:
        from roster_kernel.providers.anthropic_llm import LLM_CALL_LOG as _LLM_CALL_LOG
        _call_log = []
        _call_log_tok = _LLM_CALL_LOG.set(_call_log)

    def _timed(name, coro):
        """Accumulate an awaitable's wall-clock into diag['timing'][name] (factra-style phase timing).
        No-op passthrough when diagnostics are off. Splits the non-LLM time (retrieval/embed/OpenAI
        judge) that the per-Anthropic-call log can't see."""
        if diag is None:
            return coro
        async def _run():
            _pt = time.perf_counter()
            try:
                return await coro
            finally:
                diag["timing"][name] = diag["timing"].get(name, 0) + int((time.perf_counter() - _pt) * 1000)
        return _run()
    # The span-verifier's block loader must cover EVERY source a claim can cite — corpus AND aux
    # (web). Since search is split (corpus multi-query + aux single-query), combine their loaders
    # so a web-cited quote is still verifiable (else all web claims would be rejected).
    _corpus_loader = source.make_block_loader(tenant_id, workspace_id)
    if aux_source is not None:
        _aux_loader = aux_source.make_block_loader(tenant_id, workspace_id)
        def _combined_loader(document_id: str, block_id: str):
            t = _corpus_loader(document_id, block_id)
            return t if t is not None else _aux_loader(document_id, block_id)
        verifier = BlockSpanVerifier(_combined_loader)
    else:
        verifier = BlockSpanVerifier(_corpus_loader)
    planner = planner_llm or llm   # planning steps can use a cheaper/faster model than compose

    async def emit(ev: dict) -> None:
        if on_event is not None:
            try:
                await on_event(ev)
            except Exception:
                pass               # progress events are best-effort; never break the research loop

    # Labeled user-provided context (image reading and/or uploaded-document text) for the
    # step prompts ONLY (search + reasoning framing). It is deliberately kept OUT of the
    # question string and the compose step, so attachment content can never surface as if
    # it were a grounded corpus finding.
    att = (attachment_context or "").strip()
    img_ctx = (
        f"USER-PROVIDED CONTEXT (from an uploaded image and/or document; NOT corpus "
        f"evidence — use it ONLY to decide what to search for and how to interpret "
        f"findings; NEVER cite it as a source or a verified claim):\n"
        f"{att}\n\n"
        if att else ""
    )
    # Prior conversation turns (for a FOLLOW-UP question). Context ONLY — it lets the agent resolve
    # an elliptical follow-up ("what about in children?") against what was already discussed. Like
    # image/doc context, it NEVER becomes a grounded claim and never enters the compose step.
    conv = (history_context or "").strip()
    conv_ctx = (
        f"CONVERSATION SO FAR (prior questions and answers in this thread; context to interpret "
        f"the CURRENT question — NOT corpus evidence, NEVER cite it as a source or verified claim):\n"
        f"{conv}\n\n"
        if conv else ""
    )

    def _classify_atom(atom) -> str:
        """The cited atom's structural evidence tier (best-effort; classification is a structural
        vertical hook — a bad/absent classifier never breaks the answer). Also feeds the stage-2
        binding judge's SOURCE line (the kind label rides along as data, never prompt vocabulary)."""
        if classify_evidence is None:
            return ""
        try:
            return classify_evidence(atom.source_key, atom.facets, atom.document_title, atom.text) or ""
        except Exception:   # noqa: BLE001 — classification must never break grounding
            return ""

    def _mk_verified(text: str, atom_id: str, quote: str, atom) -> VerifiedClaim:
        """Build a VerifiedClaim, stamping the cited atom's facets + evidence tier."""
        return VerifiedClaim(text, atom_id, quote, atom.source_key, atom.document_title,
                             atom.document_id, facets=dict(atom.facets or {}),
                             evidence_kind=_classify_atom(atom))

    # Evidence Contract stage 2: shared enforcement bookkeeping for the binding judge. Counts live
    # in diag["congruence"] (only when the trace is already enabled); off-subject drops are also
    # logged to the trace with reason "off_subject" so a wrong answer is debuggable without a rerun.
    def _congruence_count(key: str) -> None:
        if diag is not None:
            diag.setdefault("congruence", {"judged": 0, "off_subject": 0, "not_entailed": 0,
                                           "kind_mismatch": 0, "unjudged": 0})[key] += 1

    def _log_off_subject(origin: str, text: str, title: str) -> None:
        _log.info("congruence: off-subject claim dropped (%s): %r ⟨%s⟩", origin, text[:120], title[:80])
        if diag is not None:
            diag["trace"].append({"action": "congruence_drop", "reason": "off_subject",
                                  "origin": origin, "claim": text[:160], "title": (title or "")[:80]})

    def _apply_answer(step: AgentStep) -> None:
        for c in step.claims:
            atom = atoms.get(c.atom_id)
            if atom is None or atom.locator is None:
                result.rejected_claims.append(RejectedClaim(c.text, c.atom_id, c.quote, "unknown_atom"))
            elif verifier.verify(c.quote, atom.locator):
                result.verified_claims.append(_mk_verified(c.text, c.atom_id, c.quote, atom))
            else:
                result.rejected_claims.append(RejectedClaim(c.text, c.atom_id, c.quote, "quote_not_grounded"))

    searched_queries: list[str] = []   # every query/reformulation issued — shown to the planner so it
    #                                    doesn't re-search the same ground (the repeated-search fix)

    async def _ask(mode: str = "step") -> AgentStep:
        # Show the planner only the most-recent window of atoms (the store keeps ALL for grounding /
        # verification) — keeps late-step prompts from snowballing. Claims can cite only shown atoms.
        _all = atoms.all()
        _shown = _all[-planner_atom_window:] if len(_all) > planner_atom_window else _all
        if evidence_identity:
            # Evidence Contract stage 1: each atom carries its document identity ⟨title — source⟩ so
            # the planner attributes claims to the source's actual subject. Tagless atoms (no title)
            # render exactly as today.
            def _obs_line(a) -> str:
                tag = identity_tag(a)
                return f"{a.atom_id} {tag}: {a.text}" if tag else f"{a.atom_id}: {a.text}"
            obs = "\n".join(_obs_line(a) for a in _shown) or "(no evidence yet)"
        else:
            obs = "\n".join(f"{a.atom_id}: {a.text}" for a in _shown) or "(no evidence yet)"
        if mode == "extract":
            # DEDICATED extraction recovery: the agent answered with NO claims even though relevant
            # evidence exists. This prompt does NOT reuse the permissive discipline below — when
            # evidence exists an empty answer is INVALID here (the #1 abstention cause). Provenance
            # is unchanged: every emitted claim still passes the verbatim span-check.
            instr = (
                "You returned an EMPTY claims list, but relevant evidence IS gathered above. When "
                "relevant evidence exists, an empty answer is INVALID — you MUST extract the facts it "
                "directly supports. Emit at least one claim for EACH directly-relevant atom you can "
                "(more is better); a PARTIAL answer is correct and expected — do not withhold because "
                "the question asks for a ranking/recommendation/completeness the evidence can't fully "
                "settle. For each claim: cite the atom_id and copy a 'quote' that is an EXACT SUBSTRING "
                "of that atom's text — byte-for-byte, including every comma/period/number/unit; do NOT "
                "paraphrase, trim words, or change punctuation (a non-exact quote is DISCARDED). action MUST be "
                "'answer'; do NOT search. Format example (STRUCTURE ONLY — do not reuse these words): "
                '{"action":"answer","claims":[{"text":"A trial evaluates drug X for condition Y.",'
                '"atom_id":"a3","quote":"a verbatim span copied from atom a3"}]}')
        elif mode == "force":
            instr = ("You have reached the evidence-gathering limit. You MUST now "
                     "action='answer'. Do NOT search.")
        else:
            instr = ("Either action='search' with a FOCUSED `query` for THIS step (plus optional "
                     "reformulations in 'queries') to gather NEW evidence, or action='answer' with claims. "
                     "If the queries already tried (below) are not turning up new relevant evidence, do "
                     "NOT keep repeating them — either search a genuinely DIFFERENT angle/subtopic, or "
                     "answer with what you have.")
        # Shared answering discipline: report what the evidence DIRECTLY supports (partial is
        # fine — the synthesis notes what isn't), and copy quotes VERBATIM so the span-check
        # passes. This is the fix for advice/ranking questions where the model would otherwise
        # abstain wholesale despite holding relevant evidence.
        discipline = (
            " When you answer, report EVERY fact the evidence DIRECTLY supports — even if it "
            "only PARTIALLY answers the question, or cannot satisfy a ranking, recommendation, "
            "or 'which is best/safest' the question implies (report the supported facts; the "
            "synthesis will note what is not supported). A partial grounded answer is far better "
            "than none. Each claim must cite an atom_id and a 'quote' that is an EXACT SUBSTRING of "
            "that atom: copy the characters byte-for-byte, INCLUDING every comma, period, capital, "
            "number, and unit. Do NOT paraphrase, summarize, reformat, trim trailing words, add or "
            "remove punctuation, or 'fix' anything — if unsure where a span ends, copy a LONGER exact "
            "span. A quote that is not an exact substring of its atom WILL BE DISCARDED and the claim "
            "lost. Return an empty claims list ONLY if NONE of the gathered evidence is "
            "relevant to the question.")
        # Evidence-identity flag (stage 1): ONE added sentence — claims must be attributed to their
        # source's actual subject (the atoms above carry ⟨title — source⟩ tags). OFF → byte-identical.
        if evidence_identity:
            discipline = discipline + " " + IDENTITY_INSTRUCTION
        # extract mode is self-contained + forceful — do NOT append the permissive discipline (its
        # "empty ONLY if NONE relevant" clause is the loophole the recovery must override).
        if mode != "extract":
            instr = instr + discipline
        # ANSWER-CONTRACT planner steer: the resolved stance profile nudges WHAT to search for this
        # question (e.g. "current" → also search the most recent releases/announcements; "established"
        # → prefer benchmarked/peer-reviewed sources). "" → byte-identical. Not applied in extract mode.
        if _steer and mode != "extract":
            instr = instr + " " + _steer
        if _wo_steer and mode != "extract":       # web-only: research every question thoroughly
            instr = instr + " " + _wo_steer
        # REFLECTION intent steer: nudge the planner toward the user's REAL intent (WHAT to search).
        # "" → byte-identical. Not applied in extract mode.
        if _reflect_steer and mode != "extract":
            instr = instr + " " + _reflect_steer
        # SOURCE ROUTING (flag): tell the agent it MAY name source TYPES to also target for a query. This
        # ADDS a scoped leg on top of the flat search (never restricts it), so route toward the source
        # types most likely to hold the discriminator for THIS sub-question. Off → not mentioned.
        if source_routing and mode != "extract":
            instr = instr + (
                " Optionally also set `source_kinds` (a list) to ALSO target specific SOURCE TYPES for "
                "this query — use the source-type names described in your instructions, choosing the "
                "one(s) most likely to hold the answer to THIS sub-question. Leave it empty to search "
                "all sources; it only ADDS a targeted probe, it never removes the broad search.")
        # One fresh user message per step (all evidence so far). Ends with a user
        # turn — required by chat LLMs — and keeps the agent stateless per step.
        # img_ctx (if any) frames the search but is never merged into `question` (so it
        # stays out of the compose step and can't read as a grounded finding).
        # QUERIES ALREADY TRIED — so the planner searches new ground instead of re-issuing near-identical
        # queries (the repeated-search / diminishing-returns fix). Deduped + capped to keep the prompt bounded.
        tried = list(dict.fromkeys(searched_queries))[-16:]
        tried_ctx = ("QUERIES ALREADY TRIED (do NOT repeat these — search a DIFFERENT angle or answer):\n"
                     + "\n".join(f"- {t}" for t in tried) + "\n\n") if tried else ""
        user = (conv_ctx + img_ctx + f"Question: {question}\n\nEVIDENCE GATHERED SO FAR:\n{obs}\n\n"
                + tried_ctx + ("NOTES:\n" + "\n".join(notes) + "\n\n" if notes else "") + instr)
        # NOTE: temperature is intentionally NOT set — the current model rejects it
        # ("deprecated for this model"). Variance is countered by the answering
        # discipline above + the extract recovery re-ask, not by sampling controls.
        res = await planner.complete(system=system_prompt,
                                     messages=[{"role": "user", "content": user}],
                                     response_format=AgentStep, max_tokens=_PLANNER_MAX_TOKENS)
        budget.charge(calls=1, tokens=res.output_tokens)
        result.steps += 1
        return res.parsed

    async def _finalize_answer(step: AgentStep) -> None:
        """Apply the answer's claims through the provenance gate, then — if the agent emitted NOTHING
        (0 verified AND 0 rejected) while it had gathered evidence — retry a DEDICATED forceful
        extraction up to a few times before giving up. This is the fix for run-to-run abstention:
        the model sometimes samples an empty answer despite relevant evidence; a single re-ask (the
        old behavior) reproduced it. The guard only ever runs in the already-failing 0-verified/
        0-rejected path — it never touches a run that produced verified or rejected claims, never
        weakens the span gate (claims still pass verify()), and is bounded by attempts + budget."""
        _apply_answer(step)
        attempts = 0
        while (not result.verified_claims and not result.rejected_claims
               and atoms.all() and not budget.exhausted and attempts < max_extract_recoveries):
            attempts += 1
            try:
                budget.reserve()
            except BudgetExceeded:
                break
            result.retried_empty = True              # observability: the recovery fired
            retry = await _ask(mode="extract")
            if retry.action == "answer":
                _apply_answer(retry)
            # if it returned action="search" (ignoring the extract instruction), loop and re-ask
            # extract — bounded by `attempts`/budget so a stubborn model can't spin forever.
        if diag is not None and attempts:
            diag["retries"]["extract_recovery"] = attempts

    # A9 GRAPH-GUIDED EVIDENCE LEGS (flagged; caller computes the legs from the relationship
    # graph). Deterministic pre-loop retrieval on ≤2 edge-templated queries — the multi-hop
    # evidence the question's own wording can never reach (CKD-fatigue → anemia guideline).
    # Merged atoms are ordinary evidence: same ranking, same span gate, citable because they
    # are REAL retrieved blocks. The graph itself contributes no prompt text — the planner
    # only ever sees retrieved evidence, so "graph steers search, never cites" holds
    # structurally. Shadow mode retrieves + logs and merges nothing (counterfactual telemetry).
    _g_stash: list[tuple[dict, list]] = []     # late mode: (leg, hits) held back until post-loop
    if graph_legs:
        _g_diag: list[dict] = []
        _g_mode = "shadow" if graph_shadow else ("late" if graph_late else "early")
        for _leg in list(graph_legs)[:2]:
            _gq = (_leg.get("query") or "").strip()
            if not _gq:
                continue
            try:
                _gvec = await _timed("embed_ms", asyncio.to_thread(lambda q=_gq: list(embedder.embed([q])[0])))
                _g_hits = await _timed("graph_legs_ms", source.search(RetrievalRequest(
                    query=_gq, tenant_id=tenant_id, workspace_id=workspace_id,
                    query_embedding=_gvec, k=max(4, k // 2), facets=dict(facets or {}),
                    exclude_facets=dict(exclude_facets or {}))))
            except Exception as _ge:   # noqa: BLE001 — a dead leg never breaks the answer
                _log.warning("graph leg failed on %r: %s", _gq, _ge)
                _g_hits = []
            _merged = 0
            if _g_mode == "early" and _g_hits:
                _before = len(atoms.all())
                atoms.add_hits(_g_hits)
                _merged = len(atoms.all()) - _before
                searched_queries.append(_gq)   # planner sees it as tried — no duplicate searching
            elif _g_mode == "late" and _g_hits:
                _g_stash.append((_leg, _g_hits))   # planner never sees these — loop is byte-
                #                                    identical to graph-off (no early-stop)
            _g_diag.append({"query": _gq, "note": str(_leg.get("note", ""))[:120],
                            "hits": len(_g_hits), "merged": _merged})
        if _g_diag:
            _log.info("graph legs (%s): %s", _g_mode,
                      [(d["query"], d["hits"], d["merged"]) for d in _g_diag])
            if diag is not None:
                diag["graph_legs"] = {"shadow": graph_shadow, "mode": _g_mode, "legs": _g_diag}
            await emit({"type": "graph_legs", "shadow": graph_shadow, "mode": _g_mode,
                        "queries": [d["query"] for d in _g_diag]})
            if _g_mode == "early" and any(d["merged"] for d in _g_diag):
                # planner-only note (never reaches compose): pre-gathered adjacent-topic
                # evidence SUPPLEMENTS the question — it must not replace searching it.
                notes.append("Background evidence on closely-related topics was pre-gathered "
                             "(see atoms above). It supplements the question — still SEARCH the "
                             "question itself before answering.")

    # EVIDENCE CONTRACT stage 3 (question-contract flag): derive the question's evidence CONTRACT
    # (ONE small charged LLM call on the vertical-supplied prompt; fail-safe None → today's
    # behavior) and expand an ENUMERATIVE contract into per-entity retrieval legs — round-robin
    # across entities, capped at 8, deduped against the graph legs' [:2] (one unified leg budget
    # of 10). SHADOW: log the contract + computed legs (diag/SSE), retrieve NOTHING, alter
    # NOTHING — the confident-wrong contract must be observable before it may steer. STEER:
    # execute the legs CONCURRENTLY as separate RetrievalRequests (k=4 each — NEVER through
    # multi_query_retrieve's single fused pool, which would truncate all entities to one k-pool
    # and silently starve most of them) and STASH the hits for the same post-loop late-merge seam
    # as graph legs, so the planner window is unaffected and claims-first mines them. Baseline
    # retrieval (the planner's own searching) is unchanged and mandatory in every mode.
    # EXPLORATORY-LEGS extension (explore_legs flag): exploratory contracts with axes get
    # AXIS-ONLY legs (cap 4) through the SAME build/steer-execute/late-merge path — but ONLY
    # when explore_legs is on; OFF strips them right here so nothing downstream can consume them.
    _contract = None
    _c_stash: list[tuple[str, list]] = []       # steer: (query, hits) held back until post-loop
    # derive the contract when the QuestionContract flag is on OR the ANSWER-CONTRACT needs a stance OR a
    # DEEP READER is on (the deep company/person readers ROUTE on the contract's subject_kind + entities,
    # so they need it derived — otherwise golden, which zeros answer_profiles, leaves _contract None and
    # the deep readers can never fire) OR REFLECTION is on (the web-coverage fan-out + intent steer route
    # on the contract's subject_kind/entities/axes, so they need it derived too).
    if ((question_contract in ("shadow", "steer")) or answer_profiles or deep_company or deep_person
            or reflection in ("shadow", "steer") or contract_compose) \
            and (contract_prompt or "").strip():
        from roster_kernel.research.contract import build_legs, derive_contract
        try:
            budget.reserve()
            # contract-compose drives the ANSWER SHAPE off this contract, so it must be STABLE — vote
            # (self-consistency, best-of-3) to remove the run-to-run flip that muted mixed questions ~1/3
            # of the time. Other paths keep the single call (byte-identical). The votes run concurrently.
            _votes = 3 if contract_compose else 1
            budget.charge(calls=_votes)         # the derivation call(s) (BudgetState honesty)
            _contract = await derive_contract(question, planner, contract_prompt, votes=_votes)
        except BudgetExceeded:
            _contract = None                    # over budget → no contract → today's behavior
        if _contract is not None:               # persistable contract record (schema-registry
            result.question_contract = {        # phase 0) — independent of the diagnostics flag
                "mode": _contract.mode,
                "entities": list(_contract.entities),
                "axes": list(_contract.axes),
                "stance": _contract.stance}
        _c_graph_qs = {(_l.get("query") or "").strip() for _l in (graph_legs or [])[:2]}
        # cap 12 → 30: a probe ROSTER (15-25 named companies for a comparison table) needs one targeted
        # leg per company; at cap 12 the axis-only legs crowded the roster down to ~7 sourced companies.
        # build_legs still caps non-probe modes tightly (min(cap,4) for axis-only); only the probe branch
        # spends the wider budget (one bundled query per named company).
        _c_queries = build_legs(_contract, cap=30, exclude=_c_graph_qs, probe=enum_entity_probe)
        if _contract is not None and _contract.mode == "exploratory" and not explore_legs:
            _c_queries = []                     # exploratory legs exist ONLY under the
            #                                     explore_legs flag — OFF must stay byte-identical
            #                                     to today (no diag/SSE/retrieval trace of them),
            #                                     even though the contract now carries axes
        _c_diag: dict = {"mode": question_contract,
                         "contract": (None if _contract is None else
                                      {"mode": _contract.mode,
                                       "entities": list(_contract.entities),
                                       "axes": list(_contract.axes)}),
                         "legs": [{"query": q} for q in _c_queries]}
        if question_contract == "steer" and _c_queries:
            async def _c_fetch(q: str) -> list:
                try:
                    _cv = await asyncio.to_thread(lambda _q=q: list(embedder.embed([_q])[0]))
                    return await source.search(RetrievalRequest(
                        query=q, tenant_id=tenant_id, workspace_id=workspace_id,
                        query_embedding=_cv, k=4, facets=dict(facets or {}),
                        exclude_facets=dict(exclude_facets or {})))
                except Exception as _ce:   # noqa: BLE001 — a dead leg never breaks the answer
                    _log.warning("contract leg failed on %r: %s", q, _ce)
                    return []
            _c_results = await _timed("contract_legs_ms", asyncio.gather(*(_c_fetch(q) for q in _c_queries)))
            for _cd, _cq, _c_hits in zip(_c_diag["legs"], _c_queries, _c_results):
                _cd["hits"] = len(_c_hits)
                if _c_hits:
                    _c_stash.append((_cq, _c_hits))   # planner never sees these — loop runs
                    #                                   byte-identical to contract-off
        if diag is not None:
            diag["question_contract"] = _c_diag
        if _contract is not None:
            _log.info("question contract (%s): mode=%s entities=%d axes=%d legs=%d",
                      question_contract, _contract.mode, len(_contract.entities),
                      len(_contract.axes), len(_c_queries))
            await emit({"type": "contract", "mode": question_contract,
                        "contract_mode": _contract.mode,
                        "entities": list(_contract.entities), "legs": list(_c_queries)})

    # ANSWER-CONTRACT resolution: the derived contract's `stance` selects ONE opaque profile whose
    # knobs re-tune retrieval+ranking+compose for THIS question. `answer_profiles` None / no stance /
    # unmatched stance → `_profile` None → every effective knob below is today's behavior (byte-identical).
    _profile = (answer_profiles or {}).get((_contract.stance or "")) if (answer_profiles and _contract) else None
    _ac = _profile is not None
    _eff_freshness = _profile.get("recency") if _ac else freshness   # profile recency OVERRIDES static
    _suppress_auth = bool(_ac and _profile.get("suppress_authority")) or bool(suppress_authority)
    _steer = (_profile.get("planner_steer") or "").strip() if _ac else ""
    _answer_dir = (_profile.get("answer_directive") or "").strip() if _ac else ""
    # WEB-ONLY thoroughness: no corpus fallback, so research every question deeply — multiple sources per
    # aspect, a separate search per sub-part/entity, and don't stop after a shallow first pass. This is the
    # "some answers are too short" fix (lookups/people/analytical were stopping at ~1-2 searches).
    _wo_steer = (
        "The only evidence source is live web search, so RESEARCH THOROUGHLY like a diligent analyst: for "
        "EACH distinct part of the question and EACH named subject, issue a SEPARATE targeted search, and "
        "gather MULTIPLE independent sources per aspect. Do NOT stop after a shallow first pass. "
        "For a SET / the top / the notable items, ALSO issue MAGNITUDE-oriented searches (the biggest / "
        "largest / most-reported / ranked) so the most prominent members surface, not just a semantic "
        "sample, AND look for RANKING / LIST pages that enumerate many members with their attributes at once. "
        "ENUMERATE FROM YOUR OWN KNOWLEDGE FIRST: for a TABLE / COMPARISON / 'all X' / 'the top X' ask, you "
        "ALREADY KNOW the notable members of the set — NAME them yourself (aim for the 15-25 most notable), "
        "then issue a SEPARATE targeted search PER NAMED MEMBER to GROUND its attributes. Do NOT wait for a "
        "single list page to name the members, and NEVER omit an obvious major member just because one page "
        "didn't mention it — a knowledgeable analyst names them from expertise, then VERIFIES each against "
        "the web. Build the roster from knowledge, ground each row, mark any attribute you cannot verify. A "
        "3-row table when you could name 20 members is the failure to avoid."
    ) if web_only else ""
    # REFLECTION intent steer (flag ROSTER_REFLECTION=steer): thread the inferred HEART-OF-INTENT into the
    # planner + compose so the answer lands on what the user REALLY wants — but ONLY when the derivation
    # was confident (high/medium). Low/empty confidence stays faithful to the literal question (the drift
    # guard: never invent a "deeper" intent when unsure). The intent/brief steer WHAT to look for and HOW
    # to shape the answer; they NEVER assert facts (grounding stays with the span-gate) and the literal
    # Question header is never replaced. "" for every field → byte-identical.
    _reflect_on = (reflection == "steer" and _contract is not None
                   and getattr(_contract, "intent_confidence", "") in ("high", "medium"))
    _reflect_intent = (getattr(_contract, "intent", "") or "").strip() if _reflect_on else ""
    _reflect_brief = (getattr(_contract, "answer_brief", "") or "").strip() if _reflect_on else ""
    _reflect_steer = ""
    if _reflect_intent or _reflect_brief:
        _reflect_steer = (
            (("The user's underlying intent: " + _reflect_intent) if _reflect_intent else "")
            + ((" A strong answer must deliver: " + _reflect_brief) if _reflect_brief else "")).strip()
    if _reflect_steer:
        _log.info("reflection intent (%s): %r | brief=%r", getattr(_contract, "intent_confidence", ""),
                  _reflect_intent[:120], _reflect_brief[:160])
        result.reflection = {"intent": _reflect_intent, "answer_brief": _reflect_brief,
                             "confidence": getattr(_contract, "intent_confidence", "")}
        if diag is not None:
            diag["reflection"] = dict(result.reflection)
    _web_recency_days = _profile.get("web_recency_days") if _ac else None
    _web_max_results = _profile.get("web_max_results") if _ac else None   # per-profile web breadth (current)
    _web_open = bool(_ac and _profile.get("web_open")) or web_only   # drop the trusted-domain whitelist
    # WEB-ONLY opens the web: no corpus fallback, so the agent MUST reach the aggregator/ranking/listicle
    # pages (a "top 30 startups by ARR" roundup lists many companies+metrics in ONE page) that the trusted
    # whitelist excludes. Quality is protected downstream by cross-engine prominence, evidence-tier grading,
    # and the span gate — so open-web reach adds coverage without letting SEO junk carry a cited claim.
    # entity-open (flag): a single-entity diligence question earns ONE additive open-web probe.
    # Eligibility is the LLM's subject_kind judgment (Rule 18), never a keyword match. subject_kind
    # is only emitted when ROSTER_WEB_ENTITY_OPEN is on (contract-prompt variant), so OFF → always False.
    _entity_open = bool(entity_open_web and _contract
                        and getattr(_contract, "subject_kind", "") == "specific_entity"
                        # when the whole web leg is already open (denoise), the separate entity_open
                        # probe is redundant — avoid the 2nd Exa call (base `web` leg already covers it)
                        and not web_open_denoise)
    _deep_company_entity = ""
    _contract_entities: list[str] = []
    if _contract is not None:
        for _e in getattr(_contract, "entities", ()) or ():
            _es = str(_e).strip()
            if _es:
                _contract_entities.append(_es)
        if _contract_entities:
            _deep_company_entity = _contract_entities[0]
    _deep_company = bool(deep_company and aux_source is not None and company_reader
                         and _contract
                         and getattr(_contract, "subject_kind", "") == "specific_entity"
                         and _deep_company_entity)
    _deep_person_entity = ""
    if _contract_entities:
        _deep_person_entity = _contract_entities[0]
    _subject_kind = getattr(_contract, "subject_kind", "") if _contract is not None else ""
    # ONLY fire the person reader on a genuinely NAMED individual (subject_kind=='person'). An earlier
    # over-eager fallback (subject_kind=='' AND exactly 1 entity) mis-fired it on ROLES/CATEGORIES/companies
    # — e.g. the follow-up "expand on the AI SRE startup founders" resolved to entity 'AI SRE startup
    # founders' with subject_kind='', fired the person leg on that generic phrase, and scattered the answer
    # across every AI SRE founder (bombing a clarifying follow-up). subject_kind=='person' only.
    _deep_person = bool(deep_person and aux_source is not None and person_reader
                        and _contract
                        and _subject_kind == "person"
                        and _deep_person_entity)
    # OBSERVABILITY (Rule 13): the deep-reader gates have several conjuncts (flag, aux_source,
    # manifest slot, contract derived, subject_kind, entity) — when a deep read unexpectedly does
    # NOT fire, this trace pinpoints WHICH conjunct failed without re-running the whole pipeline.
    # subject_kind is the usual culprit (e.g. the landscape-coverage prompt swap dropping the key).
    if deep_company or deep_person:
        _dr_trace = {"subject_kind": _subject_kind,
                     "contract": _contract is not None,
                     "aux_source": aux_source is not None,
                     "person": {"flag": bool(deep_person), "slot": bool(person_reader),
                                "entity": bool(_deep_person_entity), "fires": _deep_person},
                     "company": {"flag": bool(deep_company), "slot": bool(company_reader),
                                 "entity": bool(_deep_company_entity), "fires": _deep_company}}
        _log.info("deep-reader gate: %s", _dr_trace)
        if diag is not None:
            diag["deep_reader_gate"] = _dr_trace
    # ON-DEMAND WEB COVERAGE FAN-OUT (reflection pass): the "muted: didn't look" fix. A LANDSCAPE /
    # multi-entity / general question whose business dimensions the corpus does not hold (moat / ICP /
    # distribution for specific startups) must ACTIVELY web-search those dimensions + players instead of
    # reporting "the evidence is thin." Gate: reflection on AND web available AND a contract was derived
    # AND it's NOT a single-entity/person ask (those already web-read via the deep readers / entity_open)
    # AND there is something to search. Queries = axis-only (the missing business dimensions) + entity×axis
    # (per derived category/player), built from the LLM-derived contract (Rule 18: meaning lives in the
    # contract; this only shapes the fan-out). SHADOW logs what it WOULD fire; STEER fires it at step 0,
    # late-merged + screened through the SAME web-quality + span-gate path as the web:deep legs.
    _web_cov_queries: list[str] = []
    if reflection in ("shadow", "steer") and aux_source is not None and _contract is not None \
            and _subject_kind not in ("specific_entity", "person"):
        _web_cov_queries = build_coverage_queries(
            _contract_entities, list(getattr(_contract, "axes", ()) or []),
            topic=(question or "")[:90])   # anchor bare-axis legs to the question's subject (on-topic web search)
    _web_coverage = bool(reflection == "steer" and _web_cov_queries)
    if _web_cov_queries:
        _log.info("web-coverage (%s): fires=%s %d legs %r",
                  reflection, _web_coverage, len(_web_cov_queries), _web_cov_queries[:8])
        if diag is not None:
            diag["web_coverage"] = {"mode": reflection, "fired": _web_coverage,
                                    "queries": _web_cov_queries}
    # the tier-boost/recency ranker argument, honoring per-question authority suppression
    _ranker_arg = None if _suppress_auth else (evidence_ranker if evidence_fitness else None)
    if _ac:
        # profile-driven THOROUGHNESS: a landscape/current question needs more search steps (discover
        # the leaderboard → drill into each newest model) and a wider compose cap (a landscape answer
        # spans ~15 models, not a handful). Overrides only RAISE the caller's values, never lower them.
        max_steps = max(int(max_steps), int(_profile.get("max_steps") or 0))
        compose_claim_cap = max(int(compose_claim_cap), int(_profile.get("compose_claim_cap") or 0))
        _log.info("answer-contract stance=%s recency=%s suppress_authority=%s web_open=%s entity_open=%s denoise=%s steps=%s cap=%s",
                  _contract.stance, bool(_eff_freshness), _suppress_auth, _web_open, _entity_open, web_open_denoise, max_steps, compose_claim_cap)
    # WEB-ONLY: raise the step floor so non-current questions (which don't get the current profile's
    # max_steps=14) still research the web thoroughly instead of stopping after ~1-2 searches.
    if web_only:
        max_steps = max(int(max_steps), 12)
        compose_claim_cap = max(int(compose_claim_cap), 40)

    # ROSTER_PARAMETRIC_LED (T2): the DIRECTED VERIFY LOOP. When a `prior_draft` is present the model
    # has ALREADY drafted its answer's facts + structure; retrieval now VALIDATES each asserted FACT
    # instead of authoring the answer. For each drafted fact claim we run its targeted `verify_query`,
    # then try to ground the claim against the retrieved atoms through the ADVERSARIAL directed grounder
    # + the UNTOUCHED span-gate. A fact that grounds becomes a normal cited VerifiedClaim (identical
    # shape to today's); a fact that can't be grounded lands in `unverified_priors` (labeled register,
    # T3 renders it — never grounded prose). This REPLACES the agentic search loop below; both paths
    # converge on the SAME post-loop selection / congruence / authority / compose block. OFF
    # (prior_draft is None) → this block is skipped and the agentic loop runs byte-identical to today.
    if prior_draft is not None:
        from roster_kernel.research.prior_verify import ground_asserted_claim
        _fact_claims = [c for c in (getattr(prior_draft, "claims", None) or [])
                        if getattr(c, "kind", "fact") == "fact"][:_PARAMETRIC_FACT_CAP]
        _drafted = len(_fact_claims)
        _grounded_n = 0
        await emit({"type": "parametric_verify", "drafted_facts": _drafted})
        for _ci, _c in enumerate(_fact_claims):
            if budget.exhausted:
                # Fail-closed: no budget to verify the rest → they stay UNVERIFIED (never shipped
                # from the prior alone). Includes any needs_freshness claim, as required.
                for _rest in _fact_claims[_ci:]:
                    result.unverified_priors.append(
                        {"text": _rest.text, "needs_freshness": bool(getattr(_rest, "needs_freshness", False))})
                result.stopped_reason = "budget"
                break
            _q = (getattr(_c, "verify_query", "") or "").strip() or _c.text
            _qvec = await _timed("embed_ms",
                                 asyncio.to_thread(lambda q=_q: list(embedder.embed([q])[0])))
            # Reuse the EXACT base_req construction the agentic loop uses (facets/exclusions +
            # web_open / web_denoise / web_recency), so the verifier sees the same retrieval surface.
            _base_req = RetrievalRequest(
                query=_q, tenant_id=tenant_id, workspace_id=workspace_id,
                query_embedding=_qvec, k=k, facets=dict(facets or {}),
                exclude_facets=dict(exclude_facets or {}),
                web_open=(_web_open or web_open_denoise),
                web_denoise=web_open_denoise,
                web_recency_days=_web_recency_days,
                web_max_results=_web_max_results,
            )
            await emit({"type": "search", "query": _q, "variants": []})
            _legs = [("corpus", source.search(_base_req))]
            if aux_source is not None:
                _legs.append(("web", aux_source.search(_base_req)))
            _got = await _timed("retrieval_ms", asyncio.gather(
                *[co for _n, co in _legs], return_exceptions=True))
            _hits = []
            for (_leg, _co), _r in zip(_legs, _got):
                if isinstance(_r, Exception):
                    _log.warning("%s verify leg failed on %r: %s", _leg, _q, _r)
                    if diag is not None:
                        diag.setdefault("failures", []).append(
                            {"stage": f"{_leg}_verify", "detail": f"{type(_r).__name__}: {_r}"[:200]})
                    continue
                # Same open-web screen + liveness gate the agentic loop applies to the `web` leg.
                if _leg == "web" and web_open_denoise:
                    _screened = await screen_open_web_hits(
                        _r, question=question, llm=llm, prompt=web_quality_prompt, budget=budget,
                        emit_provenance=authority_basis)
                    _r = _authoritative_subset(_r) if _screened is None else _screened
                    _r = await drop_dead_urls(_r)
                _hits += _r
            _before = len(atoms.all())
            atoms.add_hits(_hits)
            # The atoms retrieved FOR THIS claim (newly-added AND any pre-existing block this query
            # re-surfaced) — the grounding candidate set for this specific fact.
            _hit_keys = {(h.document_id, h.block_id) for h in _hits}
            _claim_atoms = [a for a in atoms.all() if (a.document_id, a.block_id) in _hit_keys]
            await emit({"type": "found", "added": len(atoms.all()) - _before,
                        "total": len(atoms.all())})
            _res = await ground_asserted_claim(_c.text, _claim_atoms, llm, verifier, budget=budget)
            if _res is not None:
                _aid, _quote = _res
                _atom = atoms.get(_aid)
                if _atom is not None:
                    result.verified_claims.append(_mk_verified(_c.text, _aid, _quote, _atom))
                    _grounded_n += 1
                else:                                   # atom vanished (defensive) → unverified
                    result.unverified_priors.append(
                        {"text": _c.text, "needs_freshness": bool(getattr(_c, "needs_freshness", False))})
            else:
                # A needs_freshness fact that fails to ground MUST stay unverified (never shipped from
                # the prior alone) — automatic here since a fact becomes verified ONLY via retrieval.
                result.unverified_priors.append(
                    {"text": _c.text, "needs_freshness": bool(getattr(_c, "needs_freshness", False))})
        result.atoms_gathered = len(atoms.all())
        if result.stopped_reason != "budget":
            result.stopped_reason = "answered"
        await emit({"type": "verified", "verified": len(result.verified_claims),
                    "rejected": len(result.rejected_claims)})
        _log.info("parametric-led verify: drafted_facts=%d grounded=%d unverified=%d",
                  _drafted, _grounded_n, len(result.unverified_priors))
        if diag is not None:
            diag["parametric"] = {"drafted_facts": _drafted, "grounded": _grounded_n,
                                  "unverified": len(result.unverified_priors)}

    # ROSTER_INTELLIGENCE_CORE (T2): adversarial FOR/AGAINST pre-seed. When the model drafted competing
    # hypotheses, PRE-SEED the atom pool with bounded targeted retrievals that seek CONFIRMING (`for_query`)
    # AND DISCONFIRMING (`against_query`) evidence per hypothesis — the disconfirmation search the agentic
    # loop never runs on its own. These legs are ADDITIVE: their atoms just join the pool and the normal
    # loop below still runs in FULL (recall preserved — we do NOT set max_steps=0). Facts still enter ONLY
    # via claims_first + the span-gate + binding + authority, all unchanged. Budget: not charged per
    # retrieval (matching the parametric verify legs and the agentic loop — retrieval is cheap); the only
    # LLM call here is the open-web screen, which charges the request budget inside `screen_open_web_hits`.
    # OFF (`hypotheses is None`) → this whole block is skipped → the loop + everything else are byte-identical.
    if hypotheses is not None:
        _hyps = list(hypotheses)[:_INTELLIGENCE_HYP_CAP]
        _for_hits_n = 0
        _against_hits_n = 0
        # T-B RED-TEAM REFUTER: a genuinely cross-family judge (the same "cross-family" test the
        # grounding gate uses — `derive_judge_llm` present AND not the drafting `llm`) authors the
        # DISCONFIRMING (against) queries — a separate, uncorrelated mind, not the drafter grading its
        # own homework. FAIL-CLOSED: no cross-family judge / refuter error / empty → fall back to the
        # hypothesis's self-authored `against_query` (today's behavior). None → self-authored throughout.
        _rf_judge = (derive_judge_llm
                     if (derive_judge_llm is not None and derive_judge_llm is not llm) else None)
        _refuter_qs_n = 0                    # total red-team queries authored (diag / observability)
        _undertested: list[dict] = []        # hypotheses whose AGAINST search found ZERO hits
        await emit({"type": "intelligence_retrieval", "hypotheses": len(_hyps)})
        for _hi, _hyp in enumerate(_hyps):
            _claim = (getattr(_hyp, "claim", "") or "").strip()
            # FOR leg (UNCHANGED): self-authored `for_query`, falling back to the claim if blank.
            _for_q = (getattr(_hyp, "for_query", "") or "").strip() or _claim
            # AGAINST legs: the cross-family red-team authors the disconfirming queries when available;
            # otherwise the hypothesis's self-authored `against_query` (today's behavior). Fail-closed.
            _against_qs: list[str] = []
            if _rf_judge is not None and _claim and not budget.exhausted:
                _against_qs = await refute_hypothesis(_claim, _rf_judge, budget=budget, n=_REFUTER_N)
                _refuter_qs_n += len(_against_qs)
            if not _against_qs:              # no judge / refuter empty / error → self-authored fallback
                _self_against = (getattr(_hyp, "against_query", "") or "").strip()
                _against_qs = [_self_against] if _self_against else []
            _against_qs = _against_qs[:_REFUTER_AGAINST_CAP]   # bound against legs per hypothesis
            _hyp_legs = [("for", _for_q)] + [("against", _q) for _q in _against_qs]
            _hyp_against_hits = 0            # hits this hypothesis's disconfirming search surfaced
            for _stance, _hq in _hyp_legs:
                if not _hq:
                    continue                      # blank leg (e.g. no against_query) → skip, never crash
                if budget.exhausted:
                    break
                try:
                    _hqvec = await _timed("embed_ms",
                                          asyncio.to_thread(lambda q=_hq: list(embedder.embed([q])[0])))
                    # Small FIXED k=4 for these targeted legs (NOT the full loop k); no query variants,
                    # no open-web expansion beyond the normal aux web leg. Same base_req shape as the loop.
                    _hreq = RetrievalRequest(
                        query=_hq, tenant_id=tenant_id, workspace_id=workspace_id,
                        query_embedding=_hqvec, k=4, facets=dict(facets or {}),
                        exclude_facets=dict(exclude_facets or {}),
                        web_open=(_web_open or web_open_denoise),
                        web_denoise=web_open_denoise,
                        web_recency_days=_web_recency_days,
                        web_max_results=_web_max_results,
                    )
                    await emit({"type": "search", "query": _hq, "variants": [],
                                "hypothesis": _hi + 1, "stance": _stance})
                    _hlegs = [("corpus", source.search(_hreq))]
                    if aux_source is not None:
                        _hlegs.append(("web", aux_source.search(_hreq)))
                    _hgot = await _timed("retrieval_ms", asyncio.gather(
                        *[co for _n, co in _hlegs], return_exceptions=True))
                    _hhits = []
                    for (_hleg, _co), _hr in zip(_hlegs, _hgot):
                        if isinstance(_hr, Exception):
                            # Fail-safe (Rule 13): a dead leg is logged + visible, never blocks the answer.
                            _log.warning("intelligence %s/%s leg failed on %r: %s",
                                         _stance, _hleg, _hq, _hr)
                            if diag is not None:
                                diag.setdefault("failures", []).append(
                                    {"stage": f"intelligence_{_stance}_{_hleg}",
                                     "detail": f"{type(_hr).__name__}: {_hr}"[:200]})
                            continue
                        # Same open-web screen + liveness gate the agentic loop applies to the `web` leg.
                        if _hleg == "web" and web_open_denoise:
                            _hscreened = await screen_open_web_hits(
                                _hr, question=question, llm=llm, prompt=web_quality_prompt, budget=budget,
                                emit_provenance=authority_basis)
                            _hr = _authoritative_subset(_hr) if _hscreened is None else _hscreened
                            _hr = await drop_dead_urls(_hr)
                        _hhits += _hr
                    _hbefore = len(atoms.all())
                    atoms.add_hits(_hhits)          # ADDITIVE: the for/against atoms join the pool
                    if _stance == "for":
                        _for_hits_n += len(_hhits)
                    else:
                        _against_hits_n += len(_hhits)
                        _hyp_against_hits += len(_hhits)
                    await emit({"type": "found", "added": len(atoms.all()) - _hbefore,
                                "total": len(atoms.all())})
                except Exception as _he:
                    # Belt-and-suspenders fail-safe: a leg NEVER blocks the answer (Rule 13).
                    _log.warning("intelligence %s leg errored on %r: %s", _stance, _hq, _he)
                    if diag is not None:
                        diag.setdefault("failures", []).append(
                            {"stage": f"intelligence_{_stance}",
                             "detail": f"{type(_he).__name__}: {_he}"[:200]})
            # UNDER-TESTED (T-B): the disconfirming search for this hypothesis surfaced NO evidence —
            # disconfirmation was ATTEMPTED but nothing was found. That is NOT confirmation; the
            # hypothesis is only not-yet-refuted (treat with caution). Flag it for the compose + reader.
            if _hyp_against_hits == 0:
                _undertested.append({"index": _hi + 1, "claim": _claim})
        # Surface the under-tested hypotheses on the result so the compose block below can warn the
        # model (do NOT let it treat a hypothesis with zero disconfirming evidence as "confirmed") and
        # the runtime can render a caution line. Empty unless the against-search came up empty.
        result.intelligence_undertested = _undertested
        _log.info("intelligence pre-seed: hypotheses=%d for_hits=%d against_hits=%d "
                  "refuter_queries=%d undertested=%d",
                  len(_hyps), _for_hits_n, _against_hits_n, _refuter_qs_n, len(_undertested))
        if diag is not None:
            diag["intelligence"] = {"hypotheses": len(_hyps),
                                    "for_hits": _for_hits_n, "against_hits": _against_hits_n}
            diag["refuter"] = {"hypotheses": len(_hyps), "refuter_queries": _refuter_qs_n,
                               "undertested": [u["index"] for u in _undertested]}

    stale_searches = 0          # consecutive searches that added NO new atoms (spinning detector)
    premature_answers = 0       # zero-evidence answer attempts (see the guard below)
    for step_i in range(max_steps if prior_draft is None else 0):
        if budget.exhausted:
            result.stopped_reason = "budget"
            break
        try:
            budget.reserve()
        except BudgetExceeded:
            result.stopped_reason = "budget"
            break

        # Force an answer on the final step, OR early when the agent is spinning — two searches in
        # a row that surfaced NO new evidence means more searching won't help; answer over what we
        # have instead of burning the full step budget (latency fix for no-evidence questions).
        force = step_i == max_steps - 1 or (stale_searches >= 2 and bool(atoms.all()))
        await emit({"type": "step", "step": step_i + 1})
        step: AgentStep = await _ask(mode="force" if force else "step")

        # ZERO-EVIDENCE ANSWER GUARD (structural): compose is span-gated, so an answer with an
        # EMPTY atom pool can never ground — yet rich attachment context (e.g. a lab-report digest)
        # can convince the planner it already knows enough ("Analyze this report" → immediate
        # answer → 0 verified claims → 'No grounded answer'). Evidence is mandatory. First offense:
        # tell the planner and let IT craft the digest-informed queries (the LLM owns query
        # semantics); a repeat offense falls back to a structural search on the question.
        # Only when NO search has been attempted at all — an honest abstention AFTER a failed
        # search is legitimate and passes through.
        if step.action == "answer" and not atoms.all() and not searched_queries and not force:
            if not premature_answers:
                premature_answers += 1
                notes.append("You attempted to ANSWER without searching. Evidence is mandatory: "
                             "SEARCH first — form queries from the question AND the attachment "
                             "context's key findings (e.g. each abnormal result), then answer "
                             "citing what you find.")
                continue
            step = AgentStep(action="search", query=step.query or question)

        if step.action == "search":
            q = step.query or question
            searched_queries.append(q)                 # (A) remember what we searched, for the next planner step
            searched_queries.extend(step.queries or [])
            # (C) show the planner's focused query for THIS step; fall back to a reformulation (which varies)
            # rather than always echoing the original question, so the trace isn't misleadingly "duplicated".
            display_q = step.query or (step.queries[0] if step.queries else question)
            await emit({"type": "search", "query": display_q, "variants": list(step.queries or [])})
            qvec = await _timed("embed_ms", asyncio.to_thread(lambda: list(embedder.embed([q])[0])))  # off the loop
            base_req = RetrievalRequest(
                query=q, tenant_id=tenant_id, workspace_id=workspace_id,
                query_embedding=qvec, k=k, facets=dict(facets or {}),
                exclude_facets=dict(exclude_facets or {}),
                web_open=(_web_open or web_open_denoise),
                web_denoise=web_open_denoise,
                web_recency_days=_web_recency_days,   # web leg honors; corpus ignores
                web_max_results=_web_max_results,   # per-profile web breadth (current stance)
            )
            # Corpus: agent reformulations → multi-query fusion (recall); else a single search.
            # aux (web): ONE call per step on the ORIGINAL query (no per-variant fan-out) — runs
            # CONCURRENTLY with the corpus so it adds breadth without multiplying latency.
            corpus_co = (multi_query_retrieve(source, base_req, step.queries, embedder=embedder,
                                              source_cap_frac=retrieval_source_cap)
                         if step.queries else source.search(base_req))
            # SOURCE ROUTING (flag): the agent may name source TYPES to ALSO target for this query.
            # This is an ADDITIVE scoped leg ON TOP OF the flat corpus pass above — never a filter, so
            # a mis-route can't lose recall (the flat leg still fires). Off / no source_kinds → no leg.
            _routed_kinds = ([s.strip() for s in (step.source_kinds or [])
                              if isinstance(s, str) and s.strip()] if source_routing else [])
            routed_co = None
            if _routed_kinds:
                routed_req = replace(base_req,
                                     facets={**base_req.facets, "source_kind": tuple(_routed_kinds)})
                routed_co = (multi_query_retrieve(source, routed_req, step.queries, embedder=embedder,
                                                  source_cap_frac=retrieval_source_cap)
                             if step.queries else source.search(routed_req))

            # Intra-retrieval progress (additive): each leg announces the moment it lands —
            # {"type":"retrieving","source":<leg>,"hits":N} between 'search' and 'found' — so a
            # slow leg (e.g. a multi-minute web search) narrates instead of leaving a silent gap
            # the user reads as "stuck". A failing leg emits nothing here and propagates to the
            # gather below, exactly as before (Rule 13 logging unchanged).
            async def _traced_leg(leg: str, co):
                r = await co
                n = len(r[0]) if leg == "web:deep_person" and isinstance(r, tuple) else len(r)
                await emit({"type": "retrieving", "source": leg, "hits": n})
                return r

            # Assemble the retrieval legs: the flat corpus pass, the optional ADDITIVE routed leg
            # (source-kind scoped, flag-gated), and the optional web leg — run concurrently. Off path
            # (no routed leg, no web) is a single "corpus" leg, identical to before.
            _legs = [("corpus", corpus_co)]
            if routed_co is not None:
                _legs.append(("corpus:routed", routed_co))
            if aux_source is not None:
                _legs.append(("web", aux_source.search(base_req)))
                if _deep_company and step_i == 0:
                    _legs.append(("web:deep", retrieve_deep_company(
                        company=_deep_company_entity, templates=company_reader or {},
                        source=aux_source, tenant_id=tenant_id, workspace_id=workspace_id,
                        llm=planner, budget=budget)))
                if _deep_person and step_i == 0:
                    _legs.append(("web:deep_person", retrieve_deep_person(
                        person=_deep_person_entity, templates=person_reader or {},
                        source=aux_source, tenant_id=tenant_id, workspace_id=workspace_id,
                        llm=planner, budget=budget)))
                # ADDITIVE open-web probe (flag): first step only, single-entity questions. Drops the
                # trusted-domain whitelist (web_open=True) so the entity's OWN site/niche coverage is
                # reachable via Exa. The whitelisted `web` leg above is untouched (authority still leads).
                if _entity_open and step_i == 0:
                    _legs.append(("web:entity_open", aux_source.search(replace(base_req, web_open=True))))
                if _web_coverage and step_i == 0:
                    _legs.append(("web:coverage", retrieve_web_coverage(
                        queries=_web_cov_queries, source=aux_source, tenant_id=tenant_id,
                        workspace_id=workspace_id)))
            if len(_legs) > 1:
                got = await _timed("retrieval_ms", asyncio.gather(
                    *[_traced_leg(name, co) for name, co in _legs], return_exceptions=True))
                hits = []
                for (leg, _co), r in zip(_legs, got):
                    if isinstance(r, Exception):
                        # a dead leg must be VISIBLE (Rule 13) — the answer proceeds on the other
                        # legs, but the trace and diagnostics say the evidence base was degraded
                        _log.warning("%s search leg failed on %r: %s", leg, q, r)
                        if diag is not None:
                            diag.setdefault("failures", []).append(
                                {"stage": f"{leg}_search", "detail": f"{type(r).__name__}: {r}"[:200]})
                    else:
                        if leg == "web:deep_person":
                            r, _profiles = r
                            result.people_profiles = list(_profiles or [])
                        _screen_this = (
                            leg in ("web:entity_open", "web:deep", "web:deep_person", "web:coverage")
                            or (leg == "web" and web_open_denoise))
                        if _screen_this:
                            _raw_n = len(r)
                            screened = await screen_open_web_hits(
                                r, question=question, llm=llm, prompt=web_quality_prompt, budget=budget,
                                # ROSTER_AUTHORITY_BASIS: request a coarse provenance role per kept hit and
                                # stamp it as a generic `web_role` facet → a real tier via classify (breadth
                                # de-risk). OFF → no role requested/stamped → byte-identical.
                                emit_provenance=authority_basis)
                            # None = could-not-judge → fail safe to the authoritative subset;
                            # a list (even empty) = judged → respect it.
                            if screened is None:
                                r = _authoritative_subset(r)
                            elif leg in ("web:deep", "web:deep_person", "web:coverage"):
                                # The judge evaluates pages, while these legs deliberately keep several
                                # chunks per page. Keep every chunk whose page survived.
                                _kept_urls = {getattr(h, "document_id", "") for h in screened}
                                r = [h for h in r if getattr(h, "document_id", "") in _kept_urls]
                            else:
                                r = screened
                            # LIVENESS: an open-web page can 404 in the user's browser even though its
                            # quote was span-verified against the body Exa fetched. Drop citations whose
                            # URL is definitively dead (404/410) so we never surface a broken evidence
                            # link. Fail-open (bot-walls/timeouts kept). Structural (Rule 18).
                            _kept_n = len(r)
                            r = await drop_dead_urls(r)
                            await emit({"type": "retrieving", "source": f"{leg}:kept", "hits": len(r)})
                            if diag is not None:
                                _dkey = ("web_denoise" if leg == "web" else
                                         "web_deep" if leg == "web:deep" else
                                         "web_deep_person" if leg == "web:deep_person" else
                                         "web_coverage" if leg == "web:coverage" else
                                         "web_entity_open")
                                diag.setdefault(_dkey, []).append(
                                    {"step": step_i + 1, "raw": _raw_n, "kept": _kept_n,
                                     "live": len(r), "dead_dropped": _kept_n - len(r)})
                        hits += r
            else:
                hits = await _timed("retrieval_ms", _traced_leg("corpus", corpus_co))
            before = len(atoms.all())
            atoms.add_hits(hits)
            added = len(atoms.all()) - before
            # (B) count diminishing-returns searches: fewer than _LOW_YIELD_ATOMS NEW atoms is "stale"
            # (not just exactly zero), so a steady +1 grind trips the force-answer after two in a row.
            stale_searches = stale_searches + 1 if added < _LOW_YIELD_ATOMS else 0
            srcs = sorted({(h.source_key or "corpus") for h in hits})
            await emit({"type": "found", "added": added, "total": len(atoms.all()), "sources": srcs})
            if diag is not None:
                diag["trace"].append({"step": step_i + 1, "action": "search", "query": q,
                                      "variants": list(step.queries or []), "retrieved": added,
                                      "total_atoms": len(atoms.all()), "sources": srcs,
                                      "forced": force})

            # vertical gating: surface a real coverage gap so the agent reaches for
            # other sources or answers honestly instead of guessing.
            if gating is not None:
                gap = gating.coverage_gap(q, hits)
                if gap:
                    result.coverage_gaps.append(gap)
                    notes.append(f"COVERAGE GAP: {gap} — use another source or say so; do not guess.")
            continue

        # action == "answer": provenance hard gate (+ recovery re-ask if it abstained)
        await emit({"type": "verifying"})
        await _finalize_answer(step)
        await emit({"type": "verified", "verified": len(result.verified_claims),
                    "rejected": len(result.rejected_claims)})
        if diag is not None:
            diag["trace"].append({"step": step_i + 1, "action": "answer", "forced": force,
                                  "emitted": len(step.claims),
                                  "verified": len(result.verified_claims),
                                  "rejected": len(result.rejected_claims)})
        result.stopped_reason = "answered"
        break
    else:
        # Loop exhausted without an answer action. Force one final answer over the
        # evidence gathered (so the agent never silently returns nothing) — unless
        # the budget is spent. In parametric mode (prior_draft set) the loop ran zero
        # steps by design — the verify loop above already produced the answer state, so
        # this force-answer branch is a no-op (guard keeps stopped_reason from the verify loop).
        if prior_draft is None:
            result.stopped_reason = "max_steps"
            if not budget.exhausted:
                try:
                    budget.reserve()
                    final = await _ask(mode="force")
                    if final.action == "answer":
                        await _finalize_answer(final)
                        result.stopped_reason = "answered"
                except BudgetExceeded:
                    pass

    result.atoms_gathered = len(atoms.all())

    # SECOND-MODEL FALLBACK GROUNDER (factra pattern): the Anthropic agent gathered relevant atoms
    # but still emitted NO claims (0 verified AND 0 rejected) even after the forceful extract-recovery.
    # Re-asking the same model is unreliable here — hand the atoms to a second model (OpenAI) to
    # atomize into cited claims, then run them through the SAME verbatim span gate (_apply_answer).
    # Provenance is unchanged: only claims whose quote verifies survive. Fail-safe: no key / error /
    # nothing → 0 claims and the original abstention stands.
    # Skipped in parametric mode (prior_draft set): the directed verify loop VALIDATES the model's
    # drafted facts — it must never AUTHOR arbitrary claims from the retrieved atoms (that would break
    # the "retrieval validates, model leads" contract and launder a non-drafted fact into grounded
    # prose). OFF (prior_draft is None) → the guard is always true → byte-identical to today.
    if (prior_draft is None and not result.verified_claims and not result.rejected_claims
            and atoms.all() and not budget.exhausted):
        await emit({"type": "grounding"})
        try:
            from roster_kernel.research.fallback_grounder import ground_claimless
            # BudgetState honesty (stage-2 panel amendment): the grounder is ONE real LLM call when
            # a key is present (no key → it returns [] without calling → nothing to charge).
            # reserve() first so an exhausted budget skips the rescue (BudgetExceeded lands in this
            # block's except → the original abstention stands, exactly the existing degrade).
            if os.environ.get("OPENAI_API_KEY"):
                budget.reserve()
                budget.charge(calls=1)
            fb = await ground_claimless(
                question=question, atoms=[(a.atom_id, _atom_render(a)) for a in atoms.all()])
            if fb:
                result.retried_empty = True
                _apply_answer(AgentStep(action="answer", claims=[
                    ClaimOut(text=c["text"], atom_id=c["atom_id"], quote=c["quote"]) for c in fb]))
                await emit({"type": "verified", "verified": len(result.verified_claims),
                            "rejected": len(result.rejected_claims)})
        except Exception:   # noqa: BLE001 — fallback is best-effort; never break the answer
            pass

    # A9 LATE MERGE: stashed graph-leg evidence joins the atom pool ONLY NOW — after the planner
    # finished its own (graph-blind) searching and after the fallback grounder (which must never
    # ground an answer from adjacent-topic atoms alone). The claims-first extraction below then
    # mines planner AND graph atoms through the same span + entailment gates, and under the
    # first-come compose cap graph-derived claims can only FILL remaining slots, never displace
    # a planner claim. Strictly additive: retrieval breadth cannot regress.
    if _g_stash:
        _late_added = 0
        for _leg, _hits in _g_stash:
            _before = len(atoms.all())
            atoms.add_hits(_hits)
            _n = len(atoms.all()) - _before
            _late_added += _n
            if diag is not None:
                for _d in diag.get("graph_legs", {}).get("legs", []):
                    if _d["query"] == (_leg.get("query") or "").strip():
                        _d["merged"] = _n
        result.atoms_gathered = len(atoms.all())
        _log.info("graph legs late-merged %d atoms post-loop", _late_added)

    # EVIDENCE CONTRACT stage 3 late merge (steer): contract-leg evidence joins the atom pool at
    # the SAME seam as graph legs — after the planner finished its own (contract-blind) searching
    # and after the fallback grounder — so the loop ran byte-identical to contract-off and the
    # legs are purely additive. The claims-first extraction below mines them through the same
    # span + entailment gates as every other atom.
    if _c_stash:
        _c_added = 0
        for _cq, _c_hits in _c_stash:
            _before = len(atoms.all())
            atoms.add_hits(_c_hits)
            _n = len(atoms.all()) - _before
            _c_added += _n
            if diag is not None:
                for _cd in diag.get("question_contract", {}).get("legs", []):
                    if _cd["query"] == _cq:
                        _cd["merged"] = _n
        result.atoms_gathered = len(atoms.all())
        _log.info("contract legs late-merged %d atoms post-loop", _c_added)

    # EVIDENCE CONTRACT stage 2 (claim-congruence flag): loop-emitted and fallback-grounder claims
    # passed only the verbatim span gate — they have NEVER been entailment-judged (the bypass behind
    # the prod misattribution failure: a real quote from the wrong document's subject shipped as fact).
    # Route every such claim through the SAME batched binding judge the claims-first candidates use
    # — ONE extra batched entail_claims invocation, only when such claims exist — and enforce:
    # off-subject → DROP (hard), not-entailed → DROP, kind-mismatch → KEEP + annotate (demoted
    # below clean claims before ranking, see the partition further down). Fail-safe (Rule 18):
    # judge unavailable (no key) / errored / over budget → KEEP + annotate "unjudged" — never drop
    # on judge failure, never a keyword fallback.
    if claim_congruence and result.verified_claims:
        _pre = result.verified_claims
        _verdicts: list = [None] * len(_pre)
        if os.environ.get("OPENAI_API_KEY"):
            try:
                from roster_kernel.research.claims_first import _ENTAIL_CHUNK, entail_claims
                _n_bind = -(-len(_pre) // _ENTAIL_CHUNK)     # ceil — mirrors the judge's chunking
                budget.reserve(calls=_n_bind)                # BudgetExceeded → degrade to "unjudged"
                budget.charge(calls=_n_bind)
                _verdicts = await _timed("judge_ms", entail_claims(
                    claims=[{"text": vc.text, "atom_id": vc.atom_id, "quote": vc.quote}
                            for vc in _pre],
                    tags=[identity_tag(vc) for vc in _pre],
                    congruence=True, kinds=[vc.evidence_kind for vc in _pre]))
            except Exception as _be:   # noqa: BLE001 — incl. BudgetExceeded: annotate, never drop
                _log.warning("binding judge unavailable for loop/fallback claims: %r", _be)
                _verdicts = [None] * len(_pre)
        _kept: list[VerifiedClaim] = []
        for vc, v in zip(_pre, _verdicts):
            if v is None:                                    # judge didn't rule → keep, annotated
                vc.congruence_note = "unjudged"
                _congruence_count("unjudged")
                _kept.append(vc)
                continue
            _congruence_count("judged")
            if not v.get("on_subject", True):                # the misattribution fix: hard drop
                _congruence_count("off_subject")
                _log_off_subject("loop", vc.text, vc.document_title)
                continue
            if not v.get("entailed", False):                 # quote doesn't support the claim
                _congruence_count("not_entailed")
                continue
            if not v.get("kind_ok", True):                   # recall-safe: keep, demote + annotate
                vc.congruence_note = "kind_mismatch"
                _congruence_count("kind_mismatch")
            _kept.append(vc)
        result.verified_claims = _kept

    # CLAIMS-FIRST comprehensive extraction (flag): the terse loop cites only a few atoms, so most
    # retrieved evidence goes unused (e.g. 2 grounded from 18). Mine EVERY atom with a cheap batched
    # model, then ADD any claim that passes BOTH the unchanged verbatim span gate AND an independent
    # entailment gate. Only adds provenance-clean claims (never fabricates, never weakens the gate);
    # runs OFF the expensive loop model. Dedups against what the loop already grounded.
    # `prior_draft is None` gate: like the fallback grounder above, comprehensive extraction AUTHORS
    # claims from the atom pool — it must not run in parametric mode, where retrieval only VALIDATES
    # the model's drafted facts. OFF → the added conjunct is always true → byte-identical to today.
    if prior_draft is None and claims_first and atoms.all() and not budget.exhausted:
        await emit({"type": "extracting"})
        try:
            from roster_kernel.research.claims_first import (
                _ATOMS_PER_CALL, _ENTAIL_CHUNK, entail_claims, extract_claims,
            )
            from roster_kernel.research.provenance import normalize
            # BudgetState honesty (stage-2 panel amendment): the extraction batches are real LLM
            # calls — charge ceil(atoms / batch-size), but only when a key is present (no key →
            # claims_first makes zero calls). reserve() first: an exhausted budget raises
            # BudgetExceeded into this block's existing except → extraction skipped, the answer
            # proceeds on the loop's claims (the same degrade the loop uses — never crashes compose).
            _cf_atoms = [(a.atom_id, _atom_render(a)) for a in atoms.all()]
            _has_judge = bool(os.environ.get("OPENAI_API_KEY"))
            if _has_judge:
                _n_extract = -(-len(_cf_atoms) // _ATOMS_PER_CALL)     # ceil
                budget.reserve(calls=_n_extract)
                budget.charge(calls=_n_extract)
            cands = await extract_claims(
                question=question, atoms=_cf_atoms,
                lenses=list(extraction_lenses), atom_cap=atom_cap,
                evidence_identity=evidence_identity)
            span_ok = []                                   # candidates whose quote verbatim-verifies
            for c in cands:
                atom = atoms.get(c["atom_id"])
                if atom is not None and atom.locator is not None \
                        and verifier.verify(c["quote"], atom.locator):
                    span_ok.append((c, atom))
            if span_ok and _has_judge:                     # charge the entail/binding batches too
                _n_entail = -(-len(span_ok) // _ENTAIL_CHUNK)          # ceil
                budget.reserve(calls=_n_entail)
                budget.charge(calls=_n_entail)
            if claim_congruence:
                # Stage 2: the SAME judge call becomes the BINDING judge — each item carries its
                # ⟨title — source⟩ tag + structural evidence kind (SOURCE is required for the
                # on_subject judgment, so tags are passed regardless of the stage-1 flag).
                verdicts = await entail_claims(
                    claims=[c for c, _ in span_ok],
                    tags=[identity_tag(atom) for _, atom in span_ok],
                    congruence=True,
                    kinds=[_classify_atom(atom) for _, atom in span_ok]) if span_ok else []
            else:
                verdicts = await entail_claims(
                    claims=[c for c, _ in span_ok],
                    tags=([identity_tag(atom) for _, atom in span_ok]
                          if evidence_identity else None)) if span_ok else []
            seen = {(vc.atom_id, normalize(vc.quote)) for vc in result.verified_claims}
            added = 0
            for (c, atom), ok in zip(span_ok, verdicts):
                note = ""
                if claim_congruence:
                    # Binding enforcement (extractor candidates fail CLOSED, as today): no verdict
                    # (judge error) → drop, exactly like today's errored-chunk False; off-subject →
                    # drop (+ trace); entailed=False → drop (as today); kind-mismatch → keep +
                    # annotate (demoted below clean claims before ranking).
                    if ok is None:
                        _congruence_count("unjudged")      # dropped (fail closed), counted for diag
                        continue
                    _congruence_count("judged")
                    if not ok.get("on_subject", True):
                        _congruence_count("off_subject")
                        _log_off_subject("claims_first", c["text"], atom.document_title)
                        continue
                    if not ok.get("entailed", False):
                        _congruence_count("not_entailed")
                        continue
                    if not ok.get("kind_ok", True):
                        note = "kind_mismatch"
                        _congruence_count("kind_mismatch")
                elif not ok:                               # entailment gate (support, not just quote)
                    continue
                key = (c["atom_id"], normalize(c["quote"]))
                if key in seen:                            # dedup vs existing + each other
                    continue
                seen.add(key)
                vc = _mk_verified(c["text"], c["atom_id"], c["quote"], atom)
                vc.congruence_note = note
                result.verified_claims.append(vc)
                added += 1
                # OFF: cap first-come at the compose limit (unchanged). ON: collect a bigger pool so
                # the relevance ranking below has real choices before it trims to the compose cap.
                if len(result.verified_claims) >= (extract_collect if evidence_select else compose_claim_cap):
                    break
            await emit({"type": "extracted", "added": added, "candidates": len(cands),
                        "total": len(result.verified_claims)})
            if diag is not None:
                diag["extraction"] = {"candidates": len(cands), "added": added}
        except Exception as _ex:   # noqa: BLE001 — extraction is best-effort; never break the answer
            if diag is not None:
                diag["failures"].append({"stage": "extraction", "detail": repr(_ex)[:200]})

    # Stage-2 demotion: kind-mismatch claims are KEPT (recall-safe per the panel ruling) but pushed
    # to the BACK of the ordering BEFORE any ranking/cap, so under the first-come compose cap they
    # can only fill remaining slots, never displace a congruent claim. Stable partition (sort on a
    # bool key) — relative order within each group is preserved. "unjudged" does NOT demote: judge
    # failure must never penalize a claim.
    if claim_congruence and result.verified_claims:
        result.verified_claims.sort(key=lambda vc: vc.congruence_note == "kind_mismatch")

    # AUTHORITY BASIS (flag): push low-basis claims (tier rank<=1 — unattributed blog / social) to the
    # BACK so authoritative tiers fill the compose cap first. Stable sort preserves cosine/first-come
    # order WITHIN each bucket (rank>=2 front, rank<=1 back). Reorder ONLY — NEVER drops (breadth
    # preserved: low-tier still fills any remaining seats + the market-signal register). UNCONDITIONAL:
    # runs regardless of pool-vs-cap (mirrors the kind_mismatch demote above), unlike the boost ranker
    # which only fires when pool>cap AND evidence_fitness is on. Uses the RAW `evidence_ranker` param
    # (NOT `_ranker_arg`) so it does NOT depend on evidence_fitness. OFF / suppress / no ranker → skipped
    # → byte-identical claim order.
    if authority_basis and not _suppress_auth and evidence_ranker is not None and result.verified_claims:
        def _basis_tier(vc) -> int:
            try:
                return int(evidence_ranker(getattr(vc, "evidence_kind", "") or "") or 0)
            except Exception:   # noqa: BLE001 — a bad/absent ranker never breaks the answer
                return 0
        result.verified_claims.sort(key=lambda vc: 0 if _basis_tier(vc) >= 2 else 1)  # stable

    # EVIDENCE CONTRACT stage 3 — SLOT-AWARE compose selection (steer, enumerative; panel
    # amendment A1, the loophole fix): a self-congruent OFF-SLOT claim (true facts about the
    # wrong entity, honestly attributed) must never EVICT a slot-filling claim from the compose
    # cap. Selection into the cap: rank the pool exactly as the existing flags would (relevance
    # ranking when a ranking flag is on, first-come otherwise), then reserve seats for
    # slot-filling claims ROUND-ROBIN across covered entities (every covered entity gets
    # representation before any gets a second seat), then fill the remaining seats with the
    # existing ranking over the leftovers. Membership-only: the final list keeps the base
    # ordering, so relative order matches what the existing path would show compose. Entity↔claim
    # matching is structural containment against the contract's OWN closed entity list (Rule 18:
    # computable set membership, not semantic judgment). OFF / shadow / exploratory → this block
    # never runs and selection is byte-identical.
    _c_enum = (question_contract == "steer" and _contract is not None
               and _contract.mode == "enumerative" and bool(_contract.entities))
    if _c_enum and len(result.verified_claims) > compose_claim_cap:
        from roster_kernel.research.contract import match_entities
        if evidence_select or evidence_fitness or country_boost or _eff_freshness or _suppress_auth:
            base = await _rank_claims_by_relevance(
                question, result.verified_claims, embedder, len(result.verified_claims),
                evidence_ranker=_ranker_arg,
                country_boost=country_boost, rank_all=True, freshness=_eff_freshness)
        else:
            base = list(result.verified_claims)     # first-come — today's default ordering
        _queues: dict[str, list] = {}               # entity → its claims, best-ranked first
        for vc in base:
            for _e in match_entities(list(_contract.entities), vc.text, vc.document_title):
                _queues.setdefault(_e, []).append(vc)
        _picked: set[int] = set()                   # id()-keyed (VerifiedClaim is unhashable)
        _idx = {e: 0 for e in _queues}
        _active = [e for e in _contract.entities if e in _queues]
        while _active and len(_picked) < compose_claim_cap:
            for _e in list(_active):                # one seat per still-active entity per pass
                _q = _queues[_e]
                _i = _idx[_e]
                while _i < len(_q) and id(_q[_i]) in _picked:
                    _i += 1                         # already seated via another entity's slot
                if _i >= len(_q):
                    _idx[_e] = _i
                    _active.remove(_e)
                    continue
                _picked.add(id(_q[_i]))
                _idx[_e] = _i + 1
                if len(_picked) >= compose_claim_cap:
                    break
        for vc in base:                             # leftover seats: existing ranking order
            if len(_picked) >= compose_claim_cap:
                break
            if id(vc) not in _picked:
                _picked.add(id(vc))
        result.verified_claims = [vc for vc in base if id(vc) in _picked]
        await emit({"type": "selecting", "from": len(base), "to": len(result.verified_claims)})

    # Evidence SELECTION (flags): compose is capped for cost/scannability, so WHICH verified findings
    # survive the cap matters. Default = first-come. Under evidence-select, keep the findings most
    # RELEVANT to the question; under evidence-fitness, additionally boost stronger evidence TIERS into
    # the cap (span+entailment already passed → provenance unchanged; this only reorders/trims already-
    # verified claims). Either flag triggers the ranking pass.
    if (evidence_select or evidence_fitness or country_boost or _eff_freshness or _suppress_auth) and len(result.verified_claims) > compose_claim_cap:
        await emit({"type": "selecting", "from": len(result.verified_claims), "to": compose_claim_cap})
        result.verified_claims = await _rank_claims_by_relevance(
            question, result.verified_claims, embedder, compose_claim_cap,
            evidence_ranker=_ranker_arg,
            country_boost=country_boost, freshness=_eff_freshness)

    # EVIDENCE CONTRACT stage 3 — the SLOT GRID (observability, both modes) + honest coverage
    # gaps (steer only). The grid counts, per contract entity, the FINAL selected claims that
    # fill its slot — logged to the diag trace so a confident-wrong contract or an empty slot is
    # debuggable without a rerun (Rule 13). In STEER mode an entity left with ZERO matching
    # claims becomes a coverage gap PRODUCED BY THE LOOP (where it is actionable), not by compose
    # (where it is a footnote). Shadow logs the grid and changes nothing else.
    _enum_compose = False        # Evidence Contract stage 4: enumerative-compose routing decision
    if _contract is not None and _contract.mode == "enumerative" and _contract.entities:
        from roster_kernel.research.contract import match_entities
        _grid = {e: 0 for e in _contract.entities}
        for vc in result.verified_claims:
            for _e in match_entities(list(_contract.entities), vc.text, vc.document_title):
                _grid[_e] += 1
        if diag is not None and "question_contract" in diag:
            diag["question_contract"]["slot_grid"] = _grid
        if question_contract == "steer":
            _axes_note = ", ".join(_contract.axes)
            for _e, _n in _grid.items():
                if _n == 0:
                    result.coverage_gaps.append(
                        f"No evidence retrieved for {_e}"
                        + (f" ({_axes_note})" if _axes_note else ""))
        # EVIDENCE CONTRACT stage 4 — ANSWER-MODE ROUTING (flag; panel A3: the mode is re-derived
        # from BOUND CLAIMS at compose time, never trusted from the pre-retrieval contract alone).
        # Enumerative compose fires only when the flag is on, the vertical supplied an addendum,
        # the derived contract says enumerative, AND ≥2 contract entities hold ≥1 slot-matched
        # claim in the FINAL verified selection (the grid above — structural containment, Rule 18).
        # A single-entity (or zero-coverage) answer keeps today's framing: there is nothing to
        # enumerate. OFF / not fired → the compose directive below is byte-identical.
        if answer_mode_routing and (enumerative_compose_addendum or "").strip():
            _covered = sum(1 for _n in _grid.values() if _n > 0)
            _enum_compose = _covered >= 2
            if diag is not None and "question_contract" in diag:
                diag["question_contract"]["answer_mode"] = {
                    "routed": _enum_compose, "covered_entities": _covered}

    # Compose a synthesized answer FROM the verified findings only (factra "living
    # answer" model). Grounded by construction: the composer sees only the verified
    # findings and must reference them [n]; it may not add outside facts. A vertical
    # may supply an optional `answer_format` directive (domain-owned) that shapes the
    # structure — the kernel stays domain-free and only threads the string through.
    if result.verified_claims:          # compose is the DELIVERABLE — always attempt it when we have
        await emit({"type": "composing", "findings": len(result.verified_claims)})  # findings (not
        n_findings = len(result.verified_claims)

        def _finding_source(vc) -> str:
            """Compose's source field — the document-identity tag appended alongside source_key
            under the evidence-identity flag. OFF (or no title) → source_key, byte-identical."""
            if not evidence_identity:
                return vc.source_key
            tag = identity_tag(vc)
            return f"{vc.source_key} {tag}" if tag else vc.source_key

        def _finding_note(vc) -> str:
            """Stage-2 annotation (claim-congruence flag): a non-empty congruence_note renders as a
            short bracketed marker — generic wording only ("kind-mismatch"/"unjudged", kernel
            litmus) — so compose can weigh the demoted/unjudged finding honestly. OFF → "" and the
            findings line is byte-identical to stage 1."""
            if not claim_congruence:
                return ""
            note = getattr(vc, "congruence_note", "")
            return f" [{note.replace('_', '-')}]" if note else ""

        def _finding_year(vc) -> str:
            """Freshness disclosure (flag): surface each finding's publication YEAR so compose can SEE
            evidence age + flag staleness. OFF (freshness None) → "" and the line is byte-identical."""
            if not _eff_freshness:
                return ""
            yr = str((getattr(vc, "facets", None) or {}).get("year") or "")[:4]
            return f" [{yr}]" if yr.isdigit() else ""

        findings = "\n".join(
            f"[{i}] {vc.text}  (quote: \"{vc.quote}\" — source: {_finding_source(vc)})"
            f"{_finding_year(vc)}{_finding_note(vc)}"
            for i, vc in enumerate(result.verified_claims, 1))

        # DEEP SYNTHESIS (flag) — DERIVE-WEAVE. On a non-lookup deep run, run the grounded reasoning
        # gate (derive: propose → validity-judge → gate) over the SAME verified findings compose sees,
        # then weave its survivors into the compose prompt as the analytical spine (a distinct block
        # right after the findings). Each survivor is already gated (label + basis + falsifier) and adds
        # NO fact — it only reasons over findings. Fail-OPEN: any derive error → no block, and compose
        # is byte-identical to the non-deep path. OFF / lookup → `_derivations` stays empty and the
        # `_deriv_block` injected below is "" (byte-identical compose prompt).
        _derivations: list = []
        _deriv_block = ""
        if deep_synthesis and kind != "lookup" and result.verified_claims:
            try:
                from roster_kernel.research.reason import derive as _derive_fn
                _derivations = await _derive_fn(
                    question, result.verified_claims, llm,
                    generate_ideas=derive_ideas, judge_llm=derive_judge_llm,
                    max_out=(4 if kind == "understanding" else 6))
            except Exception as _e:   # noqa: BLE001 — a derive failure must never break compose
                _log.warning("deep-synthesis derive-weave failed: %r", _e)
                _derivations = []
            # BUDGET: derive makes 2 LLM calls (propose + validity-judge) but surfaces no token usage,
            # so charge a conservative flat estimate (charge-after, gated like compose/frame-repair).
            # NOTE: exact per-call token metering for derive is deferred (derive returns no usage).
            if not budget.exhausted:
                budget.charge(calls=2, tokens=3000)
            if _derivations:
                _dl = []
                for _i, _d in enumerate(_derivations, 1):
                    _basis = ", ".join(str(b) for b in (getattr(_d, "basis", ()) or ()))
                    _dl.append(
                        f"[D{_i}] {_d.label}: {_d.conclusion} "
                        f"(from findings {_basis}; falsifier: {_d.falsifier})")
                _deriv_block = (
                    "\n\nGROUNDED DERIVATIONS (already validated — weave the best into the answer as its "
                    "analytical spine; keep each label + falsifier; cite [Dn]):\n" + "\n".join(_dl))
                result.derivations = _derivations   # surface for the UI (parity with the derive flag)

        # FRESHNESS disclosure metadata (flag): compute the as-of year, the newest/oldest cited year,
        # and a stale_warning when the freshest evidence predates the vertical's recency horizon — so
        # the UI + the answer can say "as of <year>" instead of presenting dated facts as current.
        if _eff_freshness:
            import datetime as _dt
            _now = _dt.date.today().year
            _yrs = []
            for vc in result.verified_claims:
                _y = str((getattr(vc, "facets", None) or {}).get("year") or "")[:4]
                if _y.isdigit() and int(_y) <= _now:   # ignore bogus future-dated records
                    _yrs.append(int(_y))
            _hz = max(1, int(_eff_freshness.get("horizon_years", _RECENCY_HORIZON_YEARS)))
            result.freshness = {
                "as_of": _now,
                "newest_year": max(_yrs) if _yrs else None,
                "oldest_year": min(_yrs) if _yrs else None,
                "n_dated": len(_yrs), "n_total": len(result.verified_claims),
                "stale_warning": bool(_yrs) and (max(_yrs) < _now - _hz),
            }

        # EVIDENCE CONTRACT stage 4 — when the enumerative-compose decision fired, APPEND the
        # vertical's addendum (an opaque caller-supplied string — kernel litmus) to the existing
        # directive. The base directive is UNTOUCHED (it is the protected baseline); not fired →
        # `_compose_directive is answer_format` and every compose prompt is byte-identical.
        # DEEP SYNTHESIS (flag): on a non-lookup deep run, REPLACE the compose base with the vertical's
        # deep format; the existing addenda (enum / axis / tech) still apply ON TOP as today. OFF or a
        # lookup → `_base_directive is answer_format`, so every compose prompt below is byte-identical.
        _base_directive = answer_format
        if deep_synthesis and kind != "lookup" and deep_answer_format:
            _base_directive = deep_answer_format
        # CONTRACT-RENDERED COMPOSE (ROSTER_CONTRACT_COMPOSE, voice ⟂ shape): the directive IS the derived
        # contract — the universal VOICE plus the SHAPE for the contract's mode — REPLACING the flat
        # golden/answer_format base. Shape follows the QUESTION, not a deployment flag. This is the single
        # chokepoint every path composes through (follow-ups + reasoned both route here), so an enumerative
        # ask ("build me a table of all X") renders a table instead of being flattened to a thesis. The
        # kernel selects the shape by mode (structural) and, for enumerative, appends the contract's
        # concrete items+dimensions — it never parses the opaque vertical prose. OFF → byte-identical.
        if contract_compose and (contract_compose_voice or "").strip():
            from roster_kernel.research.contract import render_contract_directive
            _base_directive = render_contract_directive(
                voice=contract_compose_voice, shapes=contract_compose_shapes,
                default=contract_compose_default,
                mode=(_contract.mode if _contract is not None else ""),
                entities=(_contract.entities if _contract is not None else None),
                axes=(_contract.axes if _contract is not None else None))
        _compose_directive = _base_directive
        if _enum_compose:
            _ad = (enumerative_compose_addendum or "").strip()
            _compose_directive = (_base_directive + "\n\n" + _ad) if _base_directive else _ad
        # ANSWER-AXES (flag): make the answer ADDRESS EVERY aspect the reader asked about (the derived
        # contract axes) + lead with a synthesized take — so a requested aspect (e.g. 'moat') is never
        # silently dropped and the answer synthesizes rather than surveys. No axes → a no-op.
        if axis_complete and isinstance(result.question_contract, dict):
            _axes = [str(a).strip() for a in (result.question_contract.get("axes") or []) if str(a).strip()]
            if _axes:
                _axd = _AXIS_COMPLETE_ADDENDUM.replace("<AXES>", ", ".join(_axes[:10]))
                _compose_directive = (_compose_directive + "\n\n" + _axd) if _compose_directive else _axd
        # TECH SYNTHESIS (flag): add a strategic 'how it works' technical synthesis from the evidence
        # (disclosed → cited; likely-design → labeled [[R]] inference). The addendum self-skips for a
        # subject with no technical product, so the append is unconditional when the flag is on.
        if tech_synthesis:
            _compose_directive = (
                (_compose_directive + "\n\n" + _TECH_SYNTHESIS_ADDENDUM)
                if _compose_directive else _TECH_SYNTHESIS_ADDENDUM
            )
        # AUTHORITY BASIS (flag): APPEND the vertical's floor directive — ground facts in the highest-tier
        # source; opinion/blog/social/unknown are supplementary signal, never the sole basis for a stated
        # fact. Same gate as the T1 partition (`authority_basis and not _suppress_auth`, and a directive
        # was supplied). OFF / suppress / no directive → not appended → compose prompt byte-identical.
        if authority_basis and not _suppress_auth and authority_basis_directive:
            _abd = authority_basis_directive.strip()
            if _abd:
                _compose_directive = (_compose_directive + "\n\n" + _abd) if _compose_directive else _abd
        # PARAMETRIC-LED (ROSTER_PARAMETRIC_LED, T3): when the model drafted the answer from its integrated
        # knowledge, FOLLOW its OUTLINE for structure/reasoning while keeping every FACT grounded in the
        # verified findings (cite [n]) — no unverifiable claim restated as established fact. Appended ONLY
        # when a prior_draft drove this run → OFF (prior_draft is None) → not appended → byte-identical.
        if prior_draft is not None:
            _outline = (getattr(prior_draft, "outline", "") or "").strip() or "(none)"
            # The model's REASONING claims are the analytical spine of a parametric answer — inject them so
            # compose builds around them (as labeled [[R]] inference), not just the outline. Without this an
            # understanding/reasoning-heavy question (few verifiable facts) composed a thin answer.
            _reasoning = "\n".join(
                f"- {(getattr(c, 'text', '') or '').strip()}"
                for c in (getattr(prior_draft, "claims", None) or [])
                if getattr(c, "kind", "fact") == "reasoning" and (getattr(c, "text", "") or "").strip())
            _pad = _PARAMETRIC_ADDENDUM.replace("<OUTLINE>", _outline).replace("<REASONING>", _reasoning or "(none)")
            _compose_directive = (_compose_directive + "\n\n" + _pad) if _compose_directive else _pad
        # INTELLIGENCE-CORE (ROSTER_INTELLIGENCE_CORE, T3): when the model drafted competing HYPOTHESES +
        # an analytical FRAME, structure the answer around WEIGHING them against the evidence — as a frame
        # the evidence TESTS, never as facts (facts stay retrieval-authored + [n]-cited; the synthesis is
        # labeled [[R]] and, on a deep run, flows through the derive-weave already woven above). The
        # hypotheses' falsifiers become the CRUX register (`intelligence_cruxes`, rendered post-compose as
        # "what would change this read"). Appended ONLY when `hypotheses is not None` → OFF → not appended
        # → every compose prompt byte-identical, and `intelligence_cruxes` stays empty.
        if hypotheses is not None:
            _hyps_c = list(hypotheses)[:_INTELLIGENCE_HYP_CAP]
            _hyp_lines = []
            for _hi, _h in enumerate(_hyps_c, 1):
                _claim = (getattr(_h, "claim", "") or "").strip()
                _fals = (getattr(_h, "falsifier", "") or "").strip()
                _hyp_lines.append(
                    f"H{_hi}: {_claim}" + (f" — falsifier: {_fals}" if _fals else ""))
            _hyp_text = "\n".join(_hyp_lines)
            _iad = (_INTELLIGENCE_ADDENDUM
                    .replace("<FRAME>", (intelligence_frame or "").strip() or "(none)")
                    .replace("<HYPOTHESES>", _hyp_text or "(none)"))
            # UNDER-TESTED note (T-B): warn the model about any hypothesis whose disconfirming search
            # found NOTHING — it is NOT confirmed, only not-yet-refuted. Appended ONLY when at least one
            # hypothesis is under-tested (populated during the adversarial retrieval block above); when
            # none are under-tested the addendum is byte-identical to today's intelligence addendum.
            _ut = getattr(result, "intelligence_undertested", None) or []
            if _ut:
                _ut_lines = "\n".join(
                    f"- H{u['index']}: {(u.get('claim') or '').strip()}" for u in _ut)
                _iad = _iad + (
                    "\n\nUNDER-TESTED — the red-team disconfirming search found NO evidence AGAINST "
                    "these hypotheses. That is NOT confirmation; it means disconfirmation was attempted "
                    "and nothing turned up yet. Do NOT treat them as established — flag each as "
                    "not-yet-disconfirmed and treat with explicit caution:\n" + _ut_lines)
            _compose_directive = (_compose_directive + "\n\n" + _iad) if _compose_directive else _iad
            # CRUX register: the falsifiers are the concrete observables that would flip the preferred
            # read (the model's text, surfaced post-compose as "what would change this read", NOT a fact).
            result.intelligence_cruxes = [
                (getattr(_h, "falsifier", "") or "").strip()
                for _h in _hyps_c if (getattr(_h, "falsifier", "") or "").strip()]

        async def _compose(directive: str | None) -> ComposedAnswer:
            # Base ANSWER instruction kept identical to the original (directive-free path stays a
            # near-exact no-op). A trailing META judgment (directly_addresses/gap_note) is appended
            # AFTER it — it asks only for extra metadata, not a different answer, so answer text is
            # unaffected. The vertical directive, when present, is appended AFTER that.
            compose_user = (
                f"Question: {question}\n\nVERIFIED FINDINGS (the ONLY facts you may use):\n"
                f"{findings}"
                # DEEP SYNTHESIS derive-weave: the grounded-derivations block (empty "" when OFF /
                # lookup / none survived → compose prompt byte-identical to today).
                + _deriv_block
                + "\n\n"
                "Write a clear, well-organized answer to the question that synthesizes "
                "these findings into coherent prose. Reference each finding inline as "
                "[n] where you use it. Use ONLY the findings above — do not add facts, "
                "figures, or claims not present in them. If they only partially answer "
                "the question, say what is and isn't supported."
                # ANSWER-FOCUS (flag): ANSWER the specific question and scope to its subject, instead of
                # compiling every retrieved finding. Fixes elliptical follow-ups ("what dose" → dumping
                # every drug's dose) AND single-turn "compile everything". Grounding is unchanged — it
                # still uses ONLY the verified findings and still cites [n].
                + (" Directly ANSWER the specific question asked. If some findings concern a DIFFERENT "
                   "subject, drug, population, or topic than the question, use ONLY the findings about "
                   "the asked subject and ignore the rest — do not enumerate unrelated findings. If the "
                   "findings do not contain the specific answer the question asks for, say so explicitly "
                   "as a gap; do NOT substitute a list of unrelated findings."
                   # EXISTENCE/APPROVAL questions (narrow): when the question asks whether a SPECIFIC
                   # thing EXISTS / is APPROVED / is ESTABLISHED (a named product, a fixed-dose
                   # combination, an approved therapy) and the findings describe only ADJACENT or
                   # COMPONENT evidence — the individual drugs, related research — but NOT that specific
                   # thing, do NOT present the adjacent evidence as if it answered: state plainly that
                   # the evidence does not establish the specific thing asked about, and set
                   # directly_addresses=false. (This targets 'is there an approved X for Y' / 'what is
                   # the dose of the combined X+Y pill' — it does NOT apply when the findings do address
                   # the asked entity.)
                   " If the question asks whether a SPECIFIC product/combination/approved therapy EXISTS "
                   "and the findings show only its components or adjacent research rather than that "
                   "specific thing, state plainly that the evidence does not establish it — do not "
                   "describe the adjacent research as though it were the answer." if answer_focus else "")
                + "\n\nSEPARATELY (metadata, not part of the answer prose): set directly_addresses=false "
                "if the findings only address the question by analogy/adjacent topic rather than "
                "DIRECTLY (e.g. no evidence on the exact intervention/population/outcome asked); then "
                "put ONE short line in gap_note naming the direct evidence that is missing. Otherwise "
                "directly_addresses=true and gap_note empty."
                # FRESHNESS disclosure (flag): each finding is tagged with its publication [YEAR]. When
                # the question asks for the CURRENT / LATEST / FRONTIER state or where things are HEADED,
                # you MUST state the year of the most recent evidence you are relying on, and if that
                # newest evidence predates this year, say plainly the picture is "as of <year>" and may
                # be dated — never present older evidence as the current state of the art. Base this
                # ONLY on the [YEAR] tags shown; do not invent newer facts.
                + (f"\n\nFRESHNESS: today is {_now}. The findings are tagged with a [year]. If the "
                   "question asks for the current/latest/frontier state or trajectory, state the year of "
                   "your most recent evidence and, if it predates this year, explicitly note the answer is "
                   "'as of' that year and may be dated — do NOT present older evidence as current."
                   if _eff_freshness else "")
                # ANSWER-CONTRACT: the resolved stance profile's framing directive (e.g. "current" →
                # lead with the newest, labeled announced/unbenchmarked; "established" → prioritize
                # benchmarked/peer-reviewed evidence). "" when no profile → byte-identical.
                + (("\n\n" + _answer_dir) if _answer_dir else "")
                # REFLECTION intent (flag ROSTER_REFLECTION=steer): focus the composed answer on the user's
                # REAL intent + what a great answer must deliver — a framing nudge only (states no facts;
                # the literal Question above is unchanged; grounding stays with the span-gate). "" → no-op.
                + (("\n\nFOCUS: answer to the user's underlying intent — " + _reflect_intent
                    + ((" A strong answer must deliver: " + _reflect_brief) if _reflect_brief else "")
                    + " Cover this while staying strictly grounded in the cited findings; do NOT pad.")
                   if (_reflect_intent or _reflect_brief) else "")
                # REASONING READ (flag): anchor the structured interpretation/confidence fields at the
                # KERNEL level, symmetric to the directly_addresses metadata above — the domain-free
                # mechanics live here; the domain MEANING (what each kind is, neutrality) is in the
                # directive below. Without this anchor the model composes great prose and leaves the
                # trailing structured fields empty (the fields have defaults, so nothing forces them).
                + ("\n\nSEPARATELY, you MUST ALSO populate the STRUCTURED Reasoning Read fields (required "
                   "outputs, NOT optional, separate from the answer prose above): `reasoning_purpose` "
                   "(one sentence naming the decision/outcome the reasoning serves), 2–5 `interpretation` "
                   "factors that each bear on that purpose, `reasoning_conclusion` (the informed judgment "
                   "toward the purpose), and the three-dimension `confidence` read — all following the "
                   "REASONING READ instructions in the directive below. Each interpretation factor must "
                   "set `basis_findings` to the finding number(s) it rests on and introduce no number/"
                   "date/dose not already in those findings."
                   if reasoning_read else "")
                + (("\n\n" + directive) if directive else "")
                # READABILITY (flag): plain-language STYLE layer, last so it governs prose across whatever
                # structure the contract chose. Never alters sections/citations (byte-identical when OFF).
                + (("\n\n" + _READABILITY_STYLE) if readable_prose else ""))
            comp = await llm.complete(
                system=system_prompt,
                messages=[{"role": "user", "content": compose_user}],
                response_format=ComposedAnswer, max_tokens=_COMPOSE_MAX_TOKENS)
            budget.charge(calls=1, tokens=comp.output_tokens)
            if diag is not None:
                diag["compose_calls"] += 1
            return comp.parsed

        # Compose must NOT be silently dropped on a transient LLM blip (the 'grounded, N claims,
        # empty answer' bug). It is the user-facing deliverable, so: (1) RETRY a few times — cheap
        # and idempotent, the findings are already in hand; (2) it is NOT gated on the loop budget
        # (a heavy gather must not starve the one call that writes the answer); (3) if it truly
        # can't complete, SURFACE a note + log it (Rule 13) rather than returning a blank answer.
        parsed = None
        text = ""
        for _attempt in range(max(1, compose_attempts)):
            try:
                cand = await _compose(_compose_directive)
                text = strip_control_tags((cand.answer or "").strip())   # a malformed/empty parse raises or stays "" →
                parsed = cand                         # counted as this attempt's outcome, inside the try
                if text:
                    break                             # got a real answer — done
                raise ValueError("empty compose answer")   # empty → treat as a failed attempt, retry
            except Exception as _e:   # noqa: BLE001
                _log.warning("compose attempt %d/%d failed: %r", _attempt + 1, compose_attempts, _e)
                if _attempt + 1 < compose_attempts:
                    await asyncio.sleep(_COMPOSE_BACKOFF_S * (_attempt + 1))   # backoff for a transient error
        if text:
            # Domain-free provenance check: if a directive produced an answer with a bad/absent [n]
            # reference, retry ONCE with the SAME directive (a fresh sample usually fixes an [n]
            # fluke while preserving the directive's AUDIENCE/tone — a directive-free recompose would
            # replace e.g. a patient answer with a generic clinician-toned one). Best-effort: a failed
            # fallback never overwrites the answer we already have.
            if _compose_directive and not _refs_valid(text, n_findings):
                if diag is not None:
                    diag["retries"]["compose_ref_retry"] = True
                try:
                    alt = await _compose(_compose_directive)
                    if (alt.answer or "").strip():
                        parsed, text = alt, strip_control_tags(alt.answer.strip())
                except Exception as _e:   # noqa: BLE001
                    _log.warning("compose ref-retry failed: %r", _e)
            # Reasoning-read reliability: the model sometimes writes the prose answer but SKIPS the
            # structured reasoning fields (worse on dense, table-heavy answers with a long directive) —
            # so the reasoning section is missing on some turns and present on others. When it's asked
            # for but absent, recompose ONCE and GRAFT the reasoning onto the existing answer (the
            # findings are fixed, so the retry's reasoning rests on the same evidence). Answer prose is
            # preserved; only the missing reasoning fields are filled.
            if reasoning_read and not (getattr(parsed, "interpretation", None)
                                       or getattr(parsed, "confidence", None)):
                if diag is not None:
                    diag["retries"]["reasoning_retry"] = True
                try:
                    alt = await _compose(_compose_directive)
                    if getattr(alt, "interpretation", None) or getattr(alt, "confidence", None):
                        parsed.interpretation = alt.interpretation
                        parsed.confidence = alt.confidence
                        parsed.reasoning_purpose = alt.reasoning_purpose
                        parsed.reasoning_conclusion = alt.reasoning_conclusion
                except Exception as _e:   # noqa: BLE001 — best-effort; a failed retry leaves the answer intact
                    _log.warning("compose reasoning-retry failed: %r", _e)
            result.composed_answer = text
            # DEEP SYNTHESIS (flag) — CORRECTIVE prose grounding-audit. The deep directive widens the
            # free-prose surface the span-gate does NOT cover (it gates the claims list, not the prose),
            # so a richer answer could introduce a figure absent from the findings. Turn the diagnostic
            # `_unsupported_prose_tokens` check into a CORRECTIVE one for deep runs: (1) recompose ONCE
            # asking the model to drop the offending figures; (2) if any remain, FALL BACK to the
            # non-deep compose (the protected baseline) rather than ship widened ungrounded prose.
            # Entirely inside the deep gate → OFF / lookup path never runs it (byte-identical).
            # PARAMETRIC-LED (ROSTER_PARAMETRIC_LED, T3): the corrective audit ALSO fires when the model
            # LED this run (a prior_draft is present) — the parametric compose reasons from the model's
            # own draft, so a qualitative model sentence must not ride an unsupported figure into grounded
            # prose. Same recompose-once-then-fall-back-to-baseline logic; only the trigger is widened.
            # OFF (prior_draft is None and not a deep non-lookup run) → this block never runs (unchanged).
            # GOLDEN-ANSWER (ROSTER_GOLDEN_ANSWER): the golden directive frees the prose surface exactly like
            # deep-synthesis does (it is the sole compose base, all other layers OFF), so the SAME corrective
            # hard-token audit must run — otherwise collapsing to one prompt would silently strip figure
            # grounding. `golden_answer` widens the trigger; OFF → unchanged.
            if (deep_synthesis and kind != "lookup") or prior_draft is not None or golden_answer:
                _u = _unsupported_prose_tokens(result.composed_answer, result.verified_claims)
                if _u and not budget.exhausted:
                    _fix = ("\n\nGROUNDING FIX (mandatory): you stated figure(s) "
                            + ", ".join(sorted(_u)) + " that are NOT present in the findings — remove "
                            "them; keep ONLY figures that appear verbatim in the findings above.")
                    if diag is not None:
                        diag.setdefault("retries", {})["deep_prose_audit"] = sorted(_u)
                    try:
                        _alt = await _compose((_compose_directive or "") + _fix)
                        _alt_text = strip_control_tags((_alt.answer or "").strip())
                        if _alt_text and _refs_valid(_alt_text, n_findings):
                            parsed, text = _alt, _alt_text
                            result.composed_answer = text
                    except Exception as _e:   # noqa: BLE001 — best-effort; keep the answer we have
                        _log.warning("deep-synthesis prose-audit recompose failed: %r", _e)
                    # RE-AUDIT: still-unsupported → fall back to the NON-DEEP compose (normal base
                    # directive). Prefer a grounded plain answer over a richer ungrounded one.
                    _u2 = _unsupported_prose_tokens(result.composed_answer, result.verified_claims)
                    if _u2 and not budget.exhausted:
                        # the non-deep base is `answer_format` (the deep base was `deep_answer_format`);
                        # re-run compose with it so the prose reverts to the validated baseline shape.
                        try:
                            _fb = await _compose(answer_format)
                            _fb_text = strip_control_tags((_fb.answer or "").strip())
                            if _fb_text and _refs_valid(_fb_text, n_findings):
                                parsed, text = _fb, _fb_text
                                result.composed_answer = text
                                result.deep_synthesis_fell_back = True
                        except Exception as _e:   # noqa: BLE001
                            _log.warning("deep-synthesis non-deep fallback failed: %r", _e)
                        # Whatever remains after fallback: record it LOUDLY (never silently ship it).
                        _resid = _unsupported_prose_tokens(result.composed_answer, result.verified_claims)
                        if _resid:
                            _log.warning("deep-synthesis: residual unsupported figures after fallback: %s",
                                         sorted(_resid))
                            if diag is not None:
                                diag["failures"].append(
                                    {"stage": "deep_prose_grounding",
                                     "detail": "residual unsupported figures: " + ", ".join(sorted(_resid))})
            # INTELLIGENCE-CORE (ROSTER_INTELLIGENCE_CORE, T4) — SEMANTIC cross-family grounding gate. The
            # hard-token audit above catches unsupported FIGURES only; a laundered qualitative mechanism/
            # entity/relationship/causal claim asserted as fact rides straight through it. When the model
            # drafted competing HYPOTHESES (intelligence mode) AND a genuinely CROSS-FAMILY judge is
            # available, a different-family judge re-reads the composed prose + ONLY the verified claims and
            # flags any span asserting a mechanism/entity/date/outcome/causal claim NO claim supports. On a
            # hit: recompose ONCE asking to REMOVE or RELABEL those spans as [[R]] inference; re-check; still
            # unsupported → one HARDER recompose (delete), then keep the best answer + log + record diag.
            # This is IN ADDITION to the hard-token audit (which already ran) — it never weakens it.
            # FAIL-CLOSED: no cross-family judge (`derive_judge_llm` is None or IS `llm`) / judge error / no
            # claims → the gate returns [] → no action → the hard-token audit result stands (today's
            # behavior). We NEVER run the gate same-family. OFF (hypotheses is None) → this whole block is
            # skipped → byte-identical.
            # GOLDEN-ANSWER (ROSTER_GOLDEN_ANSWER): the semantic gate catches laundered QUALITATIVE claims
            # (a mechanism/entity/causal assertion the hard-token audit misses) — exactly the risk a freer,
            # more synthetic golden answer raises. Re-bind it to `golden_answer` too so it runs on golden
            # runs whenever a cross-family judge is wired (ROSTER_CROSS_FAMILY_JUDGE, kept ON in golden mode).
            # Fail-closed when no cross-family judge → returns [] → today's behavior. OFF → unchanged.
            _ground_judge = (derive_judge_llm
                             if (derive_judge_llm is not None and derive_judge_llm is not llm) else None)
            if (hypotheses is not None or golden_answer) and _ground_judge is not None and result.composed_answer:
                from roster_kernel.research.grounding_gate import cross_family_ground_check
                try:
                    _bad = await cross_family_ground_check(
                        result.composed_answer, result.verified_claims, _ground_judge, budget=budget)
                except Exception as _e:   # noqa: BLE001 — a gate failure fails CLOSED, never breaks the answer
                    _log.warning("intelligence grounding gate failed: %r", _e); _bad = []
                if diag is not None and _bad:
                    diag["intelligence_grounding"] = {
                        "flagged": _bad[:20], "recomposed": False, "hard_recomposed": False, "resolved": False}
                if _bad and not budget.exhausted:
                    _spans = "\n".join(f'  - "{s}"' for s in _bad[:20])
                    _gfix = ("\n\nGROUNDING FIX (mandatory): the following statement(s) assert a mechanism, "
                             "entity, date, outcome, or causal claim that is NOT supported by any verified "
                             "finding above:\n" + _spans + "\nFor EACH: either REMOVE it, or — if it is your "
                             "own reasoning over the findings — RELABEL it as clearly-marked inference wrapped "
                             "[[R]]...[[/R]], never asserted as fact. Keep every [n] citation; state facts "
                             "using ONLY the verified findings.")
                    try:
                        _galt = await _compose((_compose_directive or "") + _gfix)
                        _galt_text = strip_control_tags((_galt.answer or "").strip())
                        if _galt_text and _refs_valid(_galt_text, n_findings):
                            parsed, text = _galt, _galt_text
                            result.composed_answer = text
                            if diag is not None:
                                diag["intelligence_grounding"]["recomposed"] = True
                    except Exception as _e:   # noqa: BLE001 — best-effort; keep the answer we have
                        _log.warning("intelligence grounding recompose failed: %r", _e)
                    # RE-CHECK the (re)composed prose. Still unsupported → one HARDER recompose (delete, do
                    # not relabel). Prefer a grounded answer over a richer laundered one.
                    try:
                        _bad2 = await cross_family_ground_check(
                            result.composed_answer, result.verified_claims, _ground_judge, budget=budget)
                    except Exception:   # noqa: BLE001
                        _bad2 = []
                    if _bad2 and not budget.exhausted:
                        _spans2 = "\n".join(f'  - "{s}"' for s in _bad2[:20])
                        _hfix = ("\n\nGROUNDING FIX (final, mandatory): DELETE the following unsupported "
                                 "statement(s) ENTIRELY — do not restate, hedge, or relabel them. Write the "
                                 "answer using ONLY facts present in the verified findings:\n" + _spans2)
                        try:
                            _hgalt = await _compose((_compose_directive or "") + _hfix)
                            _hgalt_text = strip_control_tags((_hgalt.answer or "").strip())
                            if _hgalt_text and _refs_valid(_hgalt_text, n_findings):
                                parsed, text = _hgalt, _hgalt_text
                                result.composed_answer = text
                                if diag is not None:
                                    diag["intelligence_grounding"]["hard_recomposed"] = True
                        except Exception as _e:   # noqa: BLE001
                            _log.warning("intelligence grounding hard-recompose failed: %r", _e)
                        # Whatever remains: keep the best answer, but record it LOUDLY (Rule 13) — never
                        # silently ship laundered prose; the caller/UI can surface the residual spans.
                        if diag is not None:
                            diag["intelligence_grounding"]["residual"] = _bad2[:20]
                        _log.warning("intelligence grounding: residual unsupported spans after recompose: %s",
                                     _bad2[:5])
                        if diag is not None:
                            diag["failures"].append(
                                {"stage": "intelligence_grounding",
                                 "detail": "residual unsupported spans: " + "; ".join(_bad2[:5])})
                    elif diag is not None:
                        diag["intelligence_grounding"]["resolved"] = True
            # Grounded charts: keep only bars whose figure appears in the cited finding (drop the whole
            # chart otherwise). Empty when the charts flag isn't driving the directive → no-op.
            result.charts = _validate_charts(getattr(parsed, "charts", []) or [], result.verified_claims)
            # Reasoning Read (flag): validate the interpretation layer (drop dangling/fabricated items)
            # and carry the confidence read. Gated on the flag so the OFF path never surfaces them even
            # if the model volunteered them; the guard is fail-safe (a fabricated inference is dropped).
            if reasoning_read:
                result.interpretation = _validate_interpretation(
                    getattr(parsed, "interpretation", []) or [], result.verified_claims)
                conf = getattr(parsed, "confidence", None)
                result.confidence = conf.model_dump() if conf is not None else None
                # DEEP SYNTHESIS (flag) — the confidence RATIONALE is free prose too (currently ungated),
                # so a deep run validates each dimension's rationale for unsupported hard tokens and BLANKS
                # any that introduce a figure absent from the findings (fail-safe; the level band stays).
                # Gated on the deep run so the non-deep reasoning-read path is byte-identical.
                if (deep_synthesis and kind != "lookup" and isinstance(result.confidence, dict)):
                    for _dim in ("factual", "causal", "generalization"):
                        _d = result.confidence.get(_dim)
                        if isinstance(_d, dict):
                            _rat = (_d.get("rationale") or "").strip()
                            if _rat and _unsupported_prose_tokens(_rat, result.verified_claims):
                                _d["rationale"] = ""
                # Purpose + conclusion FRAME the answer — they restate/synthesize figures already in the
                # grounded COMPOSED ANSWER, not just single claim atoms. So the no-new-facts allowance is
                # the union of (verified findings) AND (the composed answer); see _frame_grounded. Without
                # the answer in the allowance, a valid Informed judgment vanished whenever it cited a
                # figure present in the answer but not verbatim in a claim atom (e.g. "≤1 hour/day").
                _all_tokens = extract_hard_tokens(
                    " ".join((vc.text + " " + vc.quote) for vc in result.verified_claims)
                    + " " + _REF_MARK_RE.sub(" ", result.composed_answer or ""))
                # strip_control_tags FIRST: a completion can bleed its own serialization into a frame
                # field value (e.g. "…pivot.</reasoning_conclusion> <parameter name=\"confidence\">{…}"),
                # and _frame_grounded only checks hard-token grounding — so a leaked blob whose tokens all
                # happen to appear in the answer would survive into the "Informed judgment" UI. Truncate at
                # the first control tag, THEN ground the clean text.
                result.reasoning_purpose = _frame_grounded(
                    strip_control_tags(getattr(parsed, "reasoning_purpose", "")), _all_tokens)
                result.reasoning_conclusion = _frame_grounded(
                    strip_control_tags(getattr(parsed, "reasoning_conclusion", "")), _all_tokens)
                # REPAIR (once): when the guard blanks a frame the model DID write (it stated a figure
                # outside the allowance), the judgment itself is usually valid — only the number is
                # unlicensed. One small call restates the frame QUALITATIVELY (no figures), then the
                # SAME guard re-validates. The guard stays authoritative (a still-failing repair stays
                # blank); the LLM owns the rewrite (Rule 18). This is why "Informed judgment" no longer
                # vanishes at random on numerically-dense answers.
                _blank = [k for k, raw in (("reasoning_purpose", getattr(parsed, "reasoning_purpose", "")),
                                           ("reasoning_conclusion", getattr(parsed, "reasoning_conclusion", "")))
                          if (raw or "").strip() and not getattr(result, k)]
                if _blank and not budget.exhausted:
                    if diag is not None:
                        diag["retries"]["frame_repair"] = list(_blank)
                    try:
                        class _FrameFix(BaseModel):
                            reasoning_purpose: str = ""
                            reasoning_conclusion: str = ""
                        fix = await llm.complete(
                            system=("Restate these reasoning-frame fields WITHOUT any specific numbers, "
                                    "dates, doses, or percentages — express the same judgment "
                                    "qualitatively (e.g. 'a small absolute benefit', 'roughly double'). "
                                    "Keep the direction and force of the judgment; do not add facts and "
                                    "do not hedge it into vagueness. Return both fields."),
                            messages=[{"role": "user", "content":
                                       "reasoning_purpose: " + (getattr(parsed, "reasoning_purpose", "") or "")
                                       + "\n\nreasoning_conclusion: "
                                       + (getattr(parsed, "reasoning_conclusion", "") or "")}],
                            response_format=_FrameFix, max_tokens=400)
                        # BudgetState honesty (stage-2 panel amendment): the frame-repair call was
                        # a real, previously-unmetered LLM call — charge it (charge-after, like
                        # compose: the block is already gated on `not budget.exhausted`).
                        budget.charge(calls=1, tokens=fix.output_tokens)
                        fp = fix.parsed
                        if "reasoning_purpose" in _blank:
                            result.reasoning_purpose = _frame_grounded(
                                strip_control_tags(getattr(fp, "reasoning_purpose", "")), _all_tokens)
                        if "reasoning_conclusion" in _blank:
                            result.reasoning_conclusion = _frame_grounded(
                                strip_control_tags(getattr(fp, "reasoning_conclusion", "")), _all_tokens)
                    except Exception as _e:   # noqa: BLE001 — best-effort; guard outcome stands
                        _log.warning("reasoning frame repair failed: %r", _e)
            # Honesty signal → coverage gap: a "grounded-on-analogues" answer still flags the gap,
            # so the UI shows the prominent fill-the-gaps affordance (LLM-owned judgment, no regex).
            if parsed.directly_addresses is False and (parsed.gap_note or "").strip():
                result.coverage_gaps.append(parsed.gap_note.strip())
        if not result.composed_answer:
            # Every compose attempt failed — SURFACE it (never a silent blank); the verified
            # evidence still stands and is shown, and the user is told to retry.
            result.compose_failed = True
            result.composed_answer = _COMPOSE_FAIL_NOTE
            _log.warning("compose produced NO answer despite %d verified findings", n_findings)
            if diag is not None:
                diag["failures"].append({"stage": "compose",
                                         "detail": f"exhausted {compose_attempts} attempts — answer not generated"})

    # per-source contribution: retrieved (atoms) vs. cited (verified claims)
    stats: dict[str, dict[str, int]] = {}
    for a in atoms.all():
        s = a.source_key or "unknown"
        stats.setdefault(s, {"retrieved": 0, "cited": 0})["retrieved"] += 1
    for vc in result.verified_claims:
        s = vc.source_key or "unknown"
        stats.setdefault(s, {"retrieved": 0, "cited": 0})["cited"] += 1
    result.source_stats = stats

    # SEARCH-SOURCE ATTRIBUTION (web): per engine, how much of its evidence was RETRIEVED vs actually
    # CITED (relevance/landing rate), and how many cited pages ONLY it surfaced (novelty). Reads the
    # `web_provider`/`web_providers` facets that ride on each web atom + its cited claim. Empty when the
    # web leg is single-provider or unused → no attribution noise.
    def _row(p):
        return _wp.setdefault(p, {"retrieved": 0, "cited": 0, "unique_cited": 0})
    _wp: dict[str, dict[str, int]] = {}
    for a in atoms.all():
        p = (getattr(a, "facets", None) or {}).get("web_provider")
        if p:
            _row(p)["retrieved"] += 1
    for vc in result.verified_claims:
        f = getattr(vc, "facets", None) or {}
        p = f.get("web_provider")
        if not p:
            continue
        r = _row(p)
        r["cited"] += 1
        provs = [x for x in (f.get("web_providers") or p).split(",") if x]
        if len(set(provs)) == 1:                 # only this engine returned the page → novel to it
            r["unique_cited"] += 1
    result.web_providers = _wp

    # Troubleshooting summary (flag): fold the captured trace into a compact, UI-ready shape. Pure
    # bookkeeping over data already in hand — no extra model calls; None unless collect_diagnostics.
    if diag is not None:
        rej_by_reason: dict[str, int] = {}
        for rc in result.rejected_claims:
            rej_by_reason[rc.reason] = rej_by_reason.get(rc.reason, 0) + 1
        n_search = sum(1 for t in diag["trace"] if t.get("action") == "search")
        compose_calls = diag.pop("compose_calls", 0)
        diag["retries"]["compose"] = max(0, compose_calls - 1)   # attempts beyond the first
        diag["funnel"] = {
            "atoms_gathered": len(atoms.all()),
            "claims_emitted": len(result.verified_claims) + len(result.rejected_claims),
            "verified": len(result.verified_claims),
            "rejected": len(result.rejected_claims),
            "rejected_by_reason": rej_by_reason,
        }
        diag["tool_calls"] = {
            "llm_total": budget.spent_calls,
            "planner_steps": result.steps,
            "searches": n_search,
            "web_enabled": aux_source is not None,
            "compose_calls": compose_calls,
        }
        diag["budget"] = {"llm_calls": budget.spent_calls, "max_calls": budget.max_calls,
                          "tokens": budget.spent_tokens}
        diag["stopped_reason"] = result.stopped_reason
        diag["retried_empty"] = result.retried_empty
        diag["compose_failed"] = result.compose_failed
        # A4: evidence-tier histogram of the cited findings (prod-observable evidence-fitness signal).
        tiers: dict[str, int] = {}
        for vc in result.verified_claims:
            k = getattr(vc, "evidence_kind", "") or "unclassified"
            tiers[k] = tiers.get(k, 0) + 1
        diag["evidence_tiers"] = tiers
        # A6: hard-token scan of the PROSE answer — a number/dose/date/% in the prose that is NOT in any
        # verified finding is a potential fabrication the structured guards can't see. Report it (never
        # auto-drop the answer). Deterministic, no model call.
        unsupported = _unsupported_prose_tokens(result.composed_answer, result.verified_claims)
        if unsupported:
            diag["failures"].append({"stage": "prose_grounding",
                                     "detail": "unsupported figures in prose: " + ", ".join(sorted(unsupported))})
        diag["prose_unsupported_tokens"] = sorted(unsupported)
        diag["duration_ms"] = int((time.monotonic() - _diag_t0) * 1000)
        # attribute wall-clock: per-Anthropic-call latencies + the LLM-vs-other split
        if _call_log_tok is not None:
            calls = list(_call_log)
            _LLM_CALL_LOG.reset(_call_log_tok)
            llm_ms = sum(c["ms"] for c in calls)
            diag["llm_calls_detail"] = calls
            _tm = diag["timing"]
            _measured = (_tm.get("judge_ms", 0) + _tm.get("retrieval_ms", 0)
                         + _tm.get("contract_legs_ms", 0) + _tm.get("graph_legs_ms", 0) + _tm.get("embed_ms", 0))
            _tm.update({
                "total_ms": diag["duration_ms"],
                "anthropic_calls": len(calls),
                "anthropic_ms": llm_ms,
                "anthropic_slowest_ms": max((c["ms"] for c in calls), default=0),
                "non_anthropic_ms": max(0, diag["duration_ms"] - llm_ms),   # retrieval+embed+OpenAI judges+overhead
                # residual = non-Anthropic wall-clock not attributed to a measured phase (overlap/overhead)
                "unattributed_ms": max(0, diag["duration_ms"] - llm_ms - _measured),
            })
            # one-line human-readable breakdown for quick reading in logs / the diag payload
            _parts = [f"total={diag['duration_ms']/1000:.1f}s",
                      f"anthropic={llm_ms/1000:.1f}s({len(calls)} calls)"]
            for _k in ("judge_ms", "retrieval_ms", "embed_ms", "contract_legs_ms", "graph_legs_ms"):
                if _tm.get(_k):
                    _parts.append(f"{_k[:-3]}={_tm[_k]/1000:.1f}s")
            _tm["summary"] = " · ".join(_parts)
            _log.info("Q&A latency breakdown: %s", _tm["summary"])
        result.diagnostics = diag
    return result


# A figure the compose EXPLICITLY tagged as an unverified estimate, e.g. "~$4.2B (est., unverified)" or
# "10k customers (est., unverified)". These ride the semantic gate's existing analytical-read exemption
# (grounding_gate.py) and are DELIBERATELY exempt from the hard-token audit too (user-authorized flagged
# estimates for private-company columns). ONLY a figure carrying this exact marker is exempt — every
# other figure stays fully audited, so unlabeled fabrication is still stripped.
_ESTIMATE_SPAN_RE = re.compile(
    r"[~≈]?\$?\d[\d.,]*\s*[BMKkbmt%]?[A-Za-z ]{0,24}?\(\s*est\.?,?\s*unverified\s*\)", re.I)


def _unsupported_prose_tokens(prose: str, verified: list["VerifiedClaim"]) -> set[str]:
    """Hard tokens (number/dose/date/%) in the composed PROSE that appear in NO verified finding's
    text/quote — i.e. figures the prose introduced that the evidence doesn't support. Structural
    (Rule 18); the compose fail-note has none, so a failed compose reports nothing. Inline citation
    markers [n] are STRIPPED first (they're references, not figures); figures EXPLICITLY tagged
    '(est., unverified)' are stripped too (authorized flagged estimates — see _ESTIMATE_SPAN_RE)."""
    if not prose:
        return set()
    clean = re.sub(r"\[\d+\]", " ", prose)          # citation refs are not evidence figures
    clean = _ESTIMATE_SPAN_RE.sub(" ", clean)       # flagged estimates are exempt (labeled non-facts)
    src = " ".join((vc.text + " " + vc.quote) for vc in verified)
    return extract_hard_tokens(clean) - extract_hard_tokens(src)
