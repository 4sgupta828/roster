"""FastAPI app — POST /research over the multi-source agent.

Vertical-neutral: it activates ONE vertical at boot (ROSTER_ACTIVE_VERTICAL) and
serves its sources + gating + persona. Providers run in ROSTER_PROVIDER_MODE
(replay by default → offline/free). A ResearchService can be injected for tests.
"""
from __future__ import annotations

import asyncio
import json
import os
import urllib.parse
import re
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from roster_kernel.providers.base import resolve_mode
from roster_kernel.providers.cassette import CassetteMiss
from roster_kernel.retrieval.postgres import PostgresRetrievalSource
from roster_kernel.retrieval.web import WebRetrievalSource
from roster_kernel.runtime.build import build_embedder, build_llm, build_web, load_active_vertical
from roster_kernel.runtime.ingest import ingest_connector_to_postgres
from roster_kernel.runtime.research import ResearchService

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# ---- Resumable SSE runs -----------------------------------------------------
# Railway's edge closes long-lived SSE connections after ~30-60s in steady state
# regardless of keepalives (verified with the LLM-free /admin/stream-test probe:
# clean EOF after ~3 pings — every client, no deploy in flight, no app restart).
# The app can't prevent that, so streams are RESUMABLE instead: every streaming
# run buffers its events here under a run_id, and GET /stream/{run_id}?since=N
# replays the buffer from any cursor and follows live. The FE reconnects
# silently on any drop, so an edge cut becomes a sub-second blip instead of an
# error. The buffer is per-replica process memory: with numReplicas>1 a resume
# can land on a replica that never saw the run (404) — the FE retries (each
# attempt re-rolls the replica) and ultimately falls back to /sessions polling,
# which reads the cross-replica store.
_SSE_RUNS: dict[str, dict] = {}
_SSE_RUN_TTL = 30 * 60   # keep finished runs resumable this long


def _sse_run_new() -> dict:
    now = time.time()
    for k in [k for k, v in _SSE_RUNS.items() if now - v["ts"] > _SSE_RUN_TTL]:
        _SSE_RUNS.pop(k, None)
    run = {"id": uuid.uuid4().hex, "events": [], "done": False, "ts": now, "task": None}
    _SSE_RUNS[run["id"]] = run
    return run


def _sse_push(run: dict, ev: dict) -> None:
    run["events"].append(ev)
    run["ts"] = time.time()


def _sse_done(run: dict) -> None:
    run["done"] = True
    run["ts"] = time.time()


async def _sse_follow(run: dict, since: int = 0):
    """Yield the run's events from cursor `since` (each stamped with `_seq` so the client knows
    its resume cursor), following live with 15s pings until the run is done. The events list is
    append-only on a single event loop, so no locking is needed."""
    idx = max(0, int(since))
    last_beat = time.time()
    while True:
        events = run["events"]
        if idx < len(events):
            while idx < len(run["events"]):
                yield f"data: {json.dumps(dict(run['events'][idx], _seq=idx))}\n\n"
                idx += 1
            last_beat = time.time()
        elif run["done"]:
            return
        else:
            await asyncio.sleep(0.4)
            if time.time() - last_beat >= 15:
                yield ": ping\n\n"
                last_beat = time.time()


def structured_answers() -> bool:
    """Flag (default OFF, Rule 20): when ON, the active vertical's answer_format
    directive shapes the synthesized answer (markdown sections). OFF = flat prose,
    byte-identical to the pre-flag path."""
    return os.environ.get("ROSTER_STRUCTURED_ANSWERS", "").lower() in ("1", "true", "yes")


def clinical_synthesis() -> bool:
    """Flag (default OFF, Rule 20): when ON (and structured answers are ON), the medical vertical's
    SHARPER clinical-synthesis directive shapes the answer — scope-up-front, registry=protocol-not-
    efficacy, surrogate≠clinical endpoints, preserve specific figures, no citation stacking, no vague
    hype. Same adaptive section set — provenance unchanged. OFF → the base answer_format, byte-identical."""
    return os.environ.get("ROSTER_CLINICAL_SYNTHESIS", "").lower() in ("1", "true", "yes")


def vision_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, uploaded image/PDF/DICOM attachments are
    described by the vision pre-step and used as CONTEXT for the grounded research. The
    description is never a verified claim. OFF → attachments are ignored."""
    return os.environ.get("ROSTER_VISION", "").lower() in ("1", "true", "yes")


def gap_healing_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, an under-evidenced answer can surface a gap-fill
    plan (LLM-proposed connector ingest jobs) that the user queues, and a background processor
    ingests them into the corpus — self-healing. OFF → no gap plan, endpoints 404, no processor."""
    return os.environ.get("ROSTER_GAP_HEALING", "").lower() in ("1", "true", "yes")


def pulse_enabled() -> bool:
    """Flag (default OFF, Rule 20): Evidence Pulse P0 — the corpus-currency subsystem.
    ON: curator-declared lineage sweeps into the change-event ledger, approved events stamp
    superseded/retracted facets onto blocks, and retrieval + claim ranking demote superseded
    (exclude retracted) sources. OFF → no ledger, no stamps, no demotion — byte-identical."""
    return os.environ.get("ROSTER_PULSE", "").lower() in ("1", "true", "yes")


def graph_enabled() -> bool:
    """Flag (default OFF, Rule 20): the Grounded Relationship Graph P0
    (learnings/knowledgegraph.md) — typed curated edges between canonical topics, admin
    sync/list surface, /graph/related read API, and A5 evidence invalidation. OFF → no
    tables, endpoints 404, byte-identical serving."""
    return os.environ.get("ROSTER_GRAPH", "").lower() in ("1", "true", "yes")


def graph_pulse_enabled() -> bool:
    """Flag (default OFF; requires ROSTER_GRAPH): consumer C2 — a change event on topic X also
    surfaces to watchers of X's graph neighbors as a visually-distinct 'related change'."""
    return graph_enabled() and os.environ.get(
        "ROSTER_GRAPH_PULSE", "").lower() in ("1", "true", "yes")


def graph_expand_mode() -> str:
    """Consumer C1 (A9 graph-guided evidence legs) via ROSTER_GRAPH_EXPAND:
    "" (default) → off, byte-identical; "shadow" → legs retrieve + log, merge nothing;
    "late" (RECOMMENDED) → legs merge POST-LOOP before claims-first extraction — the planner
    searches exactly as with graph off (no early-stop possible), graph evidence is strictly
    additive; "1/true/on" → EARLY merge into the pre-loop pool (steers the planner; kept as
    the A/B arm). Requires ROSTER_GRAPH."""
    if not graph_enabled():
        return ""
    v = os.environ.get("ROSTER_GRAPH_EXPAND", "").lower()
    if v in ("shadow", "late"):
        return v
    return "on" if v in ("1", "true", "yes", "on") else ""


def graph_map_mode() -> str:
    """v3-P1 (default OFF): "llm" → when structural containment finds NO graph topic in the
    question, ONE small vocabulary-aware mapping call resolves synonyms/abbreviations
    ("parkinsonism" → Parkinson disease). Code validates verbatim vocabulary membership —
    the model can never mint a topic. "" → containment-only, byte-identical."""
    if not graph_enabled():
        return ""
    return "llm" if os.environ.get("ROSTER_GRAPH_MAP", "").lower() == "llm" else ""


_GRAPH_STORE = None
_GRAPH_MAP_LLM = None


def _graph_map_llm():
    global _GRAPH_MAP_LLM
    if _GRAPH_MAP_LLM is None:
        _GRAPH_MAP_LLM = build_llm(mode=resolve_mode())
    return _GRAPH_MAP_LLM


async def _map_question_topics(question: str, g) -> list[str]:
    """LLM fallback mapping (v3-P1). Fail-safe: any error/abstention → [] (no expansion)."""
    manifest = load_active_vertical()
    prompt = getattr(manifest, "graph_map_prompt", None)
    if not prompt:
        return []
    try:
        vocab = await g.edge_topics()
        if not vocab:
            return []
        from pydantic import BaseModel

        class _Mapped(BaseModel):
            topics: list[str] = []

        comp = await _graph_map_llm().complete(
            system=prompt + "\n\nTOPIC LIST:\n" + "\n".join(f"- {t}" for t in vocab),
            messages=[{"role": "user", "content": question[:2000]}],
            response_format=_Mapped, max_tokens=250)
        allowed = set(vocab)
        return [t for t in comp.parsed.topics if t in allowed][:2]   # verbatim members only
    except Exception:   # noqa: BLE001
        return []


def _graph_store():
    """Module-level lazy GraphStore singleton (one pool + one adjacency snapshot per process),
    shared by the API endpoints and the answer-path expander. None when off/unconfigured."""
    global _GRAPH_STORE
    if _GRAPH_STORE is None:
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if dsn and graph_enabled():
            from roster_kernel.graph import GraphStore
            manifest = load_active_vertical()
            _GRAPH_STORE = GraphStore(
                dsn, relations=tuple(getattr(manifest, "graph_relations", ()) or ()))
    return _GRAPH_STORE


_GRAPH_REL_PHRASE = {"increases_risk_of": "in", "causes": "in", "comorbid_with": "with",
                     "complication_of": "after", "treats": "for", "precipitates": "induced"}
# v3 C-3: masquerade relations OUTRANK confidence-1.0 comorbidity edges when consumed via an
# INCOMING edge (the question names the cover story) — that's the hard-case payoff.
_GRAPH_REL_PRIORITY = {"underlies_presentation_of": 0, "mimics": 0}
_GRAPH_DARK_RELATIONS = {"manifests_as"}     # C4's primitive — no leg consumer until v3-P1


def _graph_leg_query(nb: dict) -> str:
    """Per-relation leg template (v3 C-3). Masquerade legs search the HIDDEN topic presenting
    as the asked cover-story, discriminator included — the query the user never typed."""
    other = nb["object"] if nb["direction"] == "out" else nb["subject"]
    if nb["relation"] in ("mimics", "underlies_presentation_of") and nb["direction"] == "in":
        q = f"{nb['subject']} presenting as {nb['via']}"
        if nb.get("distinguished_by"):
            q += f" {nb['distinguished_by']}"
        return q
    phrase = _GRAPH_REL_PHRASE.get(nb["relation"], "and")
    return f"{other} {phrase} {nb['via']}"


def _make_graph_expander():
    """A9 hook for ResearchService: question → ≤2 edge-templated evidence-leg queries.
    Topic detection is precision-biased structural containment over EDGE-BEARING labels
    (no LLM call — no match means no expansion, fail-safe). None when the flag is off."""
    if not graph_expand_mode():
        return None

    async def _expand(question: str):
        mode = graph_expand_mode()          # resolved live so an env flip needs no rebuild
        g = _graph_store()
        if g is None or not mode:
            return None
        topics = await g.match_topics(question)
        if not topics and graph_map_mode() == "llm":
            topics = await _map_question_topics(question, g)   # synonym/abbrev fallback (+1 small call)
        if not topics:
            return None
        nbs = [nb for nb in await g.neighbors(topics, limit=12)
               if nb["relation"] not in _GRAPH_DARK_RELATIONS
               and not (nb["relation"] in _GRAPH_REL_PRIORITY and nb["direction"] == "out")]
        # masquerade edges (incoming) first, then the neighbor order (via-rank, confidence)
        nbs.sort(key=lambda nb: _GRAPH_REL_PRIORITY.get(nb["relation"], 1)
                 if nb["direction"] == "in" else 1)
        legs, seen = [], {t.lower() for t in topics}
        for nb in nbs:
            other = nb["object"] if nb["direction"] == "out" else nb["subject"]
            if other.lower() in seen:
                continue
            seen.add(other.lower())
            legs.append({"query": _graph_leg_query(nb),
                         "note": f"{nb['subject']} {nb['relation']} {nb['object']}"})
            if len(legs) == 2:
                break
        return ({"legs": legs, "shadow": mode == "shadow", "late": mode == "late"}
                if legs else None)

    return _expand


def stream_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, /research/stream serves live SSE progress events
    (searching/found/verifying/composing → final). OFF → the endpoint 404s; /research unchanged."""
    return os.environ.get("ROSTER_STREAM", "").lower() in ("1", "true", "yes")


def effort_scale_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, a request's `effort` multiplier (1.0..2.5) scales how
    hard the research loop works (turns, results considered, context, citations, LLM budget) on a
    hard question. OFF → `effort` is forced to 1.0 and ignored (byte-identical to today). Effort only
    scales STRUCTURAL search — the provenance/grounding gates are never touched."""
    return os.environ.get("ROSTER_EFFORT_SCALE", "").lower() in ("1", "true", "yes")


# Effort slider stops echoed to /config when the flag is on (UI renders the control from this).
EFFORT_STOPS = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]


def patient_mode_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, a request may choose audience='patient' to get a
    patient-facing answer (same evidence + gates, a plain-language compose directive). OFF → audience
    is forced 'clinician' and the toggle/echo are hidden (byte-identical to today)."""
    return os.environ.get("ROSTER_PATIENT_MODE", "").lower() in ("1", "true", "yes")


def answer_charts_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, append the vertical's chart guidance so compose may emit a
    grounded bar chart (each bar validated against its cited finding in code; ungrounded → dropped).
    Requires structured answers. OFF → the directive is unchanged and `charts` stays empty."""
    return os.environ.get("ROSTER_ANSWER_CHARTS", "").lower() in ("1", "true", "yes")


def visual_augment_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, each answer offers on-demand "Add visuals" — POST
    /visuals/augment restructures the finished answer into grounded conceptual visuals (flow/tree/
    timeline), every element quote-anchored to the answer. OFF → the endpoint 404s and the UI
    affordance is hidden (byte-identical to today). User-triggered — never adds answer latency.
    Distinct from ROSTER_ANSWER_CHARTS (inline numeric charts) and ROSTER_ANSWER_VISUALS (prose
    tables): this owns spatial/structural visuals only."""
    return os.environ.get("ROSTER_VISUAL_AUGMENT", "").lower() in ("1", "true", "yes")


def visual_auto_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON (with ROSTER_VISUAL_AUGMENT), the frontend AUTO-generates
    visuals on every fresh grounded answer (Q&A + Panel) instead of waiting for a click — visuals become
    part of the answer by default. Each fresh answer spends one extra LLM call, so this is a live-flippable
    rollback surface. OFF → visuals stay click-to-generate (byte-identical)."""
    return os.environ.get("ROSTER_VISUAL_AUTO", "").lower() in ("1", "true", "yes")


def voice_intake_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, Guided Intake offers a hands-free voice conversation —
    browser-native SpeechRecognition dictates the user's answers and SpeechSynthesis speaks each
    clarifying question. FE-only behavior (feature-detected per browser); this flag just gates the
    affordance via /config. OFF → the mic never shows (byte-identical to today)."""
    return os.environ.get("ROSTER_VOICE_INTAKE", "").lower() in ("1", "true", "yes")


def term_glossary_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, each answer offers on-demand key-term explanations
    (POST /terms/explain — definitional, with related-term edges) and the accumulated glossary is
    browsable at GET /glossary (the All Terms page). OFF → both endpoints 404 and the UI affordances
    are hidden (byte-identical to today). Explanations are user-triggered — never add answer latency."""
    return os.environ.get("ROSTER_TERM_GLOSSARY", "").lower() in ("1", "true", "yes")


def discovery_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON (with the vertical's discovery_entity_of), POST /discover
    surfaces the companies/orgs most associated with a capability query — the scouting/sourcing surface
    for corp-dev / M&A. OFF → the endpoint 404s (byte-identical to today)."""
    return os.environ.get("ROSTER_DISCOVERY", "").lower() in ("1", "true", "yes")


def refine_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, a FRESH question (no history) is first sent to /refine,
    which proposes a few distinct sharper standalone questions to pick from (express refinement). The
    LLM returns [] when the question is already precise → the FE just answers it. OFF → no /refine
    step (byte-identical); follow-ups are never refined (the resolver handles those)."""
    return os.environ.get("ROSTER_REFINE", "").lower() in ("1", "true", "yes")


def answer_visuals_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, append the vertical's visualization guidance to the
    compose directive so answers proactively use comparison tables / ranked options / pros-cons —
    strictly from the verified findings. Requires structured answers (tables render only then). OFF →
    the directive is unchanged (byte-identical)."""
    return os.environ.get("ROSTER_ANSWER_VISUALS", "").lower() in ("1", "true", "yes")


def ask_panel_enabled() -> bool:
    """Flag (default OFF, Rule 20 — ALPHA): when ON, a clinician can convene an AI specialist panel
    (`POST /panel/ask`) — each specialist runs its own grounded, lens-scoped research and the panel
    synthesizes their pooled verified findings. Costs N× a single answer; OFF → the endpoint 404s."""
    return os.environ.get("ROSTER_ASK_PANEL", "").lower() in ("1", "true", "yes")


def panel_dedup_enabled() -> bool:
    """Flag (default OFF, Rule 20 — panel upgrade P2, +0 LLM calls): when ON, the panel's pooled
    findings are DEDUPLICATED by (atom_id, normalized quote) — a claim several lenses independently
    established collapses to ONE survivor carrying every lens that found it, and its findings line
    renders the computed convergence ("found independently by N lenses: …") for the chair to weigh.
    OFF → pooling, findings strings, and claim dicts are byte-identical to today."""
    return os.environ.get("ROSTER_PANEL_DEDUP", "").lower() in ("1", "true", "yes")


def panel_contract_enabled() -> bool:
    """Flag (default OFF, Rule 20 — panel upgrade P3+P1, +1 LLM call per panel run): when ON, the
    panel derives ONE shared QuestionContract (the vertical's contract_prompt) BEFORE the specialists
    run — each lens's focus gains a scoped 'Ensure coverage of: …' line, the POOLED claims are
    slot-matched (entities for enumerative contracts, axes for exploratory), slots no specialist
    evidenced surface as panel-level coverage_gaps, and the synthesis directive routes to the
    vertical's panel enumerative/decision addendum when ≥2 slots hold evidence (stage-4 pattern).
    OFF → no derivation, no scoped lines, no gaps, base directive only (byte-identical)."""
    return os.environ.get("ROSTER_PANEL_CONTRACT", "").lower() in ("1", "true", "yes")


def duel_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, `engine="reasoned"` on /research[/stream] routes through
    the ALTERNATE reason-first engine (scaffold → coverage-steered retrieval → decision-gated compose)
    and the FE runs both engines on fresh questions as a blinded A/B with a which-is-better vote —
    clinician preference data that settles the retrieval-first vs reasoning-first question empirically.
    OFF → the engine param is ignored and no duel UI shows (byte-identical)."""
    return os.environ.get("ROSTER_DUEL", "").lower() in ("1", "true", "yes")


def ingest_in_api_enabled() -> bool:
    """Whether the API process runs the corpus-ingest drain thread. Default TRUE (single-service
    behavior). Set ROSTER_INGEST_IN_API=false once a SEPARATE ingest worker (deploy/Dockerfile.worker,
    which carries docling for full-text PDF parsing) runs the drain — so ingestion runs ONLY on the
    worker and the API stays lean. Job claims are atomic, so a brief overlap is safe, not doubled."""
    return os.environ.get("ROSTER_INGEST_IN_API", "true").lower() not in ("0", "false", "no", "off")


def pdf_bridge_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_PDF_BRIDGE opens POST /admin/corpus/ingest-pdf — the
    local→prod full-text bridge. arXiv rate-limits the PROD IP on PDF fetches (old papers with no HTML
    degrade to abstract-only), so a good-IP LOCAL box downloads the PDF and ships the bytes here; prod
    runs its already-working docling + the normal ingest (clean-replace recovers the abstract stub).
    OFF → the endpoint 404s (byte-identical)."""
    return os.environ.get("ROSTER_PDF_BRIDGE", "").lower() in ("1", "true", "yes")


def source_routing_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_SOURCE_ROUTING lets the research agent name source TYPES to
    ALSO target for a query (an additive scoped retrieval leg on top of the flat search — never a
    filter, so a mis-route can't lose recall). OFF → the agent's source_kinds field is ignored and no
    scoped leg runs (byte-identical)."""
    return os.environ.get("ROSTER_SOURCE_ROUTING", "").lower() in ("1", "true", "yes")


def retrieval_diversity_frac() -> float | None:
    """Flag (default OFF, Rule 20): ROSTER_RETRIEVAL_DIVERSITY caps any single source_key to
    ceil(k*frac) of the top-k fused retrieval pool, so a volume-skewed source (e.g. 464k SEC blocks
    vs a few thousand research/community blocks) can't crowd out other sources on broad queries.
    Backfill preserves recall. Value is the fraction (default 0.5 when the flag is on/"1"/"true", or a
    numeric override like "0.4"); OFF/unset/invalid → None → retrieval is byte-identical to today."""
    raw = os.environ.get("ROSTER_RETRIEVAL_DIVERSITY", "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return None
    if raw in ("1", "true", "yes", "on"):
        return 0.5
    try:
        v = float(raw)
        return v if 0.0 < v <= 1.0 else None
    except ValueError:
        return None


def reasoned_default_enabled() -> bool:
    """Flag (default OFF, live-toggleable): when ON, single answers (no explicit engine — i.e. whenever
    the A/B duel isn't running the question) DEFAULT to the REASONED engine: clinical scaffold →
    coverage-steered retrieval → decision-gated compose. Explicit engine="standard" (the duel's control
    arm) still runs standard, so duel votes keep their contrast. Costs +1 scaffold LLM call per fresh
    question. Grounding identical to the standard engine."""
    return os.environ.get("ROSTER_REASONED_DEFAULT", "").lower() in ("1", "true", "yes")


def integrative_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, the per-question "include complementary & integrative
    approaches" opt-in appears (itself off by default per question). Double opt-in: this flag gates the
    feature; the user chooses per question. Grounding invariant unchanged — the section only shapes what
    is searched and how VERIFIED findings are presented; evidence-strength labels are required."""
    return os.environ.get("ROSTER_INTEGRATIVE", "").lower() in ("1", "true", "yes")


def accounts_enabled() -> bool:
    """Flag (default OFF, Rule 20 — adoption P0): when ON, users register a real account
    (`POST /auth/register`, free verified-clinician tier via structural NPI lookup) and every answer
    carries a feedback affordance (`POST /feedback`) keyed to the W1–W9 warrant taxonomy — the
    accumulating ground-truth signal. OFF → endpoints 404 and the FE keeps the localStorage-only
    identity gate (byte-identical)."""
    return os.environ.get("ROSTER_ACCOUNTS", "").lower() in ("1", "true", "yes")


def triage_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, a "Guided" intake mode runs a short clarifying conversation
    (`POST /triage/step`) that converges on a crisp question and recommends a route (Quick Q&A vs
    Specialist Panel). One small LLM call per turn, hard-capped; it never answers or advises — only
    narrows + routes. OFF → the endpoint 404s and the FE shows only the two answer modes."""
    return os.environ.get("ROSTER_TRIAGE", "").lower() in ("1", "true", "yes")


# The clarifying-turn cap (structural convergence guarantee — code owns structure, the LLM owns meaning):
# after this many assistant questions, the next turn is FORCED to route. Keeps intake from interrogating.
TRIAGE_MAX_ASK = int(os.environ.get("ROSTER_TRIAGE_MAX_ASK", "2"))


def intake_v2_enabled() -> bool:
    """Flag (default OFF, Rule 20): Guided Intake v2 — /triage/step uses the vertical's v2 directive +
    the TriageTurnV2 schema (register choice fact/case, structured case_facts, clinical-register
    refined_question + retrieval_terms) with a per-REGISTER ask backstop. OFF → v1, byte-identical."""
    return os.environ.get("ROSTER_INTAKE_V2", "").lower() in ("1", "true", "yes")


# Under v2 the ask cap is a per-register BACKSTOP (the prompt owns convergence; code owns the ceiling):
# "fact" keeps the v1 cap; "case" (a described patient/situation) gets room for a structured intake.
TRIAGE_MAX_ASK_CASE = int(os.environ.get("ROSTER_TRIAGE_MAX_ASK_CASE", "8"))


def triage_ask_cap(v2: bool, register: str) -> int:
    """The forced-convergence ask cap for this turn. v1 → always TRIAGE_MAX_ASK. Under v2 the register
    (echoed by the LAST assistant turn, posted back by the FE) selects the backstop — absent/unknown
    defaults to the CASE cap so a lost echo never truncates a structured intake."""
    if not v2:
        return TRIAGE_MAX_ASK
    return TRIAGE_MAX_ASK if register == "fact" else TRIAGE_MAX_ASK_CASE


def evidence_fitness_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, the relevance-selection step additionally BOOSTS stronger
    evidence tiers (guideline/systematic-review > RCT > cohort > case report, via the medical authority
    pyramid) into the compose cap — so the answer rests on the best-tier evidence, not just the most
    similar text. Boost-only, provenance untouched. OFF → ranking is relevance-only (byte-identical)."""
    return os.environ.get("ROSTER_EVIDENCE_FITNESS", "").lower() in ("1", "true", "yes")


def authority_basis_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, an answer's FACTUAL BASIS leans on the highest-authority
    sources — low-basis claims (unattributed blog / social, tier rank<=1) are STABLY pushed to the back
    of the verified-claim pool so authoritative tiers fill the compose cap first (reorder only, NEVER
    dropped — breadth preserved), and a compose directive tells the composer to treat opinion/blog/
    social as supplementary signal, never the sole basis for a stated fact. Reuses `_suppress_auth`
    (opinion/foresight stances stay exempt). OFF → claim order + compose prompt byte-identical to today."""
    return os.environ.get("ROSTER_AUTHORITY_BASIS", "").lower() in ("1", "true", "yes")


def freshness_ranking_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, the vertical's `freshness_policy` re-orders the verified-
    claim pool by RECENCY across all evidence tiers over a short horizon (so a 2026 paper/repo/news
    outranks a 2024 one for a fast-moving field), and the answer carries an as-of/staleness disclosure.
    OFF → recency stays the kernel default (controlling-tier only, 12yr) = byte-identical to today."""
    return os.environ.get("ROSTER_FRESHNESS_RANKING", "").lower() in ("1", "true", "yes")


def answer_contract_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, one LLM classification per question derives an evidence
    STANCE (via the vertical's contract_prompt), and the vertical's `answer_profiles[stance]` re-tunes
    retrieval + ranking + compose for THAT question — "current" (latest/news → recency-first, authority
    suppressed, lead with newest) vs "established" (proven/benchmarked/reviewed → authority-first) vs
    "balanced" (today's behavior). Retrieval stays generic; the contract is the single locus of
    intelligence. OFF → no stance is resolved and every knob is byte-identical to today."""
    return os.environ.get("ROSTER_ANSWER_CONTRACT", "").lower() in ("1", "true", "yes")


def evidence_identity_enabled() -> bool:
    """Flag (default OFF, Rule 20 — Evidence Contract stage 1): when ON, every LLM-visible evidence
    surface (planner observations, claims-first extractor + entailment items, compose findings, panel
    synthesis, fallback grounder) renders each atom's DOCUMENT IDENTITY — ⟨title — source⟩ — and the
    claim-writing instructions require attributing each claim to its source's actual subject (never
    generalizing a source to a different subject or class). Zero extra LLM calls, ~10 tokens/atom.
    OFF → every prompt string is byte-identical to today."""
    return os.environ.get("ROSTER_EVIDENCE_IDENTITY", "").lower() in ("1", "true", "yes")


def claim_congruence_enabled() -> bool:
    """Flag (default OFF, Rule 20 — Evidence Contract stage 2): when ON, ONE unified batched BINDING
    judge covers all three claim paths (loop-emitted, claims-first, fallback-grounder — closing the
    entailment bypass the sitagliptin failure rode). Each claim is judged {entailed, on_subject,
    kind_ok} against its source's identity tag + structural evidence kind: off-subject or unentailed
    → dropped; kind-mismatch → kept but demoted + annotated; judge unavailable → kept + "unjudged"
    (never dropped on judge failure, never a keyword fallback). Cost: 0–1 extra batched call. OFF →
    stage-1-only prompts and enforcement, byte-identical."""
    return os.environ.get("ROSTER_CLAIM_CONGRUENCE", "").lower() in ("1", "true", "yes")


def question_contract_mode() -> str:
    """Evidence Contract stage 3 (Rule 20, default OFF) via ROSTER_QUESTION_CONTRACT:
    "" (default) → off, byte-identical; "shadow" → derive the QuestionContract (mode/entities/
    axes, ONE small charged LLM call on the vertical's contract_prompt) + compute the per-entity
    retrieval legs + log contract/legs/slot-grid to the diag trace — NO leg retrieval, NO
    selection change; "steer" → enumerative contracts additionally EXECUTE the legs (cap 8,
    round-robin across entities, k=4 each, concurrent, late-merged post-loop like graph legs —
    baseline retrieval unchanged), compose selection reserves cap seats for slot-filling claims
    (an off-slot claim can never evict a slot-filler), and entities left with zero claims become
    honest loop-produced coverage gaps. Any other value → off."""
    v = os.environ.get("ROSTER_QUESTION_CONTRACT", "").lower()
    return v if v in ("shadow", "steer") else ""


def reflection_mode() -> str:
    """Reflection pass (Rule 20, default OFF) via ROSTER_REFLECTION. "" (default) → off, byte-identical;
    "shadow" → derive the enriched reflection contract (intent/confidence/answer_brief/ambiguity/
    candidates) + LOG the web-coverage legs it WOULD fire — steer/collect NOTHING; "steer" → thread
    the intent into planner+compose (confidence>=medium), fan out ON-DEMAND WEB coverage legs for
    landscape/multi-entity questions (the "muted: didn't look" fix), and run interactive grounded
    disambiguation when genuinely ambiguous. Any other value → off. See docs/specs/reflection_pass.md."""
    v = os.environ.get("ROSTER_REFLECTION", "").lower()
    return v if v in ("shadow", "steer") else ""


def _apply_reflection_addendum(base_prompt, manifest):
    """When ROSTER_REFLECTION is on, append the vertical's reflection addendum to the active contract
    prompt so the ONE derivation call also returns the heart-of-intent. OFF → base unchanged (byte-
    identical). If either the base or the addendum is missing, the base is returned as-is."""
    if not reflection_mode():
        return base_prompt
    add = getattr(manifest, "reflection_contract_addendum", None)
    return (base_prompt + add) if (base_prompt and add) else base_prompt


def web_only_enabled() -> bool:
    """Flag (default OFF, Rule 20) via ROSTER_WEB_ONLY: drop the CORPUS retrieval source entirely so the
    web is the ONLY source. Makes answers current/fluid like ChatGPT (no stale-corpus / Wikipedia
    domination) — at the cost of roster's structured/authoritative depth (SEC filings, patents, papers,
    GitHub). Also widens the web leg (max_results 25 vs 8) since the corpus-retrieval latency budget is
    freed. OFF → corpus + web (byte-identical). Reversible with no redeploy (env flag)."""
    return os.environ.get("ROSTER_WEB_ONLY", "").lower() in ("1", "true", "yes")


def people_population_enabled() -> bool:
    """Flag (default OFF, Rule 20) via ROSTER_PEOPLE_POPULATION: route people-DISCOVERY/enumeration
    questions ("find all people where role/function/location…") to the grounded people-index engine
    (answer_people_population) instead of a web-RAG retrieval sample that returns empty. An enumeration
    query is detected by whether the LLM facet-compiler yields a non-empty facet filter; a non-people
    question yields {} and falls through to normal research. OFF → the whole path is skipped (no extra
    LLM call), byte-identical."""
    return os.environ.get("ROSTER_PEOPLE_POPULATION", "").lower() in ("1", "true", "yes")


def people_geo_scope_enabled() -> bool:
    """Flag (default OFF, Rule 20) via ROSTER_PEOPLE_GEO_SCOPE: restrict people searches to ONE country
    (default 'us'), selectable from the top-right of the UI (echoed back so the FE stays in sync). It is
    a HARD filter — a country=<scope> facet is ANDed into every people query, so people we cannot place
    in that country are excluded (honest: we only surface who we can actually locate). A country named
    IN the query (compiler-parsed) overrides the selector default. OFF → no country filter is injected
    (byte-identical to today)."""
    return os.environ.get("ROSTER_PEOPLE_GEO_SCOPE", "").lower() in ("1", "true", "yes")


def jobs_enabled() -> bool:
    """Flag (default OFF, Rule 20) via ROSTER_JOBS: the JOBS MODE — search open roles aggregated from
    public ATS boards (Greenhouse/Ashby/Lever) in `rs_job`, each with an apply link. 404 when off."""
    return os.environ.get("ROSTER_JOBS", "").lower() in ("1", "true", "yes")


def enum_entity_probe_enabled() -> bool:
    """Flag (default OFF, Rule 20) via ROSTER_ENUM_ENTITY_PROBE: for an enumerative "table of the main X"
    ask with no user-named items, the derivation ALSO proposes `probe_entities` (candidate row instances)
    and retrieval fires a TARGETED entity×axis leg per candidate — the fix for a well-covered flagship
    (e.g. Claude Code) being crowded out of axis-only retrieval. Seeds RETRIEVAL only, never rows. OFF →
    the addendum isn't appended (derivation identical) and build_legs stays axis-only (byte-identical)."""
    return os.environ.get("ROSTER_ENUM_ENTITY_PROBE", "").lower() in ("1", "true", "yes")


def _apply_probe_addendum(base_prompt, manifest):
    """When ROSTER_ENUM_ENTITY_PROBE is on, append the vertical's probe-entities addendum to the active
    contract prompt so the ONE derivation call also proposes retrieval-seed candidates. OFF → base
    unchanged (byte-identical). Missing base or addendum → base as-is."""
    if not enum_entity_probe_enabled():
        return base_prompt
    add = getattr(manifest, "probe_entities_contract_addendum", None)
    return (base_prompt + add) if (base_prompt and add) else base_prompt


def explore_legs_enabled() -> bool:
    """Flag (default OFF, Rule 20 — exploratory-legs extension) via ROSTER_EXPLORE_LEGS: when ON
    (and ROSTER_QUESTION_CONTRACT=steer), EXPLORATORY questions' contract axes — 2-4 vertical-derived
    must-cover dimensions (the missed-axes finding: 17%+ of must-cover dimensions absent despite
    usable corpus evidence, because exploratory contracts got no retrieval legs) — are executed as
    AXIS-ONLY retrieval legs (cap 4, k=4 each, concurrent, late-merged post-loop like enumerative
    contract legs; baseline retrieval unchanged). Retrieval only: no slot grid, no coverage gaps,
    no compose-seat reservation for exploratory in this version. OFF → exploratory legs are never
    built; every prompt/behavior byte-identical to today even though the derived contract carries
    axes."""
    return os.environ.get("ROSTER_EXPLORE_LEGS", "").lower() in ("1", "true", "yes")


def landscape_coverage_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_LANDSCAPE_COVERAGE. A "map the landscape / examine all X /
    cluster companies" ask becomes coverage-driven: the app swaps the contract to the vertical's
    enumerative-categories `landscape_contract_prompt`, forces question_contract="steer" + explore_legs
    so the kernel fans retrieval out PER CATEGORY (entity×axis legs) instead of a few narrow searches,
    and appends the market-map compose block. Companies/facts stay strictly grounded; empty categories
    are reported as gaps. OFF → byte-identical (contract stays stance-only exploratory)."""
    return os.environ.get("ROSTER_LANDSCAPE_COVERAGE", "").lower() in ("1", "true", "yes")


def startup_population_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_STARTUP_POPULATION. When ON, a landscape ask
    is answered by AGGREGATING the grounded claim graph — group the company population
    by category in SQL and compose from the compact, per-cell-cited market map
    (`build_market_map` over `ClaimGraphStore.population_claims`) instead of feeding
    raw blocks through the ~30-finding compose cap. Wired into NO route yet (Task 4
    does the compose). OFF → byte-identical to today (no aggregation path taken)."""
    return os.environ.get("ROSTER_STARTUP_POPULATION", "").lower() in ("1", "true", "yes")


def diligence_depth_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_DILIGENCE_DEPTH. When ON, the single-company
    diligence route is exposed at POST /research/diligence — read ONE company's grounded
    claims from the graph, organize them BY DIMENSION (Team/Funding/Traction/Product/
    Market/Moat/Profile), and compose a grounded brief (every cell cited or
    `not_collected`) + grounded assessment. SEPARATE from `_do_research` and
    `/research/population`. OFF → the endpoint 404s (a true no-op, byte-identical)."""
    return os.environ.get("ROSTER_DILIGENCE_DEPTH", "").lower() in ("1", "true", "yes")


def contract_compose_enabled() -> bool:
    """MIGRATION COMPLETE — default ON. ROSTER_CONTRACT_COMPOSE: compose RENDERS the derived question
    contract — the universal VOICE plus the SHAPE the contract asks for (enumerate / decide) — instead of
    the flat golden directive that imposed one fixed shape (which muted enumerative 'build me a table'
    asks). Shape follows the QUESTION, not deployment flags. Validated end-to-end (classifier eval 8/8;
    the muted Q2 now renders a grounded table) + supersedes the (inert-for-tech) ROSTER_ANSWER_MODE_ROUTING
    path. KILL-SWITCH: set ROSTER_CONTRACT_COMPOSE=0 to fall back to the flat golden directive. A vertical
    that supplies no contract_compose_voice no-ops regardless (byte-identical)."""
    return os.environ.get("ROSTER_CONTRACT_COMPOSE", "1").lower() in ("1", "true", "yes")


def alias_resolver_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_ALIAS_RESOLVER. When ON, graph_explore loads the LLM-built
    surface-form alias map from rs_entity_alias and remaps each relationship node key
    (`<kind>:<norm>` → the canonical entity + canonical display name) so aliases of one fund/tech
    (a16z ≈ Andreessen Horowitz) collapse to ONE node. OFF → raw `<kind>:<norm>`, byte-identical."""
    return os.environ.get("ROSTER_ALIAS_RESOLVER", "").lower() in ("1", "true", "yes")


def crossviews_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_CROSSVIEWS. When ON, the CROSSVIEWS surface is
    exposed — the grounded dynamic-table endpoints (`/crossviews/options`,
    `/crossviews/agent`, `/crossviews/build`, `/crossviews/save`). OFF → every one of
    those endpoints 404s (a true no-op; no other path changes)."""
    return os.environ.get("ROSTER_CROSSVIEWS", "").lower() in ("1", "true", "yes")


def answer_mode_routing_enabled() -> bool:
    """Flag (default OFF, Rule 20 — Evidence Contract stage 4) via ROSTER_ANSWER_MODE_ROUTING:
    when ON, an ENUMERATIVE question routes to an enumerative compose framing — the kernel APPENDS
    the vertical's enumerative-compose addendum (per-agent table first, safety cautions beside every
    favorable mention, population studies as context) to the existing compose directive. It fires
    ONLY when the QuestionContract (derived under ROSTER_QUESTION_CONTRACT shadow/steer) says
    enumerative AND ≥2 contract entities hold slot-matched verified claims — the pre-retrieval
    contract alone never routes compose (panel A3). The validated base directive is untouched.
    OFF → every compose prompt byte-identical."""
    return os.environ.get("ROSTER_ANSWER_MODE_ROUTING", "").lower() in ("1", "true", "yes")


def diag_trace_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, each research run captures a troubleshooting trace
    (per-turn steps, tool-call breakdown, the grounding funnel, retries, failures, budget, timing)
    surfaced in the Diagnostics box. Pure bookkeeping over data already in the loop — no extra LLM
    calls. OFF → no trace captured or surfaced (byte-identical)."""
    return os.environ.get("ROSTER_DIAG_TRACE", "").lower() in ("1", "true", "yes")


def reasoning_read_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON (and structured answers are ON), append the vertical's
    reasoning-read directive so compose emits a TYPED interpretation layer (tension/gap/assumption/
    implication/what-would-change) + a 3-dimension confidence read ON TOP of the grounded prose. Each
    item is validated in code — dangling refs and any fabricated number/dose/date/% are dropped — so
    grounding is never loosened. OFF → the directive is unchanged and no interpretation/confidence is
    surfaced (byte-identical)."""
    return os.environ.get("ROSTER_REASONING_READ", "").lower() in ("1", "true", "yes")


def readable_prose_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_READABLE_PROSE appends a plain-language WRITING-STYLE layer to
    compose so answers read like a crisp analyst brief, not a research paper (short sentences, one idea
    each, no em-dash pile-ups). Changes ONLY prose style — never the sections/structure (that's the
    question-driven contract's job), and never the findings, [n] citations, or [[R]] labels. OFF → the
    compose directive is unchanged (byte-identical)."""
    return os.environ.get("ROSTER_READABLE_PROSE", "").lower() in ("1", "true", "yes")


def axis_complete_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_ANSWER_AXES makes compose ADDRESS EACH aspect the reader asked
    about (the derived question-contract axes — product/moat/founders/…) with evidence or an explicit
    one-line gap, and LEAD with a synthesized take (consolidating source-conflicts, trimming gap-lists),
    instead of surveying only what was found. Needs a derived contract with axes; OFF → byte-identical
    (compose directive unchanged). Fixes 'good findings, useless survey answer' + silently-dropped aspects."""
    return os.environ.get("ROSTER_ANSWER_AXES", "").lower() in ("1", "true", "yes")


def tech_synthesis_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_TECH_SYNTHESIS adds a strategic 'How it works' TECHNICAL
    SYNTHESIS to the answer when the subject is a product/technology/tech company — the core technical
    building blocks (disclosed → cited [n]), the end-to-end user flow, and the LIKELY design where the
    findings don't disclose it (clearly labeled, wrapped [[R]]...[[/R]], no invented proprietary specifics),
    tied to strategy/moat. The addendum self-skips for a non-technical subject; OFF → byte-identical."""
    return os.environ.get("ROSTER_TECH_SYNTHESIS", "").lower() in ("1", "true", "yes")


def deep_synthesis_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_DEEP_SYNTHESIS makes a non-lookup answer a synthesis-first
    grounded analysis — core thesis (the non-obvious read) → tensions/contradictions → second-order
    implications → mechanism — built around grounded derivations woven into the spine, with the deep
    read held to the same grounding contract as the claims list. Lookups stay crisp. OFF → byte-identical
    (today's compose path). T1 wires the flag → service field only; T2/T3 add the behavior."""
    return os.environ.get("ROSTER_DEEP_SYNTHESIS", "").lower() in ("1", "true", "yes")


def parametric_led_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_PARAMETRIC_LED lets the model's integrated knowledge LEAD a
    parametric-eligible answer's structure + reasoning while retrieval VALIDATES every asserted fact.
    When ON AND the question is parametric-eligible (stance=established, kind∈{understanding,management},
    subject_kind!=specific_entity), a pre-retrieval PriorDraft is produced. T1 wires the flag → service
    field + produces the draft, threading it INERTLY (unused by compose until T2/T3). OFF or not
    eligible → no draft call, today's retrieve-first path byte-identical."""
    return os.environ.get("ROSTER_PARAMETRIC_LED", "").lower() in ("1", "true", "yes")


def intelligence_core_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_INTELLIGENCE_CORE makes the model OWN the inquiry on an
    eligible question — it drafts competing HYPOTHESES + a short analytical FRAME, and (T2) retrieval
    tests each hypothesis with FOR-and-AGAINST searches (adversarial disconfirmation, the one thing
    missing today). When ON AND the question is eligible (stance=established, kind∈{understanding,
    management}, subject_kind!=specific_entity), a pre-retrieval IntelligenceDraft is produced and, if
    it parses to >=2 competing hypotheses, threaded INERTLY into run_react (unused by compose until
    T2/T3 consume it). Facts stay retrieval-authored (span-gate untouched). OFF or not eligible / <2
    hypotheses → no draft threading, today's retrieval-led path byte-identical."""
    return os.environ.get("ROSTER_INTELLIGENCE_CORE", "").lower() in ("1", "true", "yes")


def golden_answer_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_GOLDEN_ANSWER collapses the eight stacked answer-shaping layers
    (deep-synthesis, axes, tech-synthesis, intelligence-core, parametric, derive, derive-ideas,
    reasoning-read, readable-prose, authority-basis, answer-profiles) into ONE golden compose directive —
    the vertical's `golden_answer_directive`, wired as `answer_format` with every other layer forced OFF.
    The answer becomes one clean freeform brief: the answer only, no narrated scaffolding (no hypotheses/
    frames/'reasoning & ideas'/cruxes/confidence meta, and the UI reasoning panel is hidden). The upstream
    EVIDENCE machinery that makes answers better stays ON and invisible (adversarial retrieval, authority
    ranking, freshness, span-gate); the two PROSE grounding audits are re-bound to this flag so the freer
    prose is still policed. OFF → the stacked path is byte-identical to today."""
    return os.environ.get("ROSTER_GOLDEN_ANSWER", "").lower() in ("1", "true", "yes")


def answer_layout_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_ANSWER_LAYOUT runs a grounding-safe PRESENTATION pass after the
    answer is composed + span-gated — a dedicated second LLM call that reflows the final text into a
    scannable whiteboard layout (short paragraphs, bullets, tables, arrow-flows) WITHOUT changing facts or
    citations. Fail-closed in code: the reflow is discarded (original kept) unless every [n] citation is
    preserved and no new hard token appears. Fixes wall-of-text where the single compose pass can't. OFF →
    the pass never runs → composed answer byte-identical."""
    return os.environ.get("ROSTER_ANSWER_LAYOUT", "").lower() in ("1", "true", "yes")


def entity_open_web_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_WEB_ENTITY_OPEN fires one entity-scoped, quality-screened
    open-web Exa probe (whitelist dropped) on step 0 for single-entity diligence questions. OFF →
    no extra Exa/LLM call, leg set byte-identical."""
    return os.environ.get("ROSTER_WEB_ENTITY_OPEN", "").lower() in ("1", "true", "yes")


def deep_company_reader_enabled() -> bool:
    """Flag (default OFF): ROSTER_DEEP_COMPANY_READER adds a bounded facet-targeted web dossier leg on
    step 0 for single-company diligence questions. OFF → no extra retrieval leg."""
    return os.environ.get("ROSTER_DEEP_COMPANY_READER", "").lower() in ("1", "true", "yes")


def deep_people_reader_enabled() -> bool:
    """Flag (default OFF): ROSTER_DEEP_PEOPLE_READER adds a bounded facet-targeted web dossier leg on
    step 0 for single-person diligence questions. OFF → no extra retrieval leg."""
    return os.environ.get("ROSTER_DEEP_PEOPLE_READER", "").lower() in ("1", "true", "yes")


def open_web_denoise_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_WEB_OPEN_DENOISE opens the aux web (Exa) leg to the FULL web
    (whitelist demoted to a ranking boost) for every web-eligible question and admits its hits through
    the denoising funnel (structural + cosine floor + authority boost + LLM screen, fail-safe to the
    authoritative subset). OFF → whitelisted Exa + DDG exactly as today (leg set + rerank byte-identical)."""
    return os.environ.get("ROSTER_WEB_OPEN_DENOISE", "").lower() in ("1", "true", "yes")


def derive_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_DERIVE runs the grounded-reasoning GATE — after the fact gate,
    derive labeled conclusions (inference/hypothesis/speculation) FROM the verified findings, each with a
    finding basis and a falsifier, pruned to the few that matter. A second trust regime: facts are
    source-provenanced, reasoning is premise-provenanced + validity-checked. OFF → no derivations
    (byte-identical)."""
    return os.environ.get("ROSTER_DERIVE", "").lower() in ("1", "true", "yes")


def derive_ideas_enabled() -> bool:
    """Flag (default OFF): ROSTER_DERIVE_IDEAS also generates grounded 'opportunity' derivations
    (whitespace / second-order implications) — the brainstorming surface. Rides ROSTER_DERIVE."""
    return os.environ.get("ROSTER_DERIVE_IDEAS", "").lower() in ("1", "true", "yes")


def cross_family_judge_enabled() -> bool:
    """Flag (default OFF, Rule 20): ROSTER_CROSS_FAMILY_JUDGE wires a DIFFERENT-family (OpenAI) model as
    `derive_judge_llm` — activating the cross-family GROUNDING GATE and making derive's validity judge
    cross-family. When ON *and* OPENAI_API_KEY is set, build_default_service constructs an
    OpenAILLMClient and passes it as derive_judge_llm; OFF or no key → derive_judge_llm=None (today's
    behavior: grounding gate + derive judge fail-closed / same-family). OFF → byte-identical."""
    return os.environ.get("ROSTER_CROSS_FAMILY_JUDGE", "").lower() in ("1", "true", "yes")


def answer_focus_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, elliptical conversational follow-ups are condensed into a
    self-contained question (so retrieval + compose inherit the subject) AND compose ANSWERS the
    question / scopes to its subject instead of compiling every retrieved finding. Needs conversation
    context for the condense half; the compose-scope half also improves single-turn. OFF →
    byte-identical (no condense call, original compose instruction)."""
    return os.environ.get("ROSTER_ANSWER_FOCUS", "").lower() in ("1", "true", "yes")


def followup_clarify_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON (and answer-focus on, with history), a genuinely AMBIGUOUS
    follow-up returns a short CLARIFYING question instead of guessing/dumping (factra's CM pattern).
    OFF → the resolver never asks; it always returns a best-guess standalone question."""
    return os.environ.get("ROSTER_FOLLOWUP_CLARIFY", "").lower() in ("1", "true", "yes")


def _resolve_audience(audience: str | None) -> str:
    """The audience actually used: 'patient' only when the flag is on AND explicitly requested;
    everything else → 'clinician' (the default, byte-identical path)."""
    if patient_mode_enabled() and (audience or "").lower() == "patient":
        return "patient"
    return "clinician"


def conversation_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, answers become a multi-turn thread — follow-up
    questions carry prior turns as context, the thread persists on one session, and suggested
    follow-ups are offered. OFF → single-answer behavior (each ask is a fresh session)."""
    return os.environ.get("ROSTER_CONVERSATION", "").lower() in ("1", "true", "yes")


def focus_deepen_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, the FE lets a user select a span of an answer and
    push focus onto it — 'Go deeper' (expand that specific area with fresh focused retrieval) or
    'Rethink' (critically re-examine that claim with grounded counter-evidence/caveats). Served by
    POST /research/focus, which 404s when OFF. OFF → no endpoint, no FE popover (byte-identical)."""
    return os.environ.get("ROSTER_FOCUS_DEEPEN", "").lower() in ("1", "true", "yes")


def query_expansion_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, a terse question is EXPANDED before retrieval — one LLM
    call adds a coverage brief (key aspects + search keywords) to the query so retrieval covers what a
    complete answer needs, regardless of how tersely the user phrased it. Steers planner + embedding,
    never adds facts (grounding gate unchanged); the pristine question stays as graph_question. OFF →
    byte-identical (no expansion call, raw question)."""
    return os.environ.get("ROSTER_QUERY_EXPANSION", "").lower() in ("1", "true", "yes")


def related_research_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, an answer carries a 'Top related public research'
    section — a SEPARATE facet-filtered semantic search over papers/preprints/filings, dedup'd,
    quality-ranked (structural), honest-omit when nothing clears the bar. OFF → empty list, no
    section, no extra retrieval (byte-identical)."""
    return os.environ.get("ROSTER_RELATED_RESEARCH", "").lower() in ("1", "true", "yes")


def company_links_enabled() -> bool:
    """Flag (default OFF, Rule 20): when ON, an answer carries a `companies` map of the companies
    it is GROUNDED on (subjects of its own company-source claims) → their canonical page + Roster
    entity page, and the FE hyperlinks the first exact mention of each in the prose. Precision-safe
    (Rule 18): only companies with a known entity are linked — never arbitrary name matching. Also
    ungates GET /entity/{id}. OFF → empty map, no links, no entity page (byte-identical)."""
    return os.environ.get("ROSTER_COMPANY_LINKS", "").lower() in ("1", "true", "yes")


# Corpus sources whose documents ARE a single company and whose document_title IS that company's
# name (structural — Rule 18: no semantic guess). EDGAR is excluded: its title is a filing, not a
# clean company name.
_COMPANY_SOURCES = ("yc", "wikidata", "companies_house")


class Attachment(BaseModel):
    data: str                              # base64-encoded file bytes
    media_type: str = ""                   # e.g. image/png, application/pdf, application/dicom
    name: str = ""


class ResearchIn(BaseModel):
    question: str
    tenant_id: str
    workspace_id: str | None = None
    sources: list[str] | None = None      # subset of source keys; None = all
    attachments: list[Attachment] | None = None   # images/PDF/DICOM → vision context
    user_name: str | None = None          # asker identity (captured at landing)
    user_email: str | None = None
    engine: str = ""                      # "" = auto (reasoned-default setting decides) · "standard" · "reasoned"
    integrative: bool = False             # per-question opt-in: complementary/integrative section (flag-gated)
    history: list[dict] | None = None     # prior turns [{question, answer}] → follow-up context
    session_id: str | None = None         # thread to append this turn to (conversation)
    intake_transcript: list[dict] | None = None  # Guided-intake conversation [{role,text}] → saved for admin audit
    effort: float = Field(default=1.0, ge=1.0, le=2.5)   # effort multiplier; ignored unless flag on
    audience: str = "clinician"           # "clinician" (default) | "patient"; ignored unless flag on
    mode: str = ""                        # analytical lens, e.g. "acquirer" (M&A); "" = default investor lens
    company: str = ""                     # single-company DILIGENCE subject (name / entity_id); used only by /research/diligence
    country: str = ""                     # people geo-scope from the top-right selector (default 'us' when the flag is on)


class FocusIn(BaseModel):
    """A 'push focus onto a selected span' request (POST /research/focus, flag-gated). The user
    highlighted `span` inside a prior answer to `question` and wants it deepened or re-examined."""
    question: str                          # the ORIGINAL question that produced the answer (context)
    span: str                              # the highlighted answer text to focus on
    mode: str = "deeper"                   # "deeper" (expand w/ fresh retrieval) | "rethink" (critically re-examine)
    tenant_id: str = "demo"
    workspace_id: str | None = None
    sources: list[str] | None = None       # optional source-key subset; None = all


class PanelIn(BaseModel):
    question: str                          # the clinician's issue / condition description
    tenant_id: str = "demo"
    workspace_id: str | None = None
    specialists: list[str] | None = None   # specialist ids to convene; None = the default panel
    sources: list[str] | None = None
    history: list[dict] | None = None      # prior panel turns [{question, answer, claims}] for a follow-up
    session_id: str | None = None          # the panel thread this turn continues (echoed back)
    rationales: dict | None = None         # {specialist_id: why-selected} from triage, shown per specialist
    attachments: list[Attachment] | None = None   # images/PDF/DICOM/pasted text → shared panel context


class CrossviewAgentIn(BaseModel):
    row_kind: str = "company"                 # company | person | category
    goal: str = ""                            # what the user wants the table to show
    current_columns: list[str] | None = None  # columns already picked (ids)
    message: str | None = None                # follow-up chat message
    category_norm: str | None = None          # optional company-population filter
    tenant_id: str = "demo"


class CrossviewBuildIn(BaseModel):
    spec: dict                                # a CrossviewSpec-dict (row_kind/columns/filters/…)
    tenant_id: str = "demo"


class CrossviewSaveIn(BaseModel):
    spec: dict                                # the source-of-truth spec
    grid: dict | None = None                  # a snapshot grid (fallback on reopen)
    transcript: list[dict] | None = None      # the design conversation
    title: str | None = None
    tenant_id: str = "demo"
    workspace_id: str | None = None


class SuggestIn(BaseModel):
    question: str
    answer: str = ""
    history: list[dict] | None = None


class RefineIn(BaseModel):
    question: str


class TriageIn(BaseModel):
    # The running intake transcript, oldest-first: [{role: "user"|"assistant", text}]. The FE holds it
    # (stateless server) and appends each turn. The last item is the user's latest message.
    transcript: list[dict] = []
    tenant_id: str = "demo"
    # v2: the LAST assistant turn's echoed register, posted back by the FE. Trusted ONLY for ask-cap
    # selection (never passed to the model); absent under v2 → the case cap applies (fail-open).
    register: str = ""
    # The USER's explicit "wrap up & search now" — forces this turn to route (works under v1 too).
    wrap_up: bool = False


class RegisterIn(BaseModel):
    email: str
    password: str = ""              # >=12 chars (enforced server-side); '' = legacy token-only register
    name: str = ""                  # optional; defaults to the email local-part when blank
    profession: str = ""            # self-declared (Physician / NP-PA / Pharmacist / Student / …)
    country: str = ""
    npi: str = ""                   # optional (US) — structurally verified against the CMS registry
    disclaimer_ack: bool = False    # the attestation from the identity gate


class LoginIn(BaseModel):
    email: str
    password: str


class BucketIn(BaseModel):
    name: str


class BucketItemIn(BaseModel):
    kind: str                       # 'person' | 'job'
    ref_id: str
    label: str = ""
    payload: dict | None = None


class SavedSearchIn(BaseModel):
    query: str
    mode: str = "research"


class ProfileIn(BaseModel):
    profile: dict                   # the superset candidate profile (free-form; FE defines the fields)


class ResumeIn(BaseModel):
    name: str
    content_type: str = "application/octet-stream"
    data_b64: str                   # base64-encoded file bytes (avoids a multipart dependency)


class SettingIn(BaseModel):
    key: str
    value: str = ""     # "on" | "off" | "" (empty = follow the env default)


class FeedbackIn(BaseModel):
    session_id: str = ""
    turn_index: int = 0
    verdict: str                    # "up" | "down" | "flag"
    modes: list[str] = []           # W1–W9 codes (shared warrant taxonomy)
    claim_index: int | None = None  # 1-based finding a flag points at, when specific
    note: str = ""
    question: str = ""              # echo of the question for a self-contained feedback row


class ExplainIn(BaseModel):
    question: str
    answer: str
    session_id: str | None = None


class TermsIn(BaseModel):
    question: str = ""
    answer: str
    session_id: str | None = None
    turn_index: int | None = None    # which thread turn this answer is (per-turn persistence, like visuals)


class TermLookupIn(BaseModel):
    term: str
    context: str = ""    # the term this one was linked FROM (helps disambiguate)


class VisualsIn(BaseModel):
    question: str = ""
    answer: str
    session_id: str | None = None
    turn_index: int | None = None    # which thread turn this answer is (per-turn persistence)


class VoiceTtsIn(BaseModel):
    text: str


class GapPlanIn(BaseModel):
    question: str
    answer: str = ""
    coverage_gaps: list[str] = []


class GapQueueIn(BaseModel):
    question: str = ""
    tenant_id: str = "demo"
    jobs: list[dict] = []                  # [{connector, query, limit, kind, rationale, quality}]


class CorpusIngestIn(BaseModel):
    """Bulk prod-direct ingest — the replacement for local download + push. Enqueues connector
    jobs into the corpus queue that the prod processor drains straight into the prod corpus."""
    jobs: list[dict] = []                  # explicit passthrough {connector, query, limit, facets, ...}
    source_country: str = ""               # optional per-batch facet stamp on every ingested block


class PdfIngestIn(BaseModel):
    """Local→prod full-text bridge payload: PDF bytes downloaded on a good-IP box, shipped for prod
    docling + ingest. Each doc: {native_id, title, facets?, pdf_b64}. source_key defaults to arxiv so
    the ingest REPLACES (not duplicates) the matching arxiv:{native_id} doc via clean-replace."""
    docs: list[dict] = []
    source_key: str = "arxiv"


class CorpusSearchIn(BaseModel):
    """Corpus-explorer pure-retrieval query (admin)."""
    query: str = ""
    source: str = ""          # exact source_key filter (edgar/github/openalex/…); "" = all
    source_kind: str = ""     # facet filter (filing/code/paper/news/…); "" = all
    k: int = 25


class DiscoverIn(BaseModel):
    """Discovery / sourcing: 'which companies are working on X' over the corpus."""
    query: str
    tenant_id: str = "demo"
    workspace_id: str | None = None
    limit: int = 20                        # how many target companies to return
    pool: int = 150                        # retrieval pool size before entity aggregation
    sources: list[str] | None = None       # restrict to certain corpus sources (e.g. ["edgar","github"])


class PulseEventIn(BaseModel):
    """Evidence Pulse admin action: approve (apply stamps) or retract (undo a mistake) an event."""
    event_id: str
    action: str          # approve | retract


class WatchIn(BaseModel):
    topic: str
    source: str = "manual"     # manual (free text → canonicalized) | suggested (already canonical)


class SeenIn(BaseModel):
    event_id: str


class GraphEdgeIn(BaseModel):
    """Graph admin action: activate | demote one edge (the one-click reversal path)."""
    edge_id: str
    action: str          # activate | demote


class TopicsIn(BaseModel):
    question: str = ""
    answer: str = ""


class PatientFlagIn(BaseModel):
    real_patient: bool = True


class Citation(BaseModel):
    text: str
    quote: str
    atom_id: str
    source: str = ""
    title: str = ""
    url: str | None = None           # canonical source page (opens in a new tab)
    document_id: str = ""
    source_kind: str = ""            # structural source_kind facet (filing/paper/news/code/…) — FE quality badge
    tier: str = ""                   # classified evidence tier (evidence_kind) — colors the FE quality badge


class ResearchOut(BaseModel):
    grounded: bool
    answer: str = ""                 # synthesized prose answer, grounded in findings
    people_rows: list = []           # people-enumeration rows (empty unless ROSTER_PEOPLE_POPULATION routed here)
    coverage_basis: dict | None = None  # honest coverage facts for a people-enumeration answer (else None)
    claims: list[Citation]           # the verified findings (evidence for the answer)
    coverage_gaps: list[str]
    rejected: int
    source_stats: dict = {}          # source -> {retrieved, cited}
    degraded_sources: dict = {}      # sources that failed this request
    session_id: str | None = None    # saved Q&A id (for history + linking a video)
    stopped_reason: str = ""         # answered | budget | max_steps (observability)
    atoms_gathered: int = 0          # evidence blocks the agent saw (observability)
    retried_empty: bool = False      # the abstention-recovery re-ask fired (observability)
    visual_observation: str = ""     # labeled AI image description (context, NOT a finding)
    attachment_notes: list[str] = [] # anything skipped when reading attachments
    effort: float | None = None      # resolved effort multiplier (only set when the flag is on)
    audience: str | None = None      # resolved audience 'clinician'|'patient' (only set when flag on)
    resolved_question: str | None = None  # condensed follow-up question, if it differed (flag on only)
    clarification: str | None = None      # a clarifying question when the follow-up was ambiguous
    derived_from_prior: bool = False      # answer is a reshape of the previous answer (no new evidence)
    charts: list = []                     # validated grounded bar charts (empty unless the flag is on)
    interpretation: list = []             # validated reasoning-read factors (empty unless the flag is on)
    confidence: dict | None = None        # 3-dimension confidence read (None unless the flag is on)
    reasoning_purpose: str = ""           # the decision the reasoning serves (empty unless the flag is on)
    reasoning_conclusion: str = ""        # the informed judgment toward that purpose (flag on only)
    derivations: list = []                # gated labeled derivations {label,kind,conclusion,basis,
    #                                       falsifier} (empty unless ROSTER_DERIVE is on) — the audit view
    diagnostics: dict | None = None       # troubleshooting trace (None unless the diag-trace flag is on)
    question_contract: dict | None = None # the derived contract that shaped the answer {mode,entities,axes,
    #                                       stance} — observability so the answer SHAPE decision is visible
    web_providers: dict = {}              # web search-source attribution: provider -> {retrieved, cited,
    #                                       unique_cited} — which engine (exa/brave/…) surfaced cited evidence
    freshness: dict | None = None         # {as_of,newest_year,oldest_year,n_dated,n_total,stale_warning}
    #                                       (None unless the freshness-ranking flag is on)
    related_research: list = []           # top related public research [{title,url,kind,venue,year,
    #                                       citations,peer_reviewed}] (empty unless the flag is on)
    companies: list = []                  # companies this answer is grounded on [{name,entity_id,url,
    #                                       roster_url}] for prose hyperlinking (empty unless flag on)
    people: list = []                     # people profile links [{name,url,host}]
    reflection: dict = {}                 # ROSTER_REFLECTION=steer: {intent, answer_brief, confidence} — what
    #                                       the pass understood the user is really after (empty when off/low-conf)
    unverified_priors: list = []          # ROSTER_PARAMETRIC_LED (T3): the model's OWN asserted facts that
    #                                       retrieval could NOT ground [{text,needs_freshness}] — the
    #                                       labeled register, kept OUT of `claims`/grounded prose (empty
    #                                       unless the parametric-led flag drove this run)


def build_default_service() -> ResearchService:
    """Assemble the service from the active vertical + env providers.

    NOTE: the corpus source's embedding dimension must match the query embedder;
    in production the corpus is Postgres-backed with OpenAI embeddings (1536) and
    the query embedder matches. Deployment wiring finalizes this alignment.
    """
    manifest = load_active_vertical()
    mode = resolve_mode()
    embedder = build_embedder(mode=mode)
    dsn = os.environ.get("ROSTER_CORPUS_DSN")

    sources: dict = {}
    connectors: dict = {}
    corpus_key = ""
    _web_only = web_only_enabled()
    if dsn and not _web_only:
        # Real pgvector corpus (empty until POST /ingest). One pg source, registered
        # under the vertical's corpus source key so gating/covers still align.
        covers = next((s.covers() for s in manifest.retrieval_sources.values()
                       if hasattr(s, "covers")), {})
        pg = PostgresRetrievalSource(dsn, dim=embedder.dim, table="rs_block", covers=covers,
                                     currency_demote=pulse_enabled())
        corpus_key = next(iter(manifest.retrieval_sources), "corpus")
        sources[corpus_key] = pg
        connectors = dict(manifest.connectors)
    elif not _web_only:
        sources = dict(manifest.retrieval_sources)      # fixture (in-memory) corpus
    # ROSTER_WEB_ONLY: drop the corpus (both real + fixture) → web is the ONLY retrieval source. The
    # answer becomes ChatGPT-like (current, fluid) at the cost of roster's structured/authoritative depth
    # (filings/patents/papers). Widen web breadth here since the corpus-retrieval latency budget is freed
    # (25 default vs 8) so the web leg has ChatGPT-grade reach. OFF → byte-identical (corpus + web=8).
    _web_max = int(os.environ.get("ROSTER_WEB_MAX_RESULTS", "35" if _web_only else "8"))
    sources["web"] = WebRetrievalSource(
        build_web(mode=mode, domains=getattr(manifest, "web_domains", ()),
                  recent=(freshness_ranking_enabled() or answer_contract_enabled() or _web_only)),
        # results per web query (env ROSTER_WEB_MAX_RESULTS). 8 with corpus (a 12 widening × 2 providers
        # × many legs regressed latency); web-only frees that budget → default 25 for ChatGPT-grade reach.
        max_results=_web_max,
        # venue-authority facets + the corpus embedder: web evidence gets graded and reranked
        # by the same machinery as corpus evidence (authority tiers, recency, query relevance).
        domain_facets=getattr(manifest, "web_domain_facets", None),
        # In WEB-ONLY mode there is no corpus, and a DeepSeek-only deployment has NO embeddings
        # provider (DeepSeek has no embeddings API). Pass embedder=None so the web source skips the
        # cosine rerank (WebRetrievalSource falls back to provider rank) instead of crashing on a
        # missing OPENAI_API_KEY. With a corpus (not web-only) the real embedder is still used.
        embedder=(None if _web_only else embedder))

    persona = manifest.persona.system_prompt() if manifest.persona else \
        "You are an evidence-grounded research agent."
    # Flag-gated (Rule 20): only pass the vertical's answer-structure directive when ON.
    # OFF → None → the kernel's flat-prose compose path, byte-identical to pre-flag. When the
    # separate clinical-synthesis flag is ALSO on, swap in the sharper directive (A/B seam);
    # falls back to the base format if the vertical doesn't supply one.
    # ROSTER_GOLDEN_ANSWER: engages only with structured answers + a vertical golden directive to swap in.
    # When ON it REPLACES answer_format with the single golden directive and suppresses every append below
    # (and, further down, forces the eight layer flags OFF). OFF → _golden is False → byte-identical.
    # ROSTER_CONTRACT_COMPOSE (voice ⟂ shape): the successor to golden — compose renders the derived
    # contract (VOICE + the SHAPE for its mode) instead of the flat golden directive. It reuses golden's
    # layer-OFF discipline (the voice IS the golden voice; every other shaping layer stays off), so it
    # sets `_golden` too; the actual directive is built in run_react from the contract, which overrides
    # answer_format there. Requires the vertical to supply the voice.
    _cc_compose = (contract_compose_enabled() and structured_answers()
                   and bool(getattr(manifest, "contract_compose_voice", None)))
    _golden = (_cc_compose or (golden_answer_enabled() and bool(getattr(manifest, "golden_answer_directive", None)))) \
        and structured_answers()
    if structured_answers():
        answer_format = manifest.answer_format
        if clinical_synthesis():
            answer_format = getattr(manifest, "clinical_answer_format", None) or manifest.answer_format
        if _golden:
            answer_format = manifest.golden_answer_directive
    else:
        answer_format = None
    # Visualization guidance (flag): append to the CLINICIAN directive so answers use tables/rankings/
    # pros-cons from the verified findings. Only when structured answers are on (tables render then).
    if answer_format and not _golden and answer_visuals_enabled() and getattr(manifest, "visual_guidance", None):
        answer_format = answer_format + "\n\n" + manifest.visual_guidance
    # Chart emission (flag): compose may populate a grounded bar chart, validated in the kernel.
    if answer_format and not _golden and answer_charts_enabled() and getattr(manifest, "chart_guidance", None):
        answer_format = answer_format + "\n\n" + manifest.chart_guidance
    # Reasoning Read (flag): append the interpretation-layer directive so compose emits typed
    # interpretation + a confidence read (both validated in the kernel). Requires structured answers.
    if answer_format and not _golden and reasoning_read_enabled() and getattr(manifest, "reasoning_format", None):
        answer_format = answer_format + "\n\n" + manifest.reasoning_format
    vision_prompt = manifest.vision_prompt if vision_enabled() else None
    report_prompt = getattr(manifest, "report_prompt", None) if vision_enabled() else None
    gap_prompt = manifest.gap_prompt if gap_healing_enabled() else None
    suggest_prompt = manifest.suggest_prompt if conversation_enabled() else None
    refine_prompt = getattr(manifest, "refine_prompt", None) if refine_enabled() else None
    # Prompts are wired UNCONDITIONALLY so the live admin toggles (duel/triage) work without a
    # redeploy — an unused prompt is inert; gating happens at request time on the live flag.
    triage_prompt = getattr(manifest, "triage_prompt", None)
    triage_prompt_v2 = getattr(manifest, "triage_prompt_v2", None)
    reasoned_scaffold = getattr(manifest, "reasoned_scaffold_prompt", None)
    reasoned_format = getattr(manifest, "reasoned_answer_format", None)
    # ROSTER_GOLDEN_ANSWER: the reasoned / understanding / deep engines compose from their OWN format
    # slots, NOT `answer_format` — and the reasoned engine is the default path for most questions. So
    # golden must swap THOSE slots too, or reasoned-routed answers keep the old structured format (the
    # wiring gap that let report sections + [[R]] leak through on a "golden" answer). Point all compose
    # bases at the single golden directive. OFF → _golden False → each keeps its manifest value.
    _understanding_fmt = getattr(manifest, "understanding_answer_format", None)
    _deep_fmt = getattr(manifest, "deep_answer_format", None)
    if _golden:
        reasoned_format = manifest.golden_answer_directive
        _understanding_fmt = manifest.golden_answer_directive
        _deep_fmt = manifest.golden_answer_directive
    # Use the BEST model for EVERY research step (planning + claim extraction + compose). A cheaper
    # planner (haiku) paraphrased quotes → span-verification rejected them (grounding regression),
    # so planner_llm is left unset and run_react uses `llm` throughout. Optional explicit override.
    planner_model = os.environ.get("ROSTER_PLANNER_MODEL", "")   # empty → same strong model as compose
    planner_llm = build_llm(mode=mode, model=planner_model) if planner_model else None
    claims_first = os.environ.get("ROSTER_CLAIMS_FIRST", "").lower() in ("1", "true", "yes")
    # Evidence selection (flag, default OFF): raise the extractor's per-atom window so full-text
    # effect-size/CI sentences aren't truncated, AND keep the claims most RELEVANT to the question
    # for compose (not the first-come 30). Both are provenance-safe (span+entail gates unchanged).
    evidence_select = os.environ.get("ROSTER_EVIDENCE_SELECT", "").lower() in ("1", "true", "yes")
    atom_cap = int(os.environ.get("ROSTER_ATOM_CAP", "6000" if evidence_select else "1600"))
    # Patient directive (per-request by audience). Reasoning Read (flag): append the PATIENT-facing
    # reasoning directive so patient answers get the same purpose→factors→judgment→confidence arc in
    # plain language (same structured fields + code validation as the clinician path).
    patient_directive = manifest.patient_answer_format if patient_mode_enabled() else None
    if patient_directive and reasoning_read_enabled() and getattr(manifest, "patient_reasoning_format", None):
        patient_directive = patient_directive + "\n\n" + manifest.patient_reasoning_format
    # Cross-family judge (flag, default OFF — Rule 20). When ON *and* a key is present, wire a
    # DIFFERENT-family (OpenAI) model as derive_judge_llm — this activates the cross-family grounding
    # gate and makes derive's validity judge cross-family. OFF or no key → None (today's behavior:
    # grounding gate + derive judge fail-closed / same-family), byte-identical.
    derive_judge_llm = None
    if cross_family_judge_enabled() and os.environ.get("OPENAI_API_KEY"):
        from roster_kernel.providers.openai_client import OpenAILLMClient
        derive_judge_llm = OpenAILLMClient()
    # ROSTER_ANSWER_LAYOUT cost control: the reflow is mechanical reformatting (code-guarded for grounding).
    # Only build a SEPARATE layout model when ROSTER_LAYOUT_MODEL is explicitly set (e.g. a cheap model when
    # the main model is expensive). UNSET → layout_llm stays None → the runtime uses the MAIN model, which
    # is already the cheap one (DeepSeek/Sonnet). This avoids a hardcoded Anthropic-Haiku dependency that
    # would burn separate credits and break when the main provider is DeepSeek and Anthropic is unfunded.
    layout_llm = None
    _layout_model = os.environ.get("ROSTER_LAYOUT_MODEL", "").strip()
    if answer_layout_enabled() and _layout_model:
        try:
            layout_llm = build_llm(mode=mode, model=_layout_model)
        except Exception:
            layout_llm = None
    # The deep readers are RETRIEVAL (evidence-gathering), NOT answer-shaping — golden collapses the
    # compose STACK but WANTS rich grounded material to synthesize from, so it must KEEP the deep readers
    # (like it keeps adversarial retrieval + authority ranking). Do NOT gate these on `not _golden`.
    _deep_company_reader = deep_company_reader_enabled()
    _deep_people_reader = deep_people_reader_enabled()
    return ResearchService(
        llm=build_llm(mode=mode), embedder=embedder, planner_llm=planner_llm,
        graph_expander=_make_graph_expander(),
        claims_first=claims_first, extraction_lenses=getattr(manifest, "extraction_lenses", ()),
        evidence_select=evidence_select, atom_cap=atom_cap,
        # ROSTER_GOLDEN_ANSWER: force every answer-shaping layer OFF so the golden directive is the SOLE
        # compose base — the directive addenda, the inline flag-instructions, and all four post-compose
        # section appends then no-op automatically (each is byte-identical when its flag/data is falsy).
        # `_golden` is False when the flag is off → every layer keeps its own value → byte-identical.
        golden_answer=_golden,
        answer_layout=answer_layout_enabled(),   # ROSTER_ANSWER_LAYOUT: grounding-safe scannability reflow
        layout_llm=layout_llm,                    # cheap model for the reflow (cost control)
        reasoning_read=reasoning_read_enabled() and not _golden,
        readable_prose=readable_prose_enabled() and not _golden,
        axis_complete=axis_complete_enabled() and not _golden,
        tech_synthesis=tech_synthesis_enabled() and not _golden,
        # ROSTER_DEEP_SYNTHESIS (T1): flag → service field + the vertical's deep format as inert data.
        # Routing (letting deep ride the reasoned/dynamic path for an unset engine) is deferred to T3;
        # T1 only wires the flag so OFF and ON stay byte-identical today.
        deep_synthesis=deep_synthesis_enabled() and not _golden,
        deep_answer_format=_deep_fmt,
        # ROSTER_PARAMETRIC_LED (T1): flag → service field + the vertical's prior-draft prompt as inert
        # data. When ON + parametric-eligible, ask_reasoned produces a PriorDraft and threads it inertly
        # (unused by compose until T2/T3). OFF or not eligible → byte-identical.
        parametric_led=parametric_led_enabled() and not _golden,
        prior_draft_prompt=getattr(manifest, "prior_draft_prompt", None),
        # ROSTER_INTELLIGENCE_CORE (T1): flag → service field + the vertical's intelligence-draft prompt as
        # inert data. When ON + eligible, ask_reasoned drafts competing hypotheses + a frame and threads
        # them inertly (unused by compose until T2/T3). OFF or not eligible → byte-identical.
        intelligence_core=intelligence_core_enabled() and not _golden,
        intelligence_draft_prompt=getattr(manifest, "intelligence_draft_prompt", None),
        deep_company=_deep_company_reader,
        company_reader=getattr(manifest, "company_reader", None),
        deep_person=_deep_people_reader,
        person_reader=getattr(manifest, "person_reader", None),
        entity_open_web=entity_open_web_enabled(),
        web_open_denoise=open_web_denoise_enabled(),
        web_quality_prompt=getattr(manifest, "web_quality_prompt", None),
        derive=derive_enabled() and not _golden,
        derive_ideas=derive_ideas_enabled() and not _golden,
        # derive_judge_llm is KEPT under golden — it powers the cross-family semantic grounding gate
        # (re-bound to golden in run_react), which is evidence policing, not answer-shaping.
        derive_judge_llm=derive_judge_llm,
        collect_diagnostics=diag_trace_enabled(),
        classify_evidence=getattr(manifest, "evidence_classifier", None),
        evidence_fitness=evidence_fitness_enabled(),
        # ROSTER_AUTHORITY_BASIS (T1/T2): unconditional low-basis partition + the compose floor directive.
        # The directive is inert vertical data (always threaded); the flag gates whether it's appended.
        # Golden drops the authority COMPOSE DIRECTIVE only; the authority RANKING (evidence_ranker below)
        # stays ON — the golden prompt states the strong-evidence rule itself, and ranking is invisible.
        authority_basis=authority_basis_enabled() and not _golden,
        authority_basis_directive=getattr(manifest, "authority_basis_directive", None),
        evidence_ranker=getattr(getattr(manifest, "authority_policy", None), "rank", None),
        freshness=(getattr(manifest, "freshness_policy", None) or None) if freshness_ranking_enabled() else None,
        answer_profiles=(getattr(manifest, "answer_profiles", None) or None) if (answer_contract_enabled() and not _golden) else None,
        evidence_identity=evidence_identity_enabled(),
        claim_congruence=claim_congruence_enabled(),
        # LANDSCAPE COVERAGE (flag): force contract steer + the enumerative-categories landscape prompt +
        # explore legs so a "map the landscape" ask fans retrieval out per category. Else the normal knobs.
        question_contract=("steer" if landscape_coverage_enabled() else
                           "shadow" if (_deep_company_reader or _deep_people_reader)
                           else question_contract_mode()),
        contract_prompt=_apply_probe_addendum(_apply_reflection_addendum(
            (getattr(manifest, "landscape_contract_prompt", None)
             if landscape_coverage_enabled()
             # contract-compose uses the SHAPE classifier (can emit "enumerative"); the base prompt can't.
             else getattr(manifest, "contract_compose_prompt", None)
             if (_cc_compose and getattr(manifest, "contract_compose_prompt", None))
             else getattr(manifest, "contract_prompt", None)),
            manifest), manifest),
        enum_entity_probe=enum_entity_probe_enabled(),
        web_only=_web_only,
        explore_legs=(True if landscape_coverage_enabled() else explore_legs_enabled()),
        reflection=reflection_mode(),   # ROSTER_REFLECTION: on-demand web coverage fan-out + intent steer
        answer_mode_routing=answer_mode_routing_enabled(),
        enumerative_compose_addendum=getattr(manifest, "enumerative_compose_addendum", None),
        # ROSTER_CONTRACT_COMPOSE (voice ⟂ shape): render the derived contract at compose (run_react builds
        # VOICE + the SHAPE for the contract mode, overriding the flat directive).
        contract_compose=_cc_compose,
        contract_compose_voice=getattr(manifest, "contract_compose_voice", None),
        contract_compose_shapes=getattr(manifest, "contract_compose_shapes", None),
        contract_compose_default=getattr(manifest, "contract_compose_default", None),
        panel_specialists=getattr(manifest, "panel_specialists", ()),
        panel_default_ids=getattr(manifest, "panel_default_ids", ()),
        panel_synthesis_directive=getattr(manifest, "panel_synthesis_directive", None),
        panel_examples=getattr(manifest, "panel_examples", ()),
        panel_dedup=panel_dedup_enabled(),
        panel_contract=panel_contract_enabled(),
        panel_enumerative_addendum=getattr(manifest, "panel_enumerative_addendum", None),
        panel_decision_addendum=getattr(manifest, "panel_decision_addendum", None),
        sources=sources, gating=manifest.gating_policy, persona_prompt=persona,
        answer_format=answer_format,
        # Patient directive resolved INDEPENDENTLY of structured_answers/clinical_synthesis — the
        # patient view selects it per-request by audience, so it must be available even when the
        # clinician structured-answer flags are off (else patient mode would silently no-op).
        patient_answer_format=patient_directive,
        vision_prompt=vision_prompt, report_prompt=report_prompt,
        layman_prompt=manifest.layman_prompt, gap_prompt=gap_prompt,
        suggest_prompt=suggest_prompt, terms_prompt=getattr(manifest, "terms_prompt", None),
        visuals_prompt=getattr(manifest, "visuals_prompt", None),
        refine_prompt=refine_prompt, triage_prompt=triage_prompt,
        triage_prompt_v2=triage_prompt_v2,
        reasoned_scaffold_prompt=reasoned_scaffold, reasoned_answer_format=reasoned_format,
        integrative_prompt=getattr(manifest, "integrative_prompt", None),
        alt_directive=getattr(manifest, "alt_directive", None),
        alt_query_hint=getattr(manifest, "alt_query_hint", None),
        integrative_query_hint=getattr(manifest, "integrative_query_hint", None),
        understanding_answer_format=_understanding_fmt,
        understanding_query_hint=getattr(manifest, "understanding_query_hint", None),
        retrieval_source_cap=retrieval_diversity_frac(),
        source_routing=source_routing_enabled(),
        vertical_name=manifest.name, ui=manifest.ui,
        connectors=connectors, corpus_source_key=corpus_key,
    )


def _run_gap_processor(dsn: str, vertical: str) -> None:
    """Entry point for the DEDICATED ingest thread. Runs its own event loop so the heavy work
    (connector fetch + blocking OpenAI embed + index) never blocks the API's serving loop — this
    is what makes prod-direct ingestion, at bulk scale, safe to run inside the API process."""
    import asyncio as _a
    _a.run(_gap_processor_loop(dsn, vertical))


async def _gap_processor_loop(dsn: str, vertical: str) -> None:
    """One-at-a-time queue drain, on the ingest thread's own loop with its own pg pool + embedder +
    connectors. Atomic claim → replica-safe; a single job's error is recorded and the loop continues
    (Rule 13). Connectors open a fresh httpx client per call, so they are safe on this loop."""
    import asyncio
    from api.gap_queue import GapQueue
    from roster_kernel.providers.base import resolve_mode
    from roster_kernel.retrieval.postgres import PostgresRetrievalSource
    from roster_kernel.runtime.build import build_embedder, load_active_vertical
    q = GapQueue(dsn, vertical=vertical)
    embedder = build_embedder(mode=resolve_mode())
    pg = PostgresRetrievalSource(dsn, dim=embedder.dim, table="rs_block")
    connectors = dict(load_active_vertical().connectors)
    # Persist raw fetched artifacts to the SAME R2 bucket as other deployments, under a distinct
    # folder (ROSTER_R2_PREFIX, default "roster/raw") so roster's objects never collide. Keys are
    # <prefix>/<sha256> (content-addressed dedup). None → raw stays in-memory (index-only).
    import os as _osenv
    object_store = None
    if _osenv.environ.get("R2_BUCKET"):
        try:
            from roster_kernel.ingestion.s3_storage import S3ObjectStore
            object_store = S3ObjectStore.from_env(
                prefix=_osenv.environ.get("ROSTER_R2_PREFIX", "roster/raw"))
        except Exception:   # noqa: BLE001 — raw persistence is best-effort; index still lands
            object_store = None
    # Evidence Pulse re-stamp hook: re-ingest overwrites block facets (erasing supersession/
    # retraction stamps) — after each completed job, re-derive stamps from the approved ledger.
    # THIS thread's own store/pool (the API loop's store must never be awaited from here).
    currency = None
    if pulse_enabled():
        from roster_kernel.currency import CurrencyStore
        currency = CurrencyStore(dsn)
    while True:
        try:
            job = await q.claim_one()
        except Exception:
            await asyncio.sleep(10); continue
        if job is None:
            if currency is not None:
                # DAILY recreatability backup → R2 (same replica-safe idle-path pattern):
                # every irreplaceable table + corpus text, so a total DB loss restores
                # from the bucket alone (scripts/restore_from_backup.py).
                try:
                    import datetime as _dt
                    import os as _os
                    if _os.environ.get("R2_BUCKET"):
                        bst = await currency.get_state("last_backup") or {}
                        blast = bst.get("at", "")
                        bdue = (not blast or (_dt.datetime.now(_dt.timezone.utc)
                                - _dt.datetime.fromisoformat(blast)).days >= 1)
                        if bdue:
                            await currency.set_state("last_backup",
                                {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                                 "status": "running"})
                            from api.backup import R2Named, run_backup
                            m = await run_backup(dsn, R2Named())
                            await currency.set_state("last_backup",
                                {"at": m["finished_at"],
                                 "manifest": {k: m[k] for k in ("date", "tables",
                                                                "corpus_parts", "errors")}})
                except Exception:   # noqa: BLE001 — the admin endpoint is the manual backstop
                    pass
            await asyncio.sleep(8); continue
        conn = connectors.get(job["connector"])
        if conn is None:
            await q.fail(job["id"], f"unknown connector {job['connector']}"); continue
        try:
            # a job may stamp generic facet overrides (e.g. {"sector":"ai"}) on everything it ingests,
            # plus the legacy source_country stamp.
            _ov = {}
            _jf = job.get("facets") or {}
            if isinstance(_jf, dict):
                _ov.update({k: v for k, v in _jf.items() if v})
            sc = job.get("source_country")
            if sc:
                _ov["source_country"] = sc
            # generic per-job connector params merged into the fetch window (e.g. {"forms":["D"]})
            _window = {"query": job["query"], "limit": job["limit"]}
            _params = job.get("params")
            if isinstance(_params, dict):
                _window.update(_params)
            n = await ingest_connector_to_postgres(
                conn, pg, tenant_id=job["tenant_id"], embedder=embedder,
                window=_window,
                object_store=object_store,
                facet_overrides=_ov or None,
                min_chars=40,   # drop metadata one-liner noise (panel: no tiny blocks)
                # COALESCE paragraphs into coherent ~1.8k-char blocks (full-text papers otherwise
                # fragment into hundreds of tiny blocks that scatter context). Deep-tech tuned.
                target_chars=1800)
            await q.complete(job["id"], n)
            if currency is not None:
                try:
                    await currency.apply_stamps()      # heal any stamps this ingest overwrote
                except Exception:   # noqa: BLE001 — best-effort; the admin scan is the backstop
                    pass
        except Exception as e:   # noqa: BLE001 — record + move on
            await q.fail(job["id"], str(e))


def create_app(service: ResearchService | None = None) -> FastAPI:
    app = FastAPI(title="Roster Research", version="0")
    app.state.service = service   # lazily built on first request if None

    def _store():
        """Vertical-isolated research-session store (Postgres-backed). Built once when a
        corpus DSN is configured; None (no persistence) against the fixture corpus."""
        if getattr(app.state, "session_store", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn:
                from api.sessions import SessionStore
                app.state.session_store = SessionStore(dsn, vertical=load_active_vertical().name)
            else:
                app.state.session_store = None
        return app.state.session_store

    def _claim_store_cached():
        """The tech claim-graph store, built ONCE and cached (its asyncpg pool is reused across
        requests) so company-link resolution doesn't spin a new pool per answer. None when no
        corpus DSN is configured."""
        if getattr(app.state, "claim_store", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn:
                from api.claimgraph_tech import make_tech_claim_store
                app.state.claim_store = make_tech_claim_store(dsn)
            else:
                app.state.claim_store = None
        return app.state.claim_store

    def _glossary():
        """Vertical-isolated glossary store (the accumulating term web). Built once when a
        corpus DSN is configured; None (no persistence) against the fixture corpus."""
        if getattr(app.state, "glossary_store", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn:
                from api.glossary import GlossaryStore
                app.state.glossary_store = GlossaryStore(dsn, vertical=load_active_vertical().name)
            else:
                app.state.glossary_store = None
        return app.state.glossary_store

    def _perf():
        """Vertical-isolated performance-metrics store. Built once when a corpus DSN is configured."""
        if getattr(app.state, "perf_store", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn:
                from api.perf import PerfStore
                app.state.perf_store = PerfStore(dsn, vertical=load_active_vertical().name)
            else:
                app.state.perf_store = None
        return app.state.perf_store

    def _settings():
        """Live product-settings store (same DSN); None without a DSN → env-only flags."""
        if getattr(app.state, "setting_store", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn:
                from api.settings import SettingStore
                app.state.setting_store = SettingStore(dsn, vertical=load_active_vertical().name)
            else:
                app.state.setting_store = None
        return app.state.setting_store

    # Flags the admin panel can flip LIVE (DB override wins; empty → env default). Only flags whose
    # gating is fully REQUEST-time belong here — accounts_enabled stays env-only (it gates store
    # construction at boot). New product settings land in this dict going forward.
    _LIVE_FLAGS = {"duel_enabled": duel_enabled, "triage_enabled": triage_enabled,
                   "ask_panel_enabled": ask_panel_enabled, "integrative_enabled": integrative_enabled,
                   "reasoned_default_enabled": reasoned_default_enabled}

    async def _flag_live(key: str) -> bool:
        """Resolved value of a controlled flag: DB override → else env default. Fail-open to env."""
        env_fn = _LIVE_FLAGS[key]
        st = _settings()
        if st is None:
            return env_fn()
        try:
            from api.settings import SettingStore
            return SettingStore.resolve_flag(await st.get(key), env_fn())
        except Exception:   # noqa: BLE001 — settings must never break a request
            return env_fn()

    def _accounts():
        """Vertical-isolated account+feedback store (same DSN as sessions); None without a DSN."""
        if getattr(app.state, "account_store", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn and accounts_enabled():
                from api.accounts import AccountStore
                app.state.account_store = AccountStore(dsn, vertical=load_active_vertical().name)
            else:
                app.state.account_store = None
        return app.state.account_store

    async def _attach_video(session_id: str, **kw) -> None:
        store = _store()
        if store is not None:
            await store.attach_video(session_id, **kw)

    def _gap_queue():
        """Vertical-isolated corpus gap-fill queue (Postgres). None unless a corpus DSN is set
        AND gap-healing is enabled — so OFF is a true no-op."""
        if getattr(app.state, "gap_queue", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn and gap_healing_enabled():
                from api.gap_queue import GapQueue
                app.state.gap_queue = GapQueue(dsn, vertical=load_active_vertical().name)
            else:
                app.state.gap_queue = None
        return app.state.gap_queue

    def _currency():
        """Evidence Pulse ledger (Postgres). None unless a corpus DSN is set AND the pulse flag is
        on — so OFF is a true no-op (no table, no stamps, no demotion)."""
        if getattr(app.state, "currency", "unset") == "unset":
            dsn = os.environ.get("ROSTER_CORPUS_DSN")
            if dsn and pulse_enabled():
                from roster_kernel.currency import CurrencyStore
                app.state.currency = CurrencyStore(dsn)
            else:
                app.state.currency = None
        return app.state.currency

    def _graph():
        """Grounded Relationship Graph store — the module-level singleton (one pool + one
        adjacency snapshot per process, shared with the answer-path expander). None unless a
        corpus DSN is set AND the graph flag is on — so OFF is a true no-op."""
        return _graph_store()

    @app.on_event("startup")
    async def _start_gap_processor() -> None:
        """Launch the corpus-ingest processor in a DEDICATED daemon thread (own loop + pools), so
        prod-direct ingestion (gap-fill AND bulk batches) never blocks the API's serving loop.
        Replica-safe: each replica's thread claims jobs atomically, so N replicas share the drain."""
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if gap_healing_enabled() and dsn and ingest_in_api_enabled() \
                and not getattr(app.state, "_gap_thread", None):
            import threading
            vertical = load_active_vertical().name
            t = threading.Thread(target=_run_gap_processor, args=(dsn, vertical),
                                 daemon=True, name="corpus-ingest")
            t.start()
            app.state._gap_thread = t

    # Answer-video add-on — separate, flag-gated router (default OFF). Kept fully out of
    # the research path: mounting it changes nothing about how answers are produced.
    from api.video import build_video_router, video_enabled
    if video_enabled():
        app.include_router(build_video_router(attach_video=_attach_video))

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/config")
    async def config() -> dict:
        """The active vertical's declared UI + available sources (drives the shell).
        Panel/triage/duel flags resolve LIVE (admin-panel override → env default)."""
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        live_panel = await _flag_live("ask_panel_enabled")
        live_triage = await _flag_live("triage_enabled")
        live_duel = await _flag_live("duel_enabled")
        ui = getattr(svc, "ui", None)
        from api.video import video_enabled
        console = ui.console() if ui and hasattr(ui, "console") else {}
        return {
            "vertical": getattr(svc, "vertical_name", ""),
            "sources": list(svc.sources.keys()),
            "navigation": ui.navigation() if ui else [],
            "search_facets": ui.search_facets() if ui else [],
            "console": console,
            "video_enabled": video_enabled(),
            "structured_answers": structured_answers(),
            "clinical_synthesis": clinical_synthesis() and structured_answers(),
            "evidence_select": bool(getattr(svc, "evidence_select", False)),
            "vision_enabled": vision_enabled(),
            "layman_enabled": bool(getattr(svc, "layman_prompt", None)),
            "gap_healing_enabled": gap_healing_enabled() and bool(getattr(svc, "gap_prompt", None)),
            "conversation_enabled": conversation_enabled(),
            "focus_deepen_enabled": focus_deepen_enabled(),
            "related_research_enabled": related_research_enabled(),
            "company_links_enabled": company_links_enabled(),
            "suggest_enabled": conversation_enabled() and bool(getattr(svc, "suggest_prompt", None)),
            "term_glossary_enabled": term_glossary_enabled() and bool(getattr(svc, "terms_prompt", None)),
            "visual_augment_enabled": visual_augment_enabled() and bool(getattr(svc, "visuals_prompt", None)),
            "visual_auto_enabled": (visual_auto_enabled() and visual_augment_enabled()
                                    and bool(getattr(svc, "visuals_prompt", None))),
            "voice_intake_enabled": voice_intake_enabled() and live_triage,
            "voice_tts_neural": (voice_intake_enabled() and live_triage
                                 and bool(os.environ.get("OPENAI_API_KEY"))),
            "stream_enabled": stream_enabled(),
            "effort_scale_enabled": effort_scale_enabled(),
            "effort_stops": EFFORT_STOPS if effort_scale_enabled() else [],
            "patient_mode_enabled": patient_mode_enabled(),
            "answer_focus_enabled": answer_focus_enabled(),
            "followup_clarify_enabled": followup_clarify_enabled(),
            "answer_visuals_enabled": answer_visuals_enabled(),
            "answer_charts_enabled": answer_charts_enabled(),
            "freshness_ranking_enabled": freshness_ranking_enabled(),
            "answer_contract_enabled": answer_contract_enabled(),
            # ROSTER_GOLDEN_ANSWER hides the UI reasoning panel (Tier E): golden emits no interpretation/
            # confidence, so the client-side "Critical reasoning" render must be off too.
            "reasoning_read_enabled": reasoning_read_enabled() and structured_answers() and not golden_answer_enabled(),
            "golden_answer_enabled": golden_answer_enabled(),
            "diag_trace_enabled": diag_trace_enabled(),
            "evidence_fitness_enabled": evidence_fitness_enabled(),
            "authority_basis_enabled": authority_basis_enabled(),
            "parametric_led_enabled": parametric_led_enabled(),
            "intelligence_core_enabled": intelligence_core_enabled(),
            "cross_family_judge_enabled": cross_family_judge_enabled() and bool(os.environ.get("OPENAI_API_KEY")),
            "ask_panel_enabled": live_panel,
            "panel_specialists": ([
                {"id": getattr(s, "id", ""), "specialty": getattr(s, "specialty", ""),
                 "focus": getattr(s, "focus", ""),   # the specialist's expertise, shown on the panel roster
                 "default": getattr(s, "id", "") in set(getattr(svc, "panel_default_ids", ()))}
                for s in getattr(svc, "panel_specialists", ())] if live_panel else []),
            "panel_examples": (list(getattr(svc, "panel_examples", ())) if live_panel else []),
            "refine_enabled": refine_enabled() and bool(getattr(svc, "refine_prompt", None)),
            "triage_enabled": live_triage and bool(getattr(svc, "triage_prompt", None)),
            "pulse_enabled": pulse_enabled() and bool(os.environ.get("ROSTER_CORPUS_DSN")),
            "graph_enabled": graph_enabled() and bool(os.environ.get("ROSTER_CORPUS_DSN")),
            "graph_expand": graph_expand_mode(),
            "accounts_enabled": accounts_enabled() and bool(os.environ.get("ROSTER_CORPUS_DSN")),
            "duel_enabled": live_duel and bool(getattr(svc, "reasoned_answer_format", None)),
            "integrative_enabled": (await _flag_live("integrative_enabled")) and bool(getattr(svc, "integrative_prompt", None)),
            "dynamic_engines_enabled": (await _flag_live("reasoned_default_enabled")) and bool(getattr(svc, "reasoned_answer_format", None)),
            "people_geo_scope_enabled": people_geo_scope_enabled(),
            "jobs_enabled": jobs_enabled(),
        }

    @app.post("/search")
    async def search(body: ResearchIn) -> dict:
        """Retrieval only (no LLM) — ranked evidence over the chosen sources.
        Always available (needs only the embedder), so the UI can show real
        evidence even when the answer model is unavailable."""
        if app.state.service is None:
            app.state.service = build_default_service()
        try:
            hits = await app.state.service.search(
                question=body.question, tenant_id=body.tenant_id,
                workspace_id=body.workspace_id, source_keys=body.sources, k=8)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"retrieval error: {e}") from e
        return {"evidence": [{
            "text": h.text[:600], "source": h.source_key or "corpus",
            "title": h.document_title, "score": round(h.score, 4),
        } for h in hits]}

    @app.post("/discover")
    async def discover(body: DiscoverIn) -> dict:
        """Discovery / sourcing (corp-dev / M&A): the companies/orgs most associated with a capability
        query, ranked, each with grounded verbatim evidence + source links. Retrieval-only (no answer
        LLM) — fast and always available. Gated by ROSTER_DISCOVERY + the vertical's discovery_entity_of."""
        if not discovery_enabled():
            raise HTTPException(status_code=404, detail="discovery not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        entity_of = getattr(load_active_vertical(), "discovery_entity_of", None)
        if entity_of is None:
            raise HTTPException(status_code=404, detail="discovery unavailable for this vertical")
        try:
            hits = await svc.search(question=body.query, tenant_id=body.tenant_id,
                                    workspace_id=body.workspace_id, source_keys=body.sources,
                                    k=max(20, min(400, body.pool)))
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"retrieval error: {e}") from e
        from roster_kernel.research.discover import aggregate_entities
        # tier bonus from the vertical's evidence classifier + authority ranks (so a company tied by an
        # audited filing ranks above one tied by a preprint or a news headline).
        classify = getattr(svc, "evidence_classifier", None) or getattr(load_active_vertical(), "evidence_classifier", None)
        authority = getattr(load_active_vertical(), "authority_policy", None)
        def _tier(h):
            if not (classify and authority):
                return 0.0
            try:
                return authority.rank(classify(h.source_key, h.facets)) / 6.0
            except Exception:   # noqa: BLE001
                return 0.0
        companies = aggregate_entities(hits, entity_of, top=max(1, min(100, body.limit)),
                                       tier_of=_tier)
        # attach a clickable source url per evidence item (vertical link resolver)
        ui = getattr(svc, "ui", None)
        for c in companies:
            for ev in c.get("evidence", []):
                if ui and hasattr(ui, "source_url"):
                    ev["url"] = ui.source_url(ev.get("document_id", ""), ev.get("quote"))
        return {"query": body.query, "count": len(companies), "companies": companies}

    @app.post("/ingest")
    async def ingest(tenant_id: str = "demo") -> dict:
        """Populate the pg-backed corpus from the active vertical's connectors.
        No-op with a note if no ROSTER_CORPUS_DSN is configured (fixture corpus)."""
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        if not svc.connectors or not svc.corpus_source_key:
            return {"ingested": 0, "note": "no pg corpus configured — set ROSTER_CORPUS_DSN"}
        pg = svc.sources[svc.corpus_source_key]
        total = 0
        for conn in svc.connectors.values():
            total += await ingest_connector_to_postgres(
                conn, pg, tenant_id=tenant_id, embedder=svc.embedder)
        return {"ingested": total, "tenant_id": tenant_id}

    # The single-page app shell is a COMMITTED file that changes on every deploy. Serve it with
    # no-store so browsers always fetch the current build — otherwise a stale cached index.html keeps
    # running old front-end code and new features (flags/UI) never reach the user until a hard refresh.
    _NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    # PERF: the shell is ~220 KB and was shipped uncompressed on every load (~3 s). Pre-gzip the HTML
    # ONCE per process (files are baked into the image, immutable per deploy) and serve the compressed
    # bytes when the client accepts gzip. Deliberately NOT a blanket GZip middleware: compressing the
    # SSE streams would buffer keepalives and resurrect the edge-502 bug — only these two HTML routes.
    import gzip as _gzip
    _HTML_CACHE: dict[str, tuple[bytes, bytes]] = {}   # name -> (raw, gzipped)

    def _html_response(fname: str, accept_encoding: str):
        from fastapi.responses import Response
        if fname not in _HTML_CACHE:
            page = _WEB_DIR / fname
            raw = page.read_bytes() if page.exists() else b"<h1>Roster</h1>"
            _HTML_CACHE[fname] = (raw, _gzip.compress(raw, 6))
        raw, gz = _HTML_CACHE[fname]
        if "gzip" in (accept_encoding or "").lower():
            return Response(gz, media_type="text/html",
                            headers={**_NO_CACHE, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(raw, media_type="text/html", headers=_NO_CACHE)

    @app.get("/", response_class=HTMLResponse)
    def index(accept_encoding: str = Header(default="")):
        return _html_response("index.html", accept_encoding)

    @app.get("/corpus", response_class=HTMLResponse)
    def corpus_explorer(accept_encoding: str = Header(default="")):
        """Admin corpus explorer — pure-retrieval source inspection (token entered client-side)."""
        return _html_response("corpus.html", accept_encoding)

    @app.get("/explorer", response_class=HTMLResponse)
    def interactive_explorer(accept_encoding: str = Header(default="")):
        """Interactive Explorer — a live, navigable slice of the grounded claim graph
        (entities + relationship edges + span-verified facts). Data via GET /graph/explore."""
        return _html_response("explorer.html", accept_encoding)

    @app.get("/entity/{entity_id:path}", response_class=HTMLResponse)
    async def entity_page(entity_id: str) -> HTMLResponse:
        """Grounded entity page (flag-gated with company links, 404 when off): everything Roster
        has span-verified about one company — its claims grouped by dimension, each with the exact
        source quote + a link. The ◉ target of a company hyperlink. Read-only, no LLM."""
        if not company_links_enabled():
            raise HTTPException(status_code=404, detail="entity pages not enabled")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="ROSTER_CORPUS_DSN not configured")
        from html import escape as _e
        from api.claimgraph_tech import make_tech_claim_store
        store = make_tech_claim_store(dsn)
        ent = await store.get_entity(entity_id)
        claims = await store.entity_claims(entity_id)
        ui = getattr(app.state.service, "ui", None) if app.state.service else None
        name = (ent or {}).get("name") or entity_id
        src_page = None
        try:
            src_page = ui.source_url(entity_id) if ui else None
        except Exception:
            src_page = None

        def _label(pred: str) -> str:
            return pred.replace("_", " ").strip().capitalize()

        rows_html = ""
        if claims:
            by_pred: dict[str, list] = {}
            for cl in claims:
                by_pred.setdefault(cl.get("predicate", ""), []).append(cl)
            for pred, cls in by_pred.items():
                items = ""
                for cl in cls:
                    val = (cl.get("object_value") or cl.get("object_norm")
                           or cl.get("object_entity_id") or "")
                    ev = cl.get("evidence") or {}
                    quote = ev.get("quote") or ""
                    try:
                        curl = ui.source_url(ev.get("document_id", ""), quote) if ui and ev.get("document_id") else None
                    except Exception:
                        curl = None
                    cite = (f'<a class="cite" href="{_e(curl, quote=True)}" target="_blank" rel="noopener noreferrer">source ↗</a>'
                            if curl else '')
                    q = f'<div class="q">“{_e(quote[:280])}”</div>' if quote else ''
                    items += f'<li><div class="v">{_e(str(val))}</div>{q}{cite}</li>'
                rows_html += f'<section class="grp"><h2>{_e(_label(pred))}</h2><ul>{items}</ul></section>'
        else:
            rows_html = '<p class="empty">No grounded claims for this entity yet.</p>'

        src_link = (f'<a class="ext" href="{_e(src_page, quote=True)}" target="_blank" rel="noopener noreferrer">Canonical page ↗</a>'
                    if src_page else '')
        html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{_e(name)} · Roster</title><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root{{--ground:#0a0d16;--panel:#111627;--line:#212a41;--line-soft:#1a2136;--ink:#e8edf9;--mute:#909bb8;--faint:#5c6688;--gold:#ffcf6b;
--sans:"IBM Plex Sans",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace;--display:"Sora",var(--sans);}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--ground);color:var(--ink);font-family:var(--sans);line-height:1.55;}}
.wrap{{max-width:760px;margin:0 auto;padding:40px 22px 80px;}}
a{{color:inherit}}.back{{font-family:var(--mono);font-size:12px;color:var(--mute);text-decoration:none}}.back:hover{{color:var(--gold)}}
.eyebrow{{font-family:var(--mono);font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--gold);margin-top:26px}}
h1{{font-family:var(--display);font-weight:700;font-size:30px;margin:.2rem 0 .1rem;letter-spacing:-.01em}}
.head-meta{{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin-top:8px}}
.ext,.eid{{font-family:var(--mono);font-size:12px;color:var(--mute);text-decoration:none}}.ext:hover{{color:var(--gold)}}
.grp{{margin-top:26px;border-top:1px solid var(--line-soft);padding-top:14px}}
.grp h2{{font-family:var(--mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--gold);font-weight:500;margin:0 0 10px}}
.grp ul{{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:12px}}
.grp li{{background:var(--panel);border:1px solid var(--line-soft);border-left:2px solid var(--gold);border-radius:9px;padding:10px 13px}}
.v{{font-size:15px;font-weight:500}}.q{{font-size:13px;color:var(--mute);font-style:italic;margin:6px 0 4px;padding-left:10px;border-left:2px solid var(--line)}}
.cite{{font-family:var(--mono);font-size:11px;color:var(--faint);text-decoration:none}}.cite:hover{{color:var(--gold)}}
.empty{{color:var(--faint);margin-top:30px}}.note{{font-size:12px;color:var(--faint);margin-top:34px;border-top:1px solid var(--line-soft);padding-top:14px}}
</style></head><body><div class="wrap">
<a class="back" href="/">&larr; Roster</a>
<div class="eyebrow">Grounded entity</div>
<h1>{_e(name)}</h1>
<div class="head-meta">{src_link}<span class="eid">{_e(entity_id)}</span></div>
{rows_html}
<div class="note">Every value here is span-verified to a quote in a source document — a live slice of Roster's claim graph.</div>
</div></body></html>"""
        return HTMLResponse(html)

    @app.get("/crossviews", response_class=HTMLResponse)
    def crossviews_designer(accept_encoding: str = Header(default="")):
        """Task CV3 (flag-gated, Rule 20). CROSSVIEWS — the split-pane grounded-table
        designer (row-axis + visualizer chat + column picker → live table with every
        non-empty cell click-to-source). Data via the CV2 /crossviews/* endpoints. OFF →
        404 (true no-op), matching the CV1/CV2 data endpoints."""
        if not crossviews_enabled():
            raise HTTPException(status_code=404, detail="crossviews not enabled")
        return _html_response("crossviews.html", accept_encoding)

    @app.get("/graph/explore")
    async def graph_explore(tenant_id: str = "demo", limit: int = 60, sector: str = ""):
        """Live data for the Interactive Explorer: the richest companies + their entity edges
        (founders/categories/investors) + grounded value-attributes with their verbatim quotes.
        Read-only over the claim graph; returns empty (never errors the page) if unavailable."""
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        _empty = {"companies": [], "edges": [], "values": [], "sectors": [], "sector": ""}
        if not dsn:
            return _empty
        cap = max(4, min(int(limit or 60), 120))
        import asyncpg
        try:
            conn = await asyncpg.connect(dsn)
        except Exception:   # noqa: BLE001
            return _empty
        try:
            # SECTOR clusters (each an AI sub-sector): the picker views ONE at a time so the graph never
            # becomes an all-clusters hairball. A company's sector lives in rs_entity.facets->>'sector'.
            sector_rows = await conn.fetch(
                """SELECT e.facets->>'sector' sector, count(DISTINCT e.entity_id) n
                   FROM rs_entity e
                   WHERE e.tenant_id = $1 AND e.kind = 'company'
                     AND coalesce(e.facets->>'sector','') <> ''
                     AND EXISTS (SELECT 1 FROM rs_claim c WHERE c.subject_id = e.entity_id
                                 AND c.tenant_id = $1)
                   GROUP BY e.facets->>'sector' ORDER BY n DESC""",
                tenant_id)
            sectors = [{"id": r["sector"], "n": r["n"]} for r in sector_rows]
            # Default to the largest sector so the payload is always a single clean cluster.
            active_sector = sector or (sectors[0]["id"] if sectors else "")
            if active_sector:
                top = await conn.fetch(
                    """SELECT e.entity_id, e.name, count(*) n FROM rs_claim cl
                       JOIN rs_entity e ON e.entity_id = cl.subject_id AND e.kind = 'company'
                       WHERE cl.tenant_id = $1 AND e.facets->>'sector' = $3
                         AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                     WHERE ev.claim_id = cl.claim_id AND ev.evidence_status = 'active')
                       GROUP BY e.entity_id, e.name ORDER BY n DESC LIMIT $2""",
                    tenant_id, cap, active_sector)
            else:
                # no sectors stamped yet (pre-population) → the whole graph, as before
                top = await conn.fetch(
                    """SELECT e.entity_id, e.name, count(*) n FROM rs_claim cl
                       JOIN rs_entity e ON e.entity_id = cl.subject_id AND e.kind = 'company'
                       WHERE cl.tenant_id = $1
                         AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                     WHERE ev.claim_id = cl.claim_id AND ev.evidence_status = 'active')
                       GROUP BY e.entity_id, e.name ORDER BY n DESC LIMIT $2""",
                    tenant_id, cap)
            subj = [r["entity_id"] for r in top]
            if not subj:
                return {**_empty, "sectors": sectors, "sector": active_sector}
            edge_rows = await conn.fetch(
                """SELECT cl.subject_id, s.name sname, cl.predicate,
                          cl.object_entity_id, o.name oname, o.kind okind
                   FROM rs_claim cl JOIN rs_entity s ON s.entity_id = cl.subject_id
                   LEFT JOIN rs_entity o ON o.entity_id = cl.object_entity_id
                   WHERE cl.object_kind = 'entity' AND cl.subject_id = ANY($1) AND cl.tenant_id = $2
                     AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                 WHERE ev.claim_id = cl.claim_id AND ev.evidence_status = 'active')""",
                subj, tenant_id)
            val_rows = await conn.fetch(
                """SELECT cl.subject_id, cl.predicate, cl.object_value, cl.object_norm,
                          (SELECT ev.quote FROM rs_claim_evidence ev
                           WHERE ev.claim_id = cl.claim_id AND ev.evidence_status = 'active'
                           ORDER BY ev.authority_tier DESC LIMIT 1) quote
                   FROM rs_claim cl
                   WHERE cl.object_kind = 'value' AND cl.subject_id = ANY($1) AND cl.tenant_id = $2
                     AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                 WHERE ev.claim_id = cl.claim_id AND ev.evidence_status = 'active')""",
                subj, tenant_id)
            # PEOPLE nodes: founders/key-people are 1:1 with their company (never corroborate), so
            # they're materialized DIRECTLY from the grounded value claims, keyed PER COMPANY
            # (`person:<subject_id>:<norm>`) so two namesake founders at different companies never
            # merge (matches claimgraph.promote_person_claims' id derivation exactly).
            person_rows = await conn.fetch(
                """SELECT cl.subject_id, s.name sname, cl.object_value, cl.object_norm
                   FROM rs_claim cl JOIN rs_entity s ON s.entity_id = cl.subject_id
                   WHERE cl.predicate IN ('has_founder','key_person') AND cl.object_kind = 'value'
                     AND cl.object_norm <> '' AND cl.subject_id = ANY($1) AND cl.tenant_id = $2
                     AND EXISTS (SELECT 1 FROM rs_claim_evidence ev
                                 WHERE ev.claim_id = cl.claim_id AND ev.evidence_status = 'active')""",
                subj, tenant_id)
            # ALIAS MAP (flag ROSTER_ALIAS_RESOLVER): LLM-built surface-form merges. rs_entity_alias has
            # no tenant/kind column and alias_norm isn't unique, so we load (kind-from-id, name) and
            # ABSTAIN on any collision below (panel guidance). Empty when the flag is OFF → OFF is a no-op.
            alias_rows = []
            if alias_resolver_enabled():
                alias_rows = await conn.fetch(
                    """SELECT a.alias_norm, a.entity_id, e.name, e.kind
                       FROM rs_entity_alias a JOIN rs_entity e ON e.entity_id = a.entity_id
                       WHERE e.tenant_id = $1 AND e.status = 'active'
                         AND e.kind = ANY($2)""",
                    tenant_id, ["investor", "product", "technology", "category", "market", "company"])
        finally:
            await conn.close()
        subj_name = {r["entity_id"]: r["name"] for r in top}
        edges = [{"s": r["subject_id"], "sname": r["sname"], "p": r["predicate"],
                  "o": r["object_entity_id"], "oname": r["oname"] or r["object_entity_id"],
                  "okind": r["okind"] or "unknown"}
                 for r in edge_rows if r["object_entity_id"]]
        # Predicate → object node-kind (config from the vertical registry, Rule 18 — not a semantic
        # guess): has_investor→investor, uses_technology→technology, compared_to→company, …
        try:
            from roster_vertical.claim_predicates import active_predicates as _ap
            _pred_kind = {p["name"]: (p.get("object_entity_kind") or p.get("mention_kind"))
                          for p in _ap(include_diligence=True)
                          if (p.get("object_entity_kind") or p.get("mention_kind"))}
        except Exception:   # noqa: BLE001 — vertical unavailable → no relationship edges (facts only)
            _pred_kind = {}
        # RELATIONSHIP EDGES straight from the grounded VALUE claims (every one span-verified), keyed
        # `<kind>:<norm>` so companies naming the SAME thing share ONE hub node — NO corroboration gate,
        # so EVERY grounded investor / product / tech / category / segment / rival lights up, not only the
        # promoted ones. (Promotion still governs the canonical ENTITY graph that answers read; this is the
        # faithful VIEW.) People are handled separately (per-company nodes). Surface-form dedup across
        # aliases (a16z ≈ Andreessen Horowitz) is the LLM alias-resolver's job (Rule 18), still deferred.
        _REL_KINDS = {"investor", "product", "technology", "category", "market", "company"}
        rel_preds = {p: k for p, k in _pred_kind.items()
                     if k in _REL_KINDS and p not in ("has_founder", "key_person")}
        # Collapse the alias map to (kind, alias_norm) → canonical, ABSTAINING on any norm that maps to
        # >1 distinct canonical of the same kind (ambiguous → keep raw, the fail-safe). Kind-scoped so a
        # norm that is a company alias never hijacks a technology edge (panel). A SELF-alias
        # (`norm -> <kind>:<norm>`, the identity mapping every raw node already has) is NOT a competing
        # canonical — exclude it, else a real merge (a16z -> andreessen horowitz) always looks ambiguous
        # against the raw node's own self-alias and never applies.
        _alias_cands: dict = {}
        _alias_name: dict = {}
        for r in alias_rows:
            if r["entity_id"] == f"{r['kind']}:{r['alias_norm']}":
                continue                                  # identity alias — ignore
            _alias_cands.setdefault((r["kind"], r["alias_norm"]), set()).add(r["entity_id"])
            _alias_name[r["entity_id"]] = r["name"]
        alias_map = {k: next(iter(v)) for k, v in _alias_cands.items() if len(v) == 1}
        _seen = {(e["s"], e["p"], e["o"]) for e in edges}
        for r in val_rows:
            kind = rel_preds.get(r["predicate"])
            if not kind or not r["object_norm"]:
                continue
            # ALIAS REMAP (before dedup, so variant spellings collapse to one edge): a canonical merge
            # for THIS kind wins; otherwise the raw `<kind>:<norm>` node (byte-identical when flag OFF).
            canon = alias_map.get((kind, r["object_norm"]))
            if canon:
                nid, oname = canon, (_alias_name.get(canon) or r["object_value"])
            else:
                nid, oname = f"{kind}:{r['object_norm']}", r["object_value"]
            k = (r["subject_id"], r["predicate"], nid)
            if k in _seen:
                continue
            _seen.add(k)
            edges.append({"s": r["subject_id"], "sname": subj_name.get(r["subject_id"], r["subject_id"]),
                          "p": r["predicate"], "o": nid, "oname": oname, "okind": kind})
        # PEOPLE edges: company -> per-company person node (founders/key-people), deduped by node id.
        for r in person_rows:
            pid = f"person:{r['subject_id']}:{r['object_norm']}"
            k = (r["subject_id"], "has_founder", pid)
            if k in _seen:
                continue
            _seen.add(k)
            edges.append({"s": r["subject_id"], "sname": r["sname"], "p": "has_founder",
                          "o": pid, "oname": r["object_value"], "okind": "person"})
        # Right-pane VALUE FACTS: exclude predicates now shown as edges/pills (relationships + people),
        # so the pane holds the genuine scalar facts (funding, revenue, traction, headcount, differentiators…).
        _edge_preds = set(rel_preds) | {"has_founder", "key_person"}
        # DEDUP value facts: the SAME fact extracted from many sources appears as near-identical value
        # strings ("$24M Series A" / "$24 million Series A" / "Series A: $24M"). Collapse by a COMPUTABLE
        # signature — currency/unit/punctuation-normalized + token-sorted (Rule 18: structural, no
        # semantics) — keeping the row with the LONGEST quote (most informative provenance). Genuinely
        # different facts ($24M-A vs $50M-B) get different signatures and are NOT merged.
        import re as _re
        def _vsig(v: str) -> str:
            s = (v or "").lower()
            s = _re.sub(r'[\$,:;()\"\'.%]', ' ', s)
            s = _re.sub(r'\b(million|mn|mm)\b', 'm', s)
            s = _re.sub(r'\b(billion|bn)\b', 'b', s)
            s = _re.sub(r'\b(thousand)\b', 'k', s)
            s = _re.sub(r'(\d)\s+([mbk])\b', r'\1\2', s)   # "24 m" -> "24m"
            return " ".join(sorted(t for t in s.split() if t))
        _best: dict = {}
        for r in val_rows:
            if r["predicate"] in _edge_preds:
                continue
            key = (r["subject_id"], r["predicate"], _vsig(r["object_value"]))
            q = r["quote"] or ""
            cur = _best.get(key)
            if cur is None or len(q) > len(cur["_ql"]):
                _best[key] = {"s": r["subject_id"], "p": r["predicate"],
                              "v": r["object_value"], "q": q[:180], "_ql": q}
        return {
            "companies": [{"id": r["entity_id"], "name": r["name"], "claims": r["n"]} for r in top],
            "edges": edges,
            "values": [{k: v for k, v in d.items() if k != "_ql"} for d in _best.values()],
            "sectors": sectors,
            "sector": active_sector,
        }

    @app.get("/graph/path")
    async def graph_path(from_: str = Query("", alias="from"), to: str = "",
                         tenant_id: str = "demo", max_depth: int = 4, max_paths: int = 10):
        """Slice-1 edge model (flag ROSTER_EDGE_MODEL, default OFF): grounded connection PATHS
        between two entities over the claim graph's active-evidence entity-edges. Returns the
        resolved endpoints and up to `max_paths` paths, each hop carrying its verbatim citation —
        the prod-observable acceptance surface for "how is X connected to Y". Read-only; returns
        `enabled:false` when the flag is off, and empty results (never a 500) when unavailable.
        `from_` maps to the query param `from` (a Python keyword)."""
        enabled = os.environ.get("ROSTER_EDGE_MODEL", "").lower() in ("1", "true", "yes")
        out = {"enabled": enabled, "source": None, "target": None, "paths": []}
        if not enabled:
            return out
        store = _claim_store_cached()
        if store is None or not from_ or not to:
            return out
        try:
            src = await store.find_entity(from_, tenant_id=tenant_id)
            tgt = await store.find_entity(to, tenant_id=tenant_id)
            if not src or not tgt:
                return {**out, "source": src, "target": tgt}
            from api.graph_path import Edge, find_paths

            async def _neighbors(eid: str):
                rows = await store.neighbors(eid, tenant_id=tenant_id)
                return [Edge(subject_id=r["subject_id"], predicate=r["predicate"],
                             object_id=r["object_id"], claim_id=r["claim_id"],
                             citation=r["citation"]) for r in rows]

            paths = await find_paths(_neighbors, src["entity_id"], tgt["entity_id"],
                                     max_depth=max(1, min(int(max_depth or 4), 5)),
                                     max_paths=max(1, min(int(max_paths or 10), 25)))
            return {"enabled": True, "source": src, "target": tgt,
                    "paths": [p.to_dict() for p in paths]}
        except Exception:   # noqa: BLE001 — read-only surface must never error the caller
            return out

    @app.get("/connections")
    async def connections(entity: str = "", tenant_id: str = "demo", limit: int = 50):
        """Slice-1 edge model (flag ROSTER_EDGE_MODEL): an entity's 1-hop grounded network — its
        direct connections with the predicate + verbatim citation per edge. Read-only; `enabled`
        echoes the resolved flag so the FE renders to match the backend path."""
        enabled = os.environ.get("ROSTER_EDGE_MODEL", "").lower() in ("1", "true", "yes")
        out = {"enabled": enabled, "entity": None, "connections": []}
        if not enabled:
            return out
        store = _claim_store_cached()
        if store is None or not entity:
            return out
        try:
            ent = await store.find_entity(entity, tenant_id=tenant_id)
            if not ent:
                return out
            rows = await store.neighbors(ent["entity_id"], tenant_id=tenant_id,
                                         cap=max(1, min(int(limit or 50), 200)))
            conns = []
            for r in rows:
                peer = r["object_id"] if r["subject_id"] == ent["entity_id"] else r["subject_id"]
                conns.append({"peer_id": peer, "predicate": r["predicate"],
                              "direction": "out" if r["subject_id"] == ent["entity_id"] else "in",
                              "citation": r["citation"]})
            return {"enabled": True, "entity": ent, "connections": conns}
        except Exception:   # noqa: BLE001
            return out

    @app.get("/{name}.png")
    def web_png(name: str):
        """Serve a PNG asset from apps/web (logo, brand mark). Basename-only + .png
        guard → no path traversal; only files that exist in the web dir are served.
        Long-lived cache: the logos change ~never (and a stale logo is harmless)."""
        from fastapi.responses import FileResponse
        safe = os.path.basename(name) + ".png"
        f = _WEB_DIR / safe
        if not f.exists():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(str(f), media_type="image/png",
                            headers={"Cache-Control": "public, max-age=604800"})

    @app.post("/research", response_model=ResearchOut)
    async def research(body: ResearchIn,
                       x_roster_token: str = Header(default="")) -> ResearchOut:
        try:
            # People routing (flag ROSTER_PEOPLE_POPULATION) now lives INSIDE _do_research — the single
            # source of truth shared with /research/stream. Both endpoints get the people-only behavior.
            return await _do_research(body, token=x_roster_token)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail=(
                "No model available in replay mode. Set ROSTER_PROVIDER_MODE=live "
                "with ANTHROPIC_API_KEY + OPENAI_API_KEY to answer live, or record "
                "cassettes first.")) from e
        except Exception as e:   # provider errors (auth, credits, rate limit, timeout)
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e

    @app.post("/jobs")
    async def jobs(body: ResearchIn) -> dict:
        """JOBS MODE (flag ROSTER_JOBS): search open roles aggregated from public ATS boards
        (Greenhouse/Ashby/Lever) — LLM parses the query into company/title-keywords/location, code
        filters `rs_job`, each result carries an apply link. 404 when off."""
        if not jobs_enabled():
            raise HTTPException(status_code=404, detail="jobs mode not enabled")
        store = _claim_store_cached()
        if store is None:
            return {"jobs": [], "count": 0, "query": {}, "stats": {"jobs": 0, "companies": 0}}
        from api.people_population import parse_job_query, semantic_enabled, embed_query
        try:
            q = await parse_job_query(body.question, build_llm(mode=resolve_mode()))
        except Exception:   # noqa: BLE001
            q = {"company": [], "title_keywords": [], "location": ""}
        # SEMANTIC (flag): rank jobs by embedding similarity (optionally within the company filter);
        # else the exact title-keyword filter. Semantic understands 'jobs building ML infra', etc.
        qvec = embed_query(body.question) if semantic_enabled() else None
        if qvec:
            rows = await store.semantic_jobs(qvec, company=(q.get("company") or None), cap=80)
        else:
            rows = await store.search_jobs(
                terms=q.get("title_keywords") or [], company=q.get("company") or None,
                location=(q.get("location") or None), cap=80)
        stats = await store.jobs_stats()
        return {"jobs": rows, "count": len(rows), "query": q, "semantic": bool(qvec), "stats": stats}

    @app.post("/research/focus")
    async def research_focus(body: FocusIn, x_roster_token: str = Header(default="")) -> dict:
        """Push focus onto a SELECTED SPAN of a prior answer (flag-gated, Rule 20; 404 when off).
        'deeper' expands that specific point with FRESH focused retrieval; 'rethink' critically
        re-examines it with grounded counter-evidence/caveats. Both stay grounded (the same react
        span-check gate runs — every new claim is verbatim-cited or dropped).

        Design (judge-panel-reviewed): call `service.ask()` DIRECTLY — never `_do_research` (which
        re-derives engine/mode/history and could force the reasoned scaffold, structurally wrong for
        a single-span dive). history=None deliberately BYPASSES the follow-up resolver (so a 'rethink'
        can't be misrouted to operate-on-prior's no-retrieval reshape). The span rides in `question`
        (the only text that steers retrieval); the mode rides in `answer_format_override` (a compact
        directive that skips the diligence memo + closing block). `graph_question` carries the PRISTINE
        original question so the graph expander anchors on the real subject, not the span blob.
        Ephemeral — NOT persisted (span-anchoring on reopen is a known trap)."""
        if not focus_deepen_enabled():
            raise HTTPException(status_code=404, detail="focus/deepen not enabled")
        span = (body.span or "").strip()[:600]
        if not span:
            raise HTTPException(status_code=400, detail="span is required")
        mode = body.mode if body.mode in ("deeper", "rethink") else "deeper"
        q = (body.question or "").strip()
        if app.state.service is None:
            app.state.service = build_default_service()

        if mode == "rethink":
            frame = ('[The reader highlighted this specific claim from the analysis and wants it '
                     'CRITICALLY RE-EXAMINED — focus narrowly on it, in the context of the question above:]')
            directive = (
                "Write a TIGHT, GROUNDED critical re-examination of ONLY the highlighted claim — 2 to 4 "
                "short paragraphs. Surface counter-evidence, caveats, limitations, hidden assumptions, or "
                "alternative interpretations that the SOURCES actually support, and state whether the claim "
                "is well-supported, overstated, or uncertain. Every point MUST cite a retrieved finding [n]; "
                "do NOT manufacture a counterpoint — if the evidence does not contradict or qualify the "
                "claim, say so plainly (a well-supported claim is a valid conclusion). Do NOT restate the "
                "broader answer and do NOT add a closing section.")
        else:
            frame = ('[The reader highlighted this specific point from the analysis and wants it explored '
                     'in GREATER DEPTH — focus narrowly on it, in the context of the question above:]')
            directive = (
                "Write a TIGHT, FOCUSED deepening of ONLY the highlighted point — 2 to 4 short paragraphs "
                "or a few bullets. Add NEW, concrete, specific grounded detail the broader answer did not "
                "spell out: mechanisms, figures, named entities, dates, evidence, examples. Every sentence "
                "that states a fact MUST carry a citation [n]. Do NOT restate the broader answer, do NOT add "
                "a conclusion / summary / 'next steps' section, and do NOT drift to adjacent topics. If the "
                "corpus holds no deeper grounded detail on this specific point, say so plainly in one line "
                "rather than padding.")

        effective_q = (f"{q}\n\n{frame}\n\"{span}\"" if q else f"{frame}\n\"{span}\"")
        qc = (f"This deepens a prior analysis answering the question: {q}" if q else None)
        try:
            res = await app.state.service.ask(
                question=effective_q, tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                source_keys=body.sources, history=None,
                answer_focus=True, clarify=False,
                answer_format_override=directive,
                graph_question=(q or span),   # pristine subject → correct graph-expander anchor (R2)
                question_context=qc)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail=(
                "No model available in replay mode. Set ROSTER_PROVIDER_MODE=live with "
                "ANTHROPIC_API_KEY + OPENAI_API_KEY to answer live.")) from e
        except Exception as e:   # provider errors (auth, credits, rate limit, timeout)
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e

        ui = getattr(app.state.service, "ui", None)
        def _url(c):
            fn = getattr(ui, "source_url", None)
            try:
                return fn(c.document_id, c.quote) if fn and c.document_id else None
            except Exception:
                return None
        claims = [{"text": c.text, "quote": c.quote, "source": c.source_key,
                   "title": c.document_title, "url": _url(c), "document_id": c.document_id,
                   "source_kind": ((c.facets or {}).get("source_kind") or ""),
                   "tier": (c.evidence_kind or "")}
                  for c in res.verified_claims]
        return {"span": span, "mode": mode, "grounded": res.grounded,
                "answer": res.composed_answer, "claims": claims,
                "rejected": len(res.rejected_claims)}

    @app.post("/research/population")
    async def research_population(body: ResearchIn,
                                  x_roster_token: str = Header(default="")) -> dict:
        """Task 4 capstone route (flag-gated, Rule 20). When ROSTER_STARTUP_POPULATION is
        ON, answer a landscape/population question by AGGREGATING the grounded claim
        graph (population → market map → compose + derive) instead of a retrieval sample.
        SEPARATE from `_do_research` — the OFF path is byte-identical (404 when the flag
        is off, so this endpoint is a true no-op)."""
        if not startup_population_enabled():
            raise HTTPException(status_code=404, detail="startup population route not enabled")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="ROSTER_CORPUS_DSN not configured")
        if app.state.service is None:
            app.state.service = build_default_service()
        llm = app.state.service.llm
        from api.population_route import answer_from_population
        try:
            result = await answer_from_population(
                question=body.question, tenant_id=body.tenant_id, dsn=dsn, llm=llm)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail=(
                "No model available in replay mode. Set ROSTER_PROVIDER_MODE=live "
                "with ANTHROPIC_API_KEY + OPENAI_API_KEY to answer live, or record "
                "cassettes first.")) from e
        except Exception as e:   # provider errors (auth, credits, rate limit, timeout)
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        # Best-effort session persist (so the answer shows in /sessions). Never breaks
        # the response — a store/DB hiccup leaves session_id None and returns the answer.
        session_id = None
        if not result.get("error") and result.get("answer"):
            store = _store()
            if store is not None:
                try:
                    cb = result.get("coverage_basis") or {}
                    session_id = await store.save(
                        tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                        question=body.question, answer=result["answer"],
                        grounded=not result.get("empty_population", False),
                        claims=[], source_stats={},
                        coverage_gaps=[], rejected=0, sources=body.sources,
                        user_name=body.user_name, user_email=body.user_email,
                        kind="population",
                        extra={"population_stats": result.get("stats") or {},
                               "coverage_basis": cb,
                               "n_uncited_dropped": result.get("n_uncited_dropped", 0)})
                except Exception:
                    session_id = None
        result["session_id"] = session_id
        return result

    @app.post("/research/diligence")
    async def research_diligence(body: ResearchIn,
                                 x_roster_token: str = Header(default="")) -> dict:
        """Task D3 route (flag-gated, Rule 20). When ROSTER_DILIGENCE_DEPTH is ON, produce a
        GROUNDED single-company diligence brief organized BY DIMENSION by reading that one
        company's grounded claims from the graph (resolve → entity_claims → dimension-grouped
        compose + citation gate + derive). The subject company is `body.company` (name or
        entity_id). SEPARATE from `_do_research` and `/research/population` — the OFF path is
        byte-identical (404 when the flag is off, so this endpoint is a true no-op)."""
        if not diligence_depth_enabled():
            raise HTTPException(status_code=404, detail="diligence depth route not enabled")
        company = (body.company or "").strip()
        if not company:
            raise HTTPException(status_code=422, detail="company is required for diligence")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="ROSTER_CORPUS_DSN not configured")
        if app.state.service is None:
            app.state.service = build_default_service()
        llm = app.state.service.llm
        from api.diligence_route import answer_diligence
        try:
            result = await answer_diligence(
                company=company, tenant_id=body.tenant_id, dsn=dsn, llm=llm)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail=(
                "No model available in replay mode. Set ROSTER_PROVIDER_MODE=live "
                "with ANTHROPIC_API_KEY + OPENAI_API_KEY to answer live, or record "
                "cassettes first.")) from e
        except Exception as e:   # provider errors (auth, credits, rate limit, timeout)
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        # Best-effort session persist (so the answer shows in /sessions). Never breaks
        # the response — a store/DB hiccup leaves session_id None and returns the answer.
        session_id = None
        if not result.get("error") and result.get("answer"):
            store = _store()
            if store is not None:
                try:
                    cb = result.get("coverage_basis") or {}
                    session_id = await store.save(
                        tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                        question=f"Diligence: {company}", answer=result["answer"],
                        grounded=not result.get("empty", False),
                        claims=[], source_stats={},
                        coverage_gaps=[], rejected=0, sources=body.sources,
                        user_name=body.user_name, user_email=body.user_email,
                        kind="diligence",
                        extra={"diligence_stats": result.get("stats") or {},
                               "coverage_basis": cb,
                               "subject_id": result.get("subject_id"),
                               "n_uncited_dropped": result.get("n_uncited_dropped", 0)})
                except Exception:
                    session_id = None
        result["session_id"] = session_id
        return result

    @app.get("/crossviews/options")
    async def crossviews_options(row_kind: str = "company",
                                 category_norm: str = "",
                                 tenant_id: str = "demo") -> dict:
        """Task CV2 (flag-gated, Rule 20). The GROUNDED column catalog + filter options for
        the CROSSVIEWS designer: the allowed columns for `row_kind` (real active
        predicates / CV1 edge-aggregate set with coverage>0) plus the distinct grounded
        categories. OFF → 404 (true no-op)."""
        if not crossviews_enabled():
            raise HTTPException(status_code=404, detail="crossviews not enabled")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="ROSTER_CORPUS_DSN not configured")
        from api.claimgraph_tech import make_tech_claim_store
        from api.crossviews import VALID_ROW_KINDS as VALID_CROSSVIEW_ROW_KINDS
        from api.crossviews_agent import build_column_catalog, _public_columns
        store = make_tech_claim_store(dsn)
        try:
            cat = category_norm or None
            columns = await build_column_catalog(
                store, row_kind=row_kind, tenant_id=tenant_id, category_norm=cat)
            categories = await store.distinct_categories(tenant_id=tenant_id, min_members=1)
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"crossviews options error: {e}") from e
        finally:
            await store.close()
        return {
            "row_kinds": list(VALID_CROSSVIEW_ROW_KINDS),
            "columns": _public_columns(columns),
            "categories": [{"object_norm": c["object_norm"], "name": c["name"],
                            "members": c["members"]} for c in categories],
        }

    @app.post("/crossviews/agent")
    async def crossviews_agent(body: CrossviewAgentIn) -> dict:
        """Task CV2 (flag-gated). ONE goal-first, server-validated turn of the column-design
        conversation (`crossview_turn`): the LLM proposes columns ONLY from the grounded
        catalog; the server re-validation gate drops anything uncovered. OFF → 404."""
        if not crossviews_enabled():
            raise HTTPException(status_code=404, detail="crossviews not enabled")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="ROSTER_CORPUS_DSN not configured")
        if app.state.service is None:
            app.state.service = build_default_service()
        llm = app.state.service.llm
        from api.claimgraph_tech import make_tech_claim_store
        from api.crossviews_agent import crossview_turn
        store = make_tech_claim_store(dsn)
        try:
            return await crossview_turn(
                store=store, llm=llm, row_kind=body.row_kind, goal=body.goal,
                current_columns=body.current_columns, message=body.message,
                tenant_id=body.tenant_id, category_norm=body.category_norm)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail=(
                "No model available in replay mode. Set ROSTER_PROVIDER_MODE=live "
                "with ANTHROPIC_API_KEY + OPENAI_API_KEY to answer live, or record "
                "cassettes first.")) from e
        except Exception as e:   # provider errors (auth, credits, rate limit, timeout)
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        finally:
            await store.close()

    @app.post("/crossviews/build")
    async def crossviews_build(body: CrossviewBuildIn) -> dict:
        """Task CV2 (flag-gated). Build the grounded grid for a spec (`build_grid`).
        Read-only — every non-empty cell carries a citation or is `not_collected`. OFF → 404."""
        if not crossviews_enabled():
            raise HTTPException(status_code=404, detail="crossviews not enabled")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=503, detail="ROSTER_CORPUS_DSN not configured")
        from api.claimgraph_tech import make_tech_claim_store
        from api.crossviews import CrossviewSpec, build_grid
        store = make_tech_claim_store(dsn)
        try:
            return await build_grid(
                store, CrossviewSpec.coerce(body.spec), tenant_id=body.tenant_id)
        except Exception as e:   # noqa: BLE001 (build_grid is itself fail-safe; this guards store setup)
            raise HTTPException(status_code=502, detail=f"crossviews build error: {e}") from e
        finally:
            await store.close()

    @app.post("/crossviews/save")
    async def crossviews_save(body: CrossviewSaveIn) -> dict:
        """Task CV2 (flag-gated). Persist ONE crossview as a `kind='crossview'` session
        (spec + optional grid snapshot + transcript ride the JSONB thread turn). The spec
        is the source of truth; reopen re-queries live via /crossviews/build. Best-effort —
        a store hiccup returns `session_id: None`, never crashes. OFF → 404."""
        if not crossviews_enabled():
            raise HTTPException(status_code=404, detail="crossviews not enabled")
        store = _store()
        session_id = None
        if store is not None:
            spec = body.spec or {}
            cols = spec.get("columns") if isinstance(spec, dict) else None
            n_cols = len(cols) if isinstance(cols, list) else 0
            rk = spec.get("row_kind") if isinstance(spec, dict) else ""
            summary = f"Crossview: {rk or 'table'} — {n_cols} column(s)"
            try:
                session_id = await store.save(
                    tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                    question=body.title or f"Crossview ({rk or 'table'})",
                    answer=summary, grounded=True, claims=[], source_stats={},
                    coverage_gaps=[], rejected=0, sources=[],
                    kind="crossview",
                    extra={"crossview_spec": spec,
                           "crossview_grid": body.grid or {},
                           "crossview_transcript": body.transcript or []})
            except Exception:   # noqa: BLE001 — best-effort persistence
                session_id = None
        return {"session_id": session_id}

    @app.post("/panel/plan")
    async def panel_plan(body: PanelIn) -> dict:
        """Phase 1 (Convene): auto-select the specialists for this case + return the full roster (each
        with its lens/expertise) so the UI can show the proposed panel and let the user adjust."""
        if not await _flag_live("ask_panel_enabled"):
            raise HTTPException(status_code=404, detail="ask panel not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        try:
            return await app.state.service.plan_panel(question=body.question)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e

    def _with_urls(claims):
        """Attach the canonical source URL to each panel claim (same as the primary evidence panel) so
        the FE can render clickable links. claim dicts carry document_id + quote (from _vc_dict)."""
        ui = getattr(app.state.service, "ui", None)
        fn = getattr(ui, "source_url", None)
        out = []
        for c in (claims or []):
            u = None
            try:
                if fn and c.get("document_id"):
                    u = fn(c.get("document_id"), c.get("quote"))
            except Exception:   # noqa: BLE001
                u = None
            out.append({**c, "url": u})
        return out

    def _panel_payload(r, session_id=None) -> dict:
        return {
            "session_id": session_id,
            "question": r.question, "n_specialists": r.n_specialists,
            "takes": [{"id": t.id, "specialty": t.specialty, "answer": t.answer,
                       "grounded": t.grounded, "n_verified": t.n_verified, "error": t.error,
                       "rationale": getattr(t, "rationale", ""),
                       "claims": _with_urls(getattr(t, "claims", []))}
                      for t in r.takes],
            "synthesis": r.synthesis, "claims": _with_urls(r.claims),
            "interpretation": r.interpretation, "confidence": r.confidence,
            "reasoning_purpose": r.reasoning_purpose, "reasoning_conclusion": r.reasoning_conclusion,
            # Panel-level slots NO specialist evidenced (shared-contract flag; [] when OFF).
            "coverage_gaps": list(getattr(r, "coverage_gaps", []) or []),
        }

    def _panel_media(body):
        """Uploaded attachments → (images, documents, previews), gated by vision_enabled() — same as
        _do_research. Returns (None, None, []) when off or empty so the panel is byte-identical without vision."""
        if not (body.attachments and vision_enabled()):
            return None, None, None, []
        from api.media import attachments_to_media, session_previews
        images, docs, pdfs, _notes = attachments_to_media([a.model_dump() for a in body.attachments])
        return images, docs, pdfs, session_previews(
            images or [], (docs or []) + [{"name": x.get("name")} for x in (pdfs or [])])

    async def _persist_panel(body, r) -> str | None:
        """Best-effort persist of a panel turn as a SHAREABLE session (mirrors _do_research). A follow-up
        (session_id present) appends to the same thread; else a new row is created. kind='panel' so the
        reopen path renders the case conference. Never breaks the response."""
        store = _store()
        if store is None:
            return None
        payload = _panel_payload(r)
        pooled = payload["claims"]   # URL-enriched, so a reopened session keeps clickable links
        turn = {"kind": "panel", "question": r.question, "answer": r.synthesis,
                "grounded": bool(pooled), "claims": pooled, "takes": payload["takes"],
                "n_specialists": r.n_specialists, "interpretation": r.interpretation,
                "confidence": r.confidence, "reasoning_purpose": r.reasoning_purpose,
                "reasoning_conclusion": r.reasoning_conclusion,
                "coverage_gaps": payload["coverage_gaps"]}
        try:
            if body.session_id and await store.append_turn(body.session_id, turn):
                return body.session_id
            return await store.save(
                tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                question=r.question, answer=r.synthesis, grounded=bool(pooled),
                claims=pooled, source_stats={}, coverage_gaps=payload["coverage_gaps"],
                rejected=0, sources=body.sources,
                interpretation=r.interpretation, confidence=r.confidence,
                reasoning_purpose=r.reasoning_purpose, reasoning_conclusion=r.reasoning_conclusion,
                kind="panel", extra={"takes": payload["takes"], "n_specialists": r.n_specialists})
        except Exception:   # noqa: BLE001 — persistence must never break the panel response
            return None

    @app.post("/panel/ask")
    async def panel_ask(body: PanelIn) -> dict:
        """Ask-Panel (Alpha): convene the selected AI specialists (or the default set) — each runs its
        own grounded, lens-scoped research — and return each specialist's take + the synthesized panel.
        NOTE: a full panel runs for several minutes; browsers should use /panel/ask/stream, which keeps
        the connection alive with SSE keepalives (a plain POST this long is cut by the edge proxy → 502)."""
        if not await _flag_live("ask_panel_enabled"):
            raise HTTPException(status_code=404, detail="ask panel not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        images, docs, pdfs, _prev = _panel_media(body)
        try:
            r = await app.state.service.ask_panel(
                question=body.question, tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                specialist_ids=body.specialists or None, source_keys=body.sources, history=body.history,
                rationales=body.rationales, images=images, documents=docs, pdf_docs=pdfs)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        sid = await _persist_panel(body, r)
        return _panel_payload(r, session_id=sid or body.session_id)

    @app.post("/panel/ask/stream")
    async def panel_ask_stream(body: PanelIn):
        """Live SSE for a panel run: emits specialist_start / specialist_done progress as each lens runs,
        then a `final` event carrying the full panel payload. The keepalive `: ping` comments keep the
        edge proxy from cutting the (multi-minute) connection — the fix for the plain-POST 502."""
        if not await _flag_live("ask_panel_enabled"):
            raise HTTPException(status_code=404, detail="ask panel not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        from fastapi.responses import StreamingResponse
        run = _sse_run_new()

        async def on_event(ev: dict) -> None:
            _sse_push(run, ev)

        images, docs, pdfs, _prev = _panel_media(body)

        async def runner() -> None:
            # Runs to completion regardless of client connections — persists, and buffers every
            # event under the run_id so a cut connection resumes via GET /stream/{run_id}.
            try:
                r = await app.state.service.ask_panel(
                    question=body.question, tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                    specialist_ids=body.specialists or None, source_keys=body.sources,
                    history=body.history, rationales=body.rationales,
                    images=images, documents=docs, pdf_docs=pdfs, on_event=on_event)
                sid = await _persist_panel(body, r)
                _sse_push(run, {"type": "final", "result": _panel_payload(r, session_id=sid or body.session_id)})
            except CassetteMiss:
                _sse_push(run, {"type": "error", "detail": "No model available in replay mode."})
            except Exception as e:   # noqa: BLE001
                _sse_push(run, {"type": "error", "detail": f"provider error: {e}"})
            finally:
                _sse_done(run)

        run["task"] = asyncio.create_task(runner())

        async def gen():
            yield ": open\n\n"                            # flush headers immediately
            yield f"data: {json.dumps({'type': 'run', 'run_id': run['id']})}\n\n"
            async for chunk in _sse_follow(run, 0):
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    async def _do_research(body: ResearchIn, on_event=None, token: str = "") -> ResearchOut:
        """Shared research core: attachments → ask (optional live on_event) → persist → ResearchOut.
        Raises CassetteMiss / provider errors for the caller to handle."""
        # PEOPLE-ENUMERATION ROUTE (flag ROSTER_PEOPLE_POPULATION, Rule 20) — the SINGLE source of
        # truth for people routing, shared by /research AND /research/stream (both funnel through
        # here; the earlier /research-only fix missed /research/stream, the UI's real endpoint — the
        # recurring "still prose" bug). On the flag path EVERY question (fresh AND threaded) is
        # answered by FILTERING the grounded people index — NEVER web/prose: kind='person' → a profile
        # card; everything else → ranked people_rows or an honest coverage-gap answer.
        if people_population_enabled():
            store = _claim_store_cached()
            if store is None:
                return ResearchOut(grounded=False, answer="The people index is unavailable right now "
                                   "— please retry.", claims=[], coverage_gaps=[], rejected=0,
                                   people_rows=[], coverage_basis=None)
            from api.people_population import answer_people_population
            # GEO SCOPE (flag ROSTER_PEOPLE_GEO_SCOPE, default OFF): restrict to ONE country (selector
            # default 'us'); a query-named country overrides it inside the engine. OFF → "" = no filter.
            scope_country = (body.country or "us").strip().lower() if people_geo_scope_enabled() else ""
            res = await answer_people_population(
                question=body.question, tenant_id=body.tenant_id,
                store=store, llm=build_llm(mode=resolve_mode()), scope_country=scope_country)
            if res.get("kind") == "person":
                if on_event is not None:
                    await on_event({"type": "people", "count": 1})
                return ResearchOut(grounded=True, answer="", claims=[], coverage_gaps=[],
                                   rejected=0, people_rows=[res["person_card"]],
                                   coverage_basis=None, session_id=None)
            answer = res.get("answer") or ("No people matched — name a role, expertise, company, "
                                           "or location (e.g. 'ML directors in NYC').")
            if on_event is not None:
                await on_event({"type": "people", "count": len(res.get("people_rows") or [])})
            sid = None
            sstore = _store()
            if sstore is not None:
                try:
                    sid = await sstore.save(
                        tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                        question=body.question, answer=answer,
                        grounded=res.get("grounded", False), claims=[], source_stats={},
                        coverage_gaps=[], rejected=0, sources=body.sources,
                        user_name=body.user_name, user_email=body.user_email,
                        kind="people_population",
                        extra={"coverage_basis": res.get("coverage_basis") or {},
                               "people_rows": res.get("people_rows") or []})
                except Exception:   # noqa: BLE001
                    sid = None
            return ResearchOut(
                grounded=res.get("grounded", False), answer=answer, claims=[],
                coverage_gaps=[], rejected=0, people_rows=res.get("people_rows") or [],
                coverage_basis=res.get("coverage_basis"), session_id=sid)
        if app.state.service is None:
            app.state.service = build_default_service()
        images, docs, pdfs, attach_notes, previews = None, None, None, [], []
        if body.attachments and vision_enabled():
            from api.media import attachments_to_media, session_previews
            images, docs, pdfs, attach_notes = attachments_to_media(
                [a.model_dump() for a in body.attachments])
            previews = session_previews(images or [], (docs or []) + [{"name": x.get("name")} for x in (pdfs or [])])
        history = body.history if conversation_enabled() else None
        # Effort is HONORED only when the flag is on; otherwise forced to 1.0 (byte-identical no-op).
        effort = body.effort if effort_scale_enabled() else 1.0
        # Audience is HONORED only when the flag is on; otherwise forced 'clinician' (byte-identical).
        audience = _resolve_audience(body.audience)
        if on_event is not None and effort > 1.0:
            await on_event({"type": "effort", "effort": effort})
        if on_event is not None and audience == "patient":
            await on_event({"type": "audience", "audience": audience})
        # Answer-focus: condense elliptical follow-ups + answer-scope compose (needs the flag; the
        # condense half additionally needs conversation history, which `history` above already gates).
        focus = answer_focus_enabled()
        # A/B duel arm: engine="reasoned" routes through the alternate scaffold+decision-gated engine.
        # Flag off (or unknown engine) → plain ask, param ignored (byte-identical, Rule 20).
        # Engine resolution — explicit values (duel arms + interlock hop chips) force a pipeline;
        # UNSET + dynamic-selection on → the scaffold call routes per question kind.
        import functools as _ft
        _eng = (body.engine or "").strip()
        _dyn = await _flag_live("reasoned_default_enabled")
        if _eng == "standard":
            _ask = app.state.service.ask
        elif _eng == "understanding" and _dyn:
            _ask = _ft.partial(app.state.service.ask_reasoned, route=False, force_kind="understanding")
        elif _eng == "reasoned" and (_dyn or await _flag_live("duel_enabled")):
            _ask = _ft.partial(app.state.service.ask_reasoned, route=False)
        elif not _eng and _dyn:
            _ask = app.state.service.ask_reasoned          # auto: the question picks the engine
        else:
            _ask = app.state.service.ask
        # per-question integrative opt-in (double opt-in: live flag AND body.integrative). Steers the
        # search (question hint) + appends the section directive; persisted question stays the original.
        _q, _extra = body.question, None
        if body.integrative and await _flag_live("integrative_enabled"):
            svc_ = app.state.service
            _extra = getattr(svc_, "integrative_prompt", None)
            hint = getattr(svc_, "integrative_query_hint", None)
            if hint:
                _q = body.question + "\n\n[" + hint + "]"
        # Analytical MODE (e.g. acquirer / M&A) or USE-CASE LENS (wisdom/foresight/genesis/market/
        # whitespace/moat): re-lens the same grounded evidence for that audience. A mode value is EITHER
        # a plain directive string (back-compat, e.g. acquirer) OR a dict {directive, suppress_authority}
        # — the lens dict can also neutralize the authority tier-boost so opinion/discussion evidence
        # isn't demoted below filings on a foresight/wisdom question.
        _mode = (body.mode or "").strip().lower()
        _mode_suppress = False
        if _mode:
            _modes = getattr(load_active_vertical(), "answer_modes", {}) or {}
            _mode_val = _modes.get(_mode)
            _mode_dir = _mode_val.get("directive") if isinstance(_mode_val, dict) else _mode_val
            if isinstance(_mode_val, dict):
                _mode_suppress = bool(_mode_val.get("suppress_authority"))
            if _mode_dir:
                _extra = (_extra + "\n\n" + _mode_dir) if _extra else _mode_dir
        # QUERY EXPANSION (flag): enrich a terse question with a coverage brief (aspects + keywords) so
        # retrieval covers what a COMPLETE answer needs, regardless of phrasing. Augments the query text
        # only (steers planner + embedding, adds no facts); the PRISTINE question rides graph_question so
        # the graph expander still anchors on the asked subject. Best-effort — never blocks the answer.
        _graph_q = None
        if query_expansion_enabled() and body.question.strip():
            try:
                from api.query_expansion import expand_query, brief_text
                _exp = await expand_query(app.state.service.llm, body.question)
                if _exp:
                    _q = _q + brief_text(_exp)
                    _graph_q = body.question
                    if on_event is not None:
                        await on_event({"type": "query_expanded",
                                        "aspects": len(_exp.get("aspects") or []),
                                        "keywords": len(_exp.get("keywords") or [])})
            except Exception:
                _graph_q = None
        res = await _ask(
            question=_q, tenant_id=body.tenant_id,
            workspace_id=body.workspace_id, source_keys=body.sources,
            images=images, documents=docs, pdf_docs=pdfs, history=history, on_event=on_event,
            effort=effort, audience=audience, graph_question=_graph_q,
            answer_focus=focus, clarify=followup_clarify_enabled(), extra_directive=_extra,
            suppress_authority=_mode_suppress)
        # Ambiguous follow-up → return the clarifying question; no research ran, nothing to persist.
        if getattr(res, "clarification", ""):
            return ResearchOut(grounded=False, answer="", claims=[], coverage_gaps=[], rejected=0,
                               clarification=res.clarification)
        ui = getattr(app.state.service, "ui", None)
        def _url(c):
            fn = getattr(ui, "source_url", None)
            try:
                return fn(c.document_id, c.quote) if fn and c.document_id else None
            except Exception:
                return None
        claims = [Citation(text=c.text, quote=c.quote, atom_id=c.atom_id,
                           source=c.source_key, title=c.document_title,
                           url=_url(c), document_id=c.document_id,
                           source_kind=((c.facets or {}).get("source_kind") or ""),
                           tier=(c.evidence_kind or ""))
                  for c in res.verified_claims]
        # Answer add-ons (flag-gated, best-effort) — computed BEFORE persistence so a reopened
        # session shows them (they ride the thread turn, not just the live response):
        #  · related_research: a SEPARATE facet-filtered search over papers/preprints/filings.
        #  · companies: LLM-detected company mentions → grounded page (known) or web search (unknown).
        related = []
        if related_research_enabled() and res.grounded and body.question:
            try:
                from api.related_research import find_related_research
                related = await find_related_research(
                    app.state.service, question=body.question, answer=res.composed_answer,
                    tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                    ui=getattr(app.state.service, "ui", None))
            except Exception:
                related = []
        companies = []
        if company_links_enabled() and res.grounded and res.composed_answer:
            try:
                from api.company_links import detect_and_resolve_companies
                companies = await detect_and_resolve_companies(
                    app.state.service, _claim_store_cached(), answer=res.composed_answer,
                    ui=getattr(app.state.service, "ui", None), tenant_id=body.tenant_id)
            except Exception:
                companies = []
        # Persist the Q&A (best-effort). Conversation follow-up (session_id + flag) APPENDS a turn.
        session_id = None
        store = _store()
        claim_dicts = [c.model_dump() for c in claims]
        if store is not None:
            turn = {"question": body.question, "answer": res.composed_answer,
                    "grounded": res.grounded, "claims": claim_dicts,
                    "source_stats": res.source_stats, "coverage_gaps": res.coverage_gaps,
                    "rejected": len(res.rejected_claims),
                    "visual_observation": res.visual_observation, "attachments": previews}
            if effort_scale_enabled():
                turn["effort"] = res.effort    # per-turn badge on the session (JSONB, no migration)
            if patient_mode_enabled():
                turn["audience"] = audience    # per-turn audience tag (only under the flag)
            if answer_charts_enabled() and getattr(res, "charts", None):
                turn["charts"] = res.charts    # persist grounded charts so a reopened session shows them
            if reasoning_read_enabled():
                if getattr(res, "interpretation", None):
                    turn["interpretation"] = res.interpretation   # persist the reasoning layer (JSONB)
                if getattr(res, "confidence", None):
                    turn["confidence"] = res.confidence
                if getattr(res, "reasoning_purpose", ""):
                    turn["reasoning_purpose"] = res.reasoning_purpose
                if getattr(res, "reasoning_conclusion", ""):
                    turn["reasoning_conclusion"] = res.reasoning_conclusion
            if diag_trace_enabled() and getattr(res, "diagnostics", None):
                turn["diagnostics"] = res.diagnostics   # persist the trace for later troubleshooting
            if getattr(res, "question_contract", None):
                # Schema-registry phase 0: persist the derived QuestionContract (mode/entities/axes)
                # on the session turn (JSONB thread — additive field, no migration). Only present
                # when a contract was actually derived (ROSTER_QUESTION_CONTRACT shadow/steer).
                turn["question_contract"] = res.question_contract
            if getattr(res, "web_providers", None):
                turn["web_providers"] = res.web_providers   # search-source attribution (additive JSONB)
            # Audit field (additive JSONB): when the question came from Guided intake, the intake
            # CONVERSATION transcript (shown only to a logged-in admin).
            _extra = {}
            if getattr(body, "intake_transcript", None):
                _extra["intake_transcript"] = [
                    {"role": (m.get("role") or "")[:12], "text": (m.get("text") or "")[:2000]}
                    for m in (body.intake_transcript or [])[:40] if isinstance(m, dict) and m.get("text")]
            if related:                       # persist so a reopened session shows the section
                _extra["related_research"] = related
            if companies:                     # persist so a reopened session re-links the prose
                _extra["companies"] = companies
            if getattr(res, "people_profiles", None):
                _extra["people"] = res.people_profiles
            turn.update(_extra)
            try:
                # Audience-guarded append: only continue a thread whose audience MATCHES this turn's
                # (mid-thread toggle → mismatch → save a fresh session instead of corrupting the thread).
                if conversation_enabled() and body.session_id and \
                        await store.append_turn(body.session_id, turn, audience=audience):
                    session_id = body.session_id
                else:
                    session_id = await store.save(
                        tenant_id=body.tenant_id, workspace_id=body.workspace_id,
                        question=body.question, answer=res.composed_answer,
                        grounded=res.grounded, claims=claim_dicts,
                        source_stats=res.source_stats, coverage_gaps=res.coverage_gaps,
                        rejected=len(res.rejected_claims), sources=body.sources,
                        user_name=body.user_name, user_email=body.user_email,
                        visual_observation=res.visual_observation, attachments=previews,
                        audience=audience,
                        charts=(res.charts if answer_charts_enabled() else None),
                        interpretation=(getattr(res, "interpretation", None) if reasoning_read_enabled() else None),
                        confidence=(getattr(res, "confidence", None) if reasoning_read_enabled() else None),
                        reasoning_purpose=(getattr(res, "reasoning_purpose", "") if reasoning_read_enabled() else ""),
                        reasoning_conclusion=(getattr(res, "reasoning_conclusion", "") if reasoning_read_enabled() else ""),
                        diagnostics=(getattr(res, "diagnostics", None) if diag_trace_enabled() else None),
                        question_contract=getattr(res, "question_contract", None),
                        web_providers=(getattr(res, "web_providers", {}) or None),
                        extra=(_extra or None))
            except Exception:
                session_id = None
        # perf metrics (best-effort): distill a compact row from the diagnostics for the admin dashboard
        _pdiag = getattr(res, "diagnostics", None)
        if _pdiag:
            _perf_store = _perf()
            if _perf_store is not None:
                try:
                    from api.perf import event_from_diagnostics
                    await _perf_store.record(event_from_diagnostics(
                        _pdiag, kind="qa", grounded=res.grounded, claims=len(claims),
                        rejected=len(res.rejected_claims), stopped_reason=res.stopped_reason,
                        effort=(res.effort if effort_scale_enabled() else None),
                        audience=audience))
                except Exception:
                    pass
        return ResearchOut(
            grounded=res.grounded, answer=res.composed_answer, claims=claims,
            coverage_gaps=res.coverage_gaps, rejected=len(res.rejected_claims),
            source_stats=res.source_stats, session_id=session_id,
            stopped_reason=res.stopped_reason, atoms_gathered=res.atoms_gathered,
            retried_empty=res.retried_empty, visual_observation=res.visual_observation,
            attachment_notes=attach_notes,
            effort=res.effort if effort_scale_enabled() else None,
            audience=audience if patient_mode_enabled() else None,
            resolved_question=(res.resolved_question or None) if answer_focus_enabled() else None,
            derived_from_prior=bool(getattr(res, "derived_from_prior", False)),
            charts=(getattr(res, "charts", []) or []) if answer_charts_enabled() else [],
            interpretation=(getattr(res, "interpretation", []) or []) if reasoning_read_enabled() else [],
            confidence=(getattr(res, "confidence", None) if reasoning_read_enabled() else None),
            reasoning_purpose=(getattr(res, "reasoning_purpose", "") if reasoning_read_enabled() else ""),
            reasoning_conclusion=(getattr(res, "reasoning_conclusion", "") if reasoning_read_enabled() else ""),
            derivations=([{"label": d.label, "kind": d.kind, "conclusion": d.conclusion,
                           "basis": list(d.basis), "falsifier": d.falsifier}
                          for d in (getattr(res, "derivations", []) or [])] if derive_enabled() else []),
            diagnostics=(getattr(res, "diagnostics", None) if diag_trace_enabled() else None),
            question_contract=getattr(res, "question_contract", None),
            web_providers=(getattr(res, "web_providers", {}) or {}),
            freshness=(getattr(res, "freshness", None) if freshness_ranking_enabled() else None),
            related_research=related,
            companies=companies,
            people=(getattr(res, "people_profiles", []) or []),
            reflection=(getattr(res, "reflection", {}) or {}),
            unverified_priors=((getattr(res, "unverified_priors", []) or [])
                               if parametric_led_enabled() else []),
        )

    @app.post("/research/stream")
    async def research_stream(body: ResearchIn, x_roster_token: str = Header(default="")):
        """Live SSE progress for a research request: emits step/search/found/verifying/composing
        events as the ReAct loop runs, then a `final` event carrying the full ResearchOut. Progress
        events are read-only (never unverified claims); persistence + the final payload happen once
        at the end, exactly like /research."""
        if not stream_enabled():
            raise HTTPException(status_code=404, detail="streaming not enabled")
        from fastapi.responses import StreamingResponse
        run = _sse_run_new()

        async def on_event(ev: dict) -> None:
            _sse_push(run, ev)

        async def runner() -> None:
            # Runs to completion regardless of client connections — the session PERSISTS server-side,
            # and every event lands in the run buffer for any number of (re)connecting readers.
            try:
                out = await _do_research(body, on_event=on_event, token=x_roster_token)
                _sse_push(run, {"type": "final", "result": out.model_dump()})
            except CassetteMiss:
                _sse_push(run, {"type": "error", "detail": "No model available in replay mode."})
            except Exception as e:   # noqa: BLE001
                _sse_push(run, {"type": "error", "detail": f"provider error: {e}"})
            finally:
                _sse_done(run)

        run["task"] = asyncio.create_task(runner())

        async def gen():
            yield ": open\n\n"                            # flush headers immediately
            yield f"data: {json.dumps({'type': 'run', 'run_id': run['id']})}\n\n"
            async for chunk in _sse_follow(run, 0):
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    @app.get("/stream/{run_id}")
    async def stream_resume(run_id: str, since: int = 0):
        """Resume a live or recently-finished streaming run (research, panel, or stream-test) from
        event cursor `since` — the FE's silent-reconnect path for when the edge cuts an SSE
        connection mid-run. 404 = this replica never saw the run (or it expired); the FE retries
        (re-rolling the replica) and falls back to /sessions polling."""
        run = _SSE_RUNS.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="unknown run")
        from fastapi.responses import StreamingResponse

        async def gen():
            yield ": open\n\n"
            async for chunk in _sse_follow(run, since):
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    @app.post("/explain")
    async def explain(body: ExplainIn) -> dict:
        """On-demand plain-language re-explanation of a grounded answer (same doctor →
        patient). Saved on the session when a session_id is given."""
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        if not getattr(svc, "layman_prompt", None):
            raise HTTPException(status_code=404, detail="plain-language explanation not available")
        try:
            text = await svc.explain(question=body.question, answer=body.answer)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        if body.session_id and text:
            store = _store()
            if store is not None:
                try:
                    await store.save_layman(body.session_id, text)
                except Exception:
                    pass
        return {"explanation": text}

    @app.post("/suggest")
    async def suggest(body: SuggestIn) -> dict:
        """On-demand suggested follow-up questions for deeper discovery (conversation feature).
        Called after an answer renders, so it never adds latency to the answer itself."""
        if not conversation_enabled():
            raise HTTPException(status_code=404, detail="suggestions not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        if not getattr(svc, "suggest_prompt", None):
            raise HTTPException(status_code=404, detail="suggestions not available for this vertical")
        hist = ""
        if body.history:
            hist = "\n\n".join(
                f"Q: {(t.get('question') or '').strip()}" for t in body.history if t.get("question"))
        try:
            qs = await svc.suggest(question=body.question, answer=body.answer, history=hist)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        return {"suggestions": qs}

    @app.post("/voice/tts")
    async def voice_tts(body: VoiceTtsIn):
        """Neural voiceover for the guided-intake voice loop: the clarifying question as warm,
        unhurried male speech (OpenAI gpt-4o-mini-tts, voice 'ash'). The FE falls back to the
        browser's local voice when this endpoint is unavailable. Text in, audio/mpeg out —
        no user audio is ever received here (STT stays entirely in the browser)."""
        if not voice_intake_enabled():
            raise HTTPException(status_code=404, detail="voice intake not enabled")
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise HTTPException(status_code=404, detail="neural voice not configured")
        text = (body.text or "").strip()[:1500]   # intake questions are short; cap the spend
        if not text:
            raise HTTPException(status_code=400, detail="empty text")
        try:
            if getattr(app.state, "tts_client", None) is None:
                from openai import AsyncOpenAI
                app.state.tts_client = AsyncOpenAI(api_key=key)
            resp = await app.state.tts_client.audio.speech.create(
                model="gpt-4o-mini-tts", voice="ash", input=text, response_format="mp3",
                instructions=(
                    "Speak as a warm, unhurried, reassuring analyst talking with someone "
                    "seeking answers: calm male register, gentle pace with natural pauses, kind "
                    "and steady. Never rushed, never chirpy, never salesy."))
            audio = getattr(resp, "content", None)
            if not isinstance(audio, (bytes, bytearray)):
                audio = await resp.aread()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"tts error: {e}") from e
        from fastapi.responses import Response as _BinResp
        return _BinResp(content=bytes(audio), media_type="audio/mpeg",
                        headers={"Cache-Control": "no-store"})

    @app.post("/terms/explain")
    async def terms_explain(body: TermsIn) -> dict:
        """On-demand key-term explanations for an answer (definitional — purpose, application,
        related-term edges). User-triggered after the answer renders; every result accumulates
        into the vertical's glossary (the All Terms web)."""
        if not term_glossary_enabled():
            raise HTTPException(status_code=404, detail="term glossary not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        if not getattr(svc, "terms_prompt", None):
            raise HTTPException(status_code=404, detail="term explanations not available for this vertical")
        try:
            terms = await svc.explain_terms(question=body.question, answer=body.answer)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        out = [t.model_dump() for t in terms]
        total = None
        g = _glossary()
        if g is not None and out:
            try:
                await g.upsert_many(out, session_id=body.session_id)
                total = await g.count()
            except Exception:
                pass    # accumulation is best-effort; the explanations still render
        if body.session_id and out:
            store = _store()
            if store is not None:
                try:
                    # persist per-TURN (like visuals) so the right answer re-renders them on reopen,
                    # in Q&A AND Panel; also keep the session-level column for back-compat.
                    await store.save_turn_terms(body.session_id, body.turn_index or 0, out)
                    await store.save_terms(body.session_id, out)
                except Exception:
                    pass
        return {"terms": out, "glossary_total": total}

    @app.post("/visuals/augment")
    async def visuals_augment(body: VisualsIn) -> dict:
        """On-demand conceptual visuals (flow/tree/timeline) restructuring a grounded answer, every
        element quote-anchored to the answer. User-triggered after the answer renders; persisted onto
        the specific thread TURN so a reopened session re-renders them on the right answer."""
        if not visual_augment_enabled():
            raise HTTPException(status_code=404, detail="add-visuals not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        if not getattr(svc, "visuals_prompt", None):
            raise HTTPException(status_code=404, detail="visuals not available for this vertical")
        try:
            visuals = await svc.visualize(question=body.question, answer=body.answer)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        if body.session_id and visuals:
            store = _store()
            if store is not None:
                try:                       # attach to the exact turn (multi-turn correctness)
                    await store.save_turn_visuals(body.session_id, body.turn_index or 0, visuals)
                except Exception:
                    pass
        return {"visuals": visuals}

    @app.post("/glossary/lookup")
    async def glossary_lookup(body: TermLookupIn) -> dict:
        """Navigate the term web: return the stored entry for a term, or — when the term was
        linked as 'related' but never explained — explain it NOW and add it to the glossary.
        Browsing deepens the web."""
        if not term_glossary_enabled():
            raise HTTPException(status_code=404, detail="term glossary not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        g = _glossary()
        if g is not None:
            try:
                got = await g.get(body.term)
                if got:
                    return {"entry": got, "fresh": False}
            except Exception:
                pass
        if not getattr(svc, "terms_prompt", None):
            raise HTTPException(status_code=404, detail="term explanations not available for this vertical")
        try:
            t = await svc.explain_term(term=body.term, context=body.context)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        if t is None:
            raise HTTPException(status_code=404, detail="no explanation produced")
        entry = t.model_dump()
        if g is not None:
            try:
                await g.upsert_many([entry], session_id=None)
                stored = await g.get(entry["term"])
                if stored:
                    return {"entry": stored, "fresh": True}
            except Exception:
                pass
        entry["related"] = [{"term": s, "known": False} for s in (entry.get("related") or [])]
        return {"entry": entry, "fresh": True}

    @app.get("/glossary")
    async def glossary_list(q: str | None = None, letter: str | None = None,
                            limit: int = 500, offset: int = 0) -> dict:
        """The All Terms page: browse/search the accumulated vocabulary web."""
        if not term_glossary_enabled():
            raise HTTPException(status_code=404, detail="term glossary not enabled")
        g = _glossary()
        if g is None:
            return {"terms": [], "total": 0, "letters": {}}
        try:
            return await g.list(q=q, letter=letter, limit=max(1, min(limit, 1000)),
                                offset=max(0, offset))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"glossary unavailable: {e}") from e

    @app.post("/refine")
    async def refine(body: RefineIn) -> dict:
        """Pre-answer question refinement: propose a few distinct sharper standalone questions to pick
        from. Returns {"refinements": []} when the question is already precise (so the FE just answers
        it), when the flag/vertical is off, or on any provider error — never a dead-end."""
        if not refine_enabled():
            return {"refinements": []}
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        if not getattr(svc, "refine_prompt", None):
            return {"refinements": []}
        try:
            opts = await svc.refine(question=body.question)
        except CassetteMiss:
            return {"refinements": []}     # replay mode → no refinement, answer the original
        except Exception:                  # provider error → fail open (answer the original)
            return {"refinements": []}
        return {"refinements": opts}

    @app.post("/triage/step")
    async def triage_step(body: TriageIn) -> dict:
        """Guided-intake / triage: one clarifying turn. Given the running transcript, return either the
        next clarifying question (status="ask") or a crisp refined question + recommended route
        (status="qa"|"panel", via `recommended_mode`) when ready. Stateless — the FE holds the transcript.
        404 when the flag/vertical is off. Never answers the medical question; only narrows + routes.

        Convergence is code-guaranteed: once the assistant has already asked TRIAGE_MAX_ASK questions
        (per-register backstop under ROSTER_INTAKE_V2: fact=TRIAGE_MAX_ASK, case=TRIAGE_MAX_ASK_CASE),
        this turn is FORCED to route (the LLM still owns whether/what to ask below that cap — Rule 18).
        `wrap_up` (the user's explicit "search now") forces a route on any turn."""
        if not await _flag_live("triage_enabled"):
            raise HTTPException(status_code=404, detail="triage mode is not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        if not getattr(svc, "triage_prompt", None):
            raise HTTPException(status_code=404, detail="triage mode is not enabled")
        transcript = [t for t in (body.transcript or []) if isinstance(t, dict) and (t.get("text") or "").strip()]
        if not transcript:
            raise HTTPException(status_code=400, detail="transcript is empty")
        v2 = intake_v2_enabled()
        asked = sum(1 for t in transcript if (t.get("role") or "") == "assistant")
        force_ready = bool(body.wrap_up) or asked >= triage_ask_cap(v2, (body.register or "").strip().lower())
        try:
            return await svc.triage(transcript=transcript, force_ready=force_ready, v2=v2)
        except CassetteMiss:
            # replay mode → route the last user message straight to Q&A (never dead-end)
            last = next((t["text"] for t in reversed(transcript) if t.get("role") == "user"), "")
            return {"status": "ready", "recommended_mode": "qa", "refined_question": last,
                    "understood_problem": last, "message": "Searching that now.", "safety": "ok"}
        except Exception as e:   # noqa: BLE001 — never dead-end the user
            raise HTTPException(status_code=502, detail=f"triage error: {e}") from e

    @app.post("/corpus/gap-plan")
    async def corpus_gap_plan(body: GapPlanIn) -> dict:
        """On-demand: what to ADD to the corpus so an under-evidenced question could be answered.
        LLM-proposed ingest jobs (over this deployment's connectors) + gold-source recommendations.
        Read-only — proposes; it does not queue or ingest anything."""
        if not gap_healing_enabled():
            raise HTTPException(status_code=404, detail="gap healing not enabled")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        try:
            plan = await svc.plan_gaps(
                question=body.question, answer=body.answer, coverage_gaps=body.coverage_gaps)
        except CassetteMiss as e:
            raise HTTPException(status_code=503, detail="No model available in replay mode.") from e
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"provider error: {e}") from e
        if plan is None:
            raise HTTPException(status_code=404, detail="gap healing not available for this vertical")
        return {
            "summary": plan.summary,
            "jobs": [j.model_dump() for j in plan.jobs],
            "recommendations": [r.model_dump() for r in plan.recommendations],
            "connectors": list(svc.connectors.keys()),
        }

    @app.post("/corpus/queue")
    async def corpus_queue_add(body: GapQueueIn) -> dict:
        """Queue user-approved gap-fill jobs. Validates every job against the real connector set
        and caps the limit (code owns structure) before persisting for the background processor."""
        if not gap_healing_enabled():
            raise HTTPException(status_code=404, detail="gap healing not enabled")
        q = _gap_queue()
        if q is None:
            raise HTTPException(status_code=404, detail="no corpus queue configured")
        if app.state.service is None:
            app.state.service = build_default_service()
        allowed = set(app.state.service.connectors.keys())
        clean = []
        for j in body.jobs or []:
            c = (j.get("connector") or "").strip()
            query = (j.get("query") or "").strip()
            if c not in allowed or not query:
                continue
            clean.append({
                "connector": c, "query": query,
                "limit": max(1, min(int(j.get("limit") or 200), 400)),
                "kind": (j.get("kind") or "")[:80],
                "rationale": (j.get("rationale") or "")[:400],
                "quality": (j.get("quality") or "")[:120],
            })
        if not clean:
            raise HTTPException(status_code=400, detail="no valid jobs (unknown connector or empty query)")
        try:
            ids = await q.enqueue(tenant_id=body.tenant_id, question=body.question, jobs=clean)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"queue error: {e}") from e
        return {"queued": len(ids), "ids": ids}

    @app.get("/corpus/queue")
    async def corpus_queue_status(limit: int = 50) -> dict:
        """Gap-fill queue status (pending/running/done/failed + blocks added) — self-healing progress."""
        q = _gap_queue()
        if q is None:
            return {"enabled": gap_healing_enabled(), "jobs": [], "summary": {}}
        try:
            return {"enabled": True, "jobs": await q.list(limit=min(limit, 200)),
                    "summary": await q.summary()}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"queue error: {e}") from e

    @app.post("/admin/corpus/ingest")
    async def admin_corpus_ingest(body: CorpusIngestIn, x_admin_token: str = Header(default="")) -> dict:
        """Bulk prod-direct ingest — replaces 'download locally + push to prod'. Validates the
        supplied connector jobs against the real connector set and enqueues them for the prod
        processor. Guarded by ROSTER_ADMIN_TOKEN when set (this endpoint spends credits + mutates
        the corpus)."""
        if not gap_healing_enabled():
            raise HTTPException(status_code=404, detail="corpus ingestion not enabled")
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="admin token required")
        q = _gap_queue()
        if q is None:
            raise HTTPException(status_code=404, detail="no corpus queue configured")
        if app.state.service is None:
            app.state.service = build_default_service()
        allowed = set(app.state.service.connectors.keys())
        cap = lambda n, d: max(1, min(int(n or d), 400))
        jobs: list[dict] = []
        for j in body.jobs or []:
            c = (j.get("connector") or "").strip()
            query = (j.get("query") or "").strip()
            if c in allowed and query:
                _jf = j.get("facets") if isinstance(j.get("facets"), dict) else {}
                _jp = j.get("params") if isinstance(j.get("params"), dict) else {}
                jobs.append({"connector": c, "query": query, "limit": cap(j.get("limit"), 200),
                             "kind": (j.get("kind") or "")[:80], "quality": (j.get("quality") or "")[:120],
                             "facets": {str(k): str(v) for k, v in (_jf or {}).items() if v},
                             "params": _jp,
                             # higher = claimed first (strategic sources jump the FIFO backlog); clamp 0..1000
                             "priority": max(0, min(int(j.get("priority") or 0), 1000))})
        if not jobs:
            raise HTTPException(status_code=400, detail="no valid jobs (unknown connector or empty inputs)")
        # a batch-level source_country stamps every job's blocks (per-job override wins)
        sc = (body.source_country or "").strip()
        if sc:
            for jb in jobs:
                jb.setdefault("source_country", sc)
        try:
            ids = await q.enqueue(tenant_id="demo", question="admin bulk ingest", jobs=jobs)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"queue error: {e}") from e
        return {"queued": len(ids), "jobs": len(jobs)}

    @app.post("/admin/corpus/ingest-pdf")
    async def admin_corpus_ingest_pdf(body: PdfIngestIn, x_admin_token: str = Header(default="")) -> dict:
        """LOCAL→PROD full-text bridge. arXiv rate-limits the PROD IP on PDF fetches, so old papers with
        no HTML degrade to abstract-only. A good-IP LOCAL box downloads the PDF and POSTs the bytes here;
        prod runs its already-working docling + the NORMAL ingest pipeline. Because the ingest derives
        document_id = f"{source_key}:{native_id}", passing the arxiv id REPLACES (clean-replace) that
        paper's abstract stub with full text — recovery, not duplication. Admin + flag gated; mutates
        the corpus + spends embedding credits."""
        if not pdf_bridge_enabled():
            raise HTTPException(status_code=404, detail="pdf bridge not enabled")
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="admin token required")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=404, detail="no corpus dsn configured")
        import base64 as _b64
        from roster_kernel.providers.base import resolve_mode
        from roster_kernel.retrieval.postgres import PostgresRetrievalSource
        from roster_kernel.runtime.build import build_embedder
        from roster_kernel.runtime.ingest import ingest_connector_to_postgres
        from roster_kernel.runtime.preparsed import PreParsedConnector
        # cache the pool + embedder across requests (the driver POSTs many small batches) so we don't
        # churn a pg pool per call.
        if getattr(app.state, "_pdf_pg", None) is None:
            app.state._pdf_embedder = build_embedder(mode=resolve_mode())
            app.state._pdf_pg = PostgresRetrievalSource(
                dsn, dim=app.state._pdf_embedder.dim, table="rs_block")
        embedder = app.state._pdf_embedder
        pg = app.state._pdf_pg
        object_store = None
        if os.environ.get("R2_BUCKET"):
            try:
                from roster_kernel.ingestion.s3_storage import S3ObjectStore
                object_store = S3ObjectStore.from_env(
                    prefix=os.environ.get("ROSTER_R2_PREFIX", "roster/raw"))
            except Exception:   # noqa: BLE001 — raw persistence best-effort; index still lands
                object_store = None
        src_key = (body.source_key or "arxiv").strip() or "arxiv"
        out: list[dict] = []
        for d in (body.docs or []):
            nid = str(d.get("native_id") or "").strip()
            title = str(d.get("title") or "").strip()
            pdf_b64 = d.get("pdf_b64") or ""
            pre_md = d.get("markdown") or ""     # already-parsed on the LOCAL box → skip prod docling
            if not (nid and (pdf_b64 or pre_md)):
                out.append({"native_id": nid, "error": "missing native_id or pdf_b64/markdown"}); continue
            try:
                if pre_md:                       # LOCAL-parsed path: no docling/torch on the serving pod
                    md_body = str(pre_md)
                else:                            # PROD-docling path: parse the shipped PDF bytes here.
                    # docling lives in the (single, tech) vertical; lazy-import so the markdown path
                    # (and the whole API) never loads torch unless a PDF is actually shipped.
                    from roster_vertical.connectors.arxiv import _docling_pdf_to_markdown
                    pdf = _b64.b64decode(pdf_b64)
                    md_body = await asyncio.to_thread(_docling_pdf_to_markdown, pdf)
                if not md_body or len(md_body) < 500:   # a real paper body dwarfs this; guard junk input
                    out.append({"native_id": nid,
                                "error": f"body too small ({len(md_body or '')} chars)"}); continue
                md = md_body if md_body.lstrip().startswith("#") else f"# {title}\n\n## Full text\n\n{md_body}"
                facets = dict(d.get("facets")) if isinstance(d.get("facets"), dict) else {}
                facets.setdefault("source_kind", "paper")
                conn = PreParsedConnector(source_key=src_key, native_id=nid, title=title,
                                          markdown=md, facets=facets)
                n = await ingest_connector_to_postgres(
                    conn, pg, tenant_id="demo", embedder=embedder, object_store=object_store,
                    min_chars=40, target_chars=1800)
                out.append({"native_id": nid, "blocks": n, "body_chars": len(md_body)})
            except Exception as e:   # noqa: BLE001 — record per-doc + continue the batch
                out.append({"native_id": nid, "error": str(e)[:200]})
        return {"ingested": out}

    # ---- Corpus Explorer (admin-gated PURE RETRIEVAL — inspect ingested sources first-hand) ----
    def _admin_ok(tok: str) -> bool:
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        return (not want) or tok == want

    import re as _re
    def _clean_snip(t: str, n: int = 400) -> str:
        """Tidy a raw block chunk for display: collapse runs of spaces/newlines so it reads as clean
        text instead of ragged markdown noise."""
        t = _re.sub(r"[ \t]{2,}", " ", (t or "").strip())
        t = _re.sub(r"\n{2,}", "\n", t)
        return t[:n]

    def _doc_url(document_id: str, facets: dict | None):
        """Canonical external URL for a document via the vertical's link builder (needs facets for
        e.g. the EDGAR CIK). None when the vertical has no clean page."""
        s = app.state.service
        if s is None:
            s = app.state.service = build_default_service()
        ui = getattr(s, "ui", None)
        try:
            return ui.source_url(document_id, facets=facets) if ui else None
        except Exception:   # noqa: BLE001
            return None

    # facet filter spec (tech vertical) — STRUCTURAL filtering only (Rule 18): (ui_key, sql_col, op)
    _CORPUS_FACET_SPEC = [
        ("source_key", "source_key", "="),
        ("source_kind", "facets->>'source_kind'", "="),
        ("sector", "facets->>'sector'", "="),
        ("source_country", "facets->>'source_country'", "="),
        ("year", "facets->>'year'", "="),
        ("content_type", "content_type", "="),
    ]

    def _corpus_facet_clauses(filters: dict, params: list) -> list[str]:
        clauses = []
        for key, col, op in _CORPUS_FACET_SPEC:
            v = str((filters or {}).get(key) or "").strip()
            if v:
                params.append(f"%{v}%" if op == "ILIKE" else v)
                clauses.append(f"{col} {op} ${len(params)}")
        return clauses

    def _parse_facets(f):
        import json as _json
        if isinstance(f, dict):
            return f
        try:
            return _json.loads(f) if f else {}
        except Exception:   # noqa: BLE001
            return {}

    @app.post("/admin/corpus/search")
    async def admin_corpus_search(body: dict, x_admin_token: str = Header(default="")) -> dict:
        """Pure retrieval over ingested blocks: mode=keyword (tsv full-text) | semantic (pgvector),
        with optional facet filters. NO LLM, NO answer — the raw blocks + provenance. Empty query +
        filters → browse the newest blocks under those filters."""
        if not _admin_ok(x_admin_token):
            raise HTTPException(status_code=401, detail="admin token required")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=404, detail="no corpus DSN")
        mode = (body.get("mode") or "keyword").strip().lower()
        query = (body.get("query") or "").strip()
        tenant = (body.get("tenant") or "demo").strip()
        limit = max(1, min(int(body.get("limit") or 25), 100))
        params: list = [tenant]
        where = ["tenant_id = $1"] + _corpus_facet_clauses(body.get("filters") or {}, params)
        if mode == "semantic":
            if not query:
                raise HTTPException(status_code=400, detail="semantic mode needs a query")
            if app.state.service is None:
                app.state.service = build_default_service()
            vec = app.state.service.embedder.embed([query])[0]
            params.append("[" + ",".join(f"{x:.6f}" for x in vec) + "]")
            where.append("embedding IS NOT NULL")
            order = f"ORDER BY embedding <=> ${len(params)}::vector"
        elif query:
            params.append(query)
            where.append(f"tsv @@ plainto_tsquery('english', ${len(params)})")
            order = f"ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', ${len(params)})) DESC"
        else:
            order = "ORDER BY created_at DESC NULLS LAST"   # browse newest under the filters
        sql = (f"SELECT document_id, block_id, left(text, 1400) AS text, facets, document_title, "
               f"content_type, source_key FROM rs_block WHERE {' AND '.join(where)} {order} LIMIT {limit}")
        import asyncpg
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(sql, *params)
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"search error: {e}") from e
        finally:
            await conn.close()
        out = []
        for r in rows:
            f = _parse_facets(r["facets"])
            out.append({"document_id": r["document_id"], "block_id": r["block_id"],
                        "text": _clean_snip(r["text"] or "", 1400), "title": r["document_title"],
                        "content_type": r["content_type"], "source_key": r["source_key"],
                        "facets": f, "url": _doc_url(r["document_id"], f)})
        return {"mode": mode, "count": len(out), "results": out}

    @app.get("/admin/corpus/document")
    async def admin_corpus_document(document_id: str, tenant: str = "demo",
                                    x_admin_token: str = Header(default="")) -> dict:
        """All blocks of ONE ingested document, in order — read the whole source firsthand."""
        if not _admin_ok(x_admin_token):
            raise HTTPException(status_code=401, detail="admin token required")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=404, detail="no corpus DSN")
        import asyncpg
        conn = await asyncpg.connect(dsn)
        try:
            rows = await conn.fetch(
                "SELECT block_id, text, facets, document_title, content_type, source_key "
                "FROM rs_block WHERE tenant_id = $1 AND document_id = $2 ORDER BY block_id LIMIT 2000",
                tenant, document_id)
        finally:
            await conn.close()
        if not rows:
            raise HTTPException(status_code=404, detail="document not found for this tenant")
        r0 = rows[0]
        f0 = _parse_facets(r0["facets"])
        return {"document_id": document_id, "title": r0["document_title"],
                "content_type": r0["content_type"], "source_key": r0["source_key"],
                "facets": f0, "n_blocks": len(rows), "url": _doc_url(document_id, f0),
                "blocks": [{"block_id": r["block_id"], "text": r["text"]} for r in rows]}

    @app.get("/admin/corpus/facets")
    async def admin_corpus_facets(tenant: str = "demo",
                                  x_admin_token: str = Header(default="")) -> dict:
        """Distinct low-cardinality facet values (for the filter dropdowns), with per-value counts."""
        if not _admin_ok(x_admin_token):
            raise HTTPException(status_code=401, detail="admin token required")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not dsn:
            raise HTTPException(status_code=404, detail="no corpus DSN")
        import asyncpg
        conn = await asyncpg.connect(dsn)
        out: dict = {}
        try:
            for key in ("source_key", "source_kind", "sector", "source_country", "content_type", "year"):
                col = key if key in ("source_key", "content_type") else f"facets->>'{key}'"
                rows = await conn.fetch(
                    f"SELECT {col} AS v, count(*) AS c FROM rs_block WHERE tenant_id = $1 AND {col} IS NOT NULL "
                    f"AND {col} <> '' GROUP BY 1 ORDER BY 2 DESC LIMIT 40", tenant)
                out[key] = [{"value": r["v"], "count": r["c"]} for r in rows]
        finally:
            await conn.close()
        return {"tenant": tenant, "facets": out}


    @app.post("/admin/glossary/sanitize")
    async def admin_glossary_sanitize(x_admin_token: str = Header(default="")) -> dict:
        """One-time repair: strip stray XML/HTML-ish tags (`<r>metformin</r>`) that older writes let
        leak into stored glossary term/related edges. Idempotent structural cleanup (Rule 18)."""
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="admin token required")
        g = _glossary()
        if g is None:
            raise HTTPException(status_code=404, detail="glossary unavailable")
        try:
            return await g.sanitize_markup()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"sanitize error: {e}") from e

    # ---- Evidence Pulse P0 admin surface (spec A4/A5): scan · list · approve/retract -----------
    def _pulse_admin_gate(x_admin_token: str):
        cur = _currency()
        if cur is None:
            raise HTTPException(status_code=404, detail="pulse not enabled")
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="admin token required")
        return cur

    @app.post("/admin/pulse/scan")
    async def admin_pulse_scan(x_admin_token: str = Header(default="")) -> dict:
        """Sweep curator-declared lineage into the ledger (declared = high-confidence → approved,
        A4) and (re-)apply all approved stamps. Idempotent — this is ALSO the manual re-stamp job
        to run after any re-ingest (facet overwrite erases stamps; the ledger restores them)."""
        cur = _pulse_admin_gate(x_admin_token)
        manifest = load_active_vertical()
        try:
            return await cur.sweep_declared(list(getattr(manifest, "lineage", ()) or ()))
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"pulse scan failed: {e}") from e

    @app.post("/admin/pulse/detect")
    async def admin_pulse_detect(x_admin_token: str = Header(default="")) -> dict:
        """SHADOW-MODE supersession detection (spec 3.2, approval-gated per A4): structural
        candidate pairs (same issuer + overlapping subjects + different years, versioned-document
        tier) → LLM judge → SHADOW events only. Nothing stamps or notifies until a human approves
        via /admin/pulse/event. Background task; status in /admin/pulse/detect (GET)."""
        cur = _pulse_admin_gate(x_admin_token)
        prompt = getattr(load_active_vertical(), "supersession_judge_prompt", None)
        if not prompt:
            raise HTTPException(status_code=404, detail="no supersession judge for this vertical")
        state = await cur.get_state("detect_scan")
        if state and state.get("status") == "running":
            return {"status": "already_running", "started_at": state.get("started_at")}
        import datetime as _dt
        await cur.set_state("detect_scan", {"status": "running",
                            "started_at": _dt.datetime.utcnow().isoformat() + "Z"})
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service

        async def _run():
            from roster_kernel.currency.candidates import edition_candidates
            try:
                docs = await cur.list_documents_meta(
                    facet_key="pub_type", facet_values=("guideline", "practice guideline"))
                decided = {(e["old_document_id"], e["new_document_id"])
                           for e in await cur.list_events(limit=500)}
                pairs = edition_candidates(docs, exclude_pairs=decided)
                registry = await _topic_registry(cur)

                class _Verdict(BaseModel):
                    supersedes: bool = False
                    materiality: str = "minor"
                    subjects: list[str] = []
                shadowed = 0
                for old, new in pairs:
                    comp = await svc.llm.complete(
                        system=prompt + _registry_block(registry),
                        messages=[{"role": "user", "content":
                                   f"OLDER: {old['title']} (issuer {old['issuer']}, {old['year']}; "
                                   f"subjects: {', '.join(old['conditions'][:8])})\n"
                                   f"NEWER: {new['title']} (issuer {new['issuer']}, {new['year']}; "
                                   f"subjects: {', '.join(new['conditions'][:8])})"}],
                        response_format=_Verdict, max_tokens=500)
                    v = comp.parsed
                    if v.supersedes:
                        subs = await cur.ensure_topics([s for s in (v.subjects or []) if s][:5])
                        await cur.record(relation="superseded_by",
                                         old_document_id=old["document_id"],
                                         new_document_id=new["document_id"],
                                         subjects=subs,
                                         materiality=("major" if v.materiality == "major" else "minor"),
                                         confidence="judge", status="shadow")
                        shadowed += 1
                await cur.set_state("detect_scan", {"status": "done", "candidates": len(pairs),
                                    "shadow_events": shadowed,
                                    "finished_at": _dt.datetime.utcnow().isoformat() + "Z"})
            except Exception as e:   # noqa: BLE001
                await cur.set_state("detect_scan", {"status": "failed", "error": str(e)[:300]})

        app.state.pulse_detect_task = asyncio.create_task(_run())
        return {"status": "started"}

    @app.get("/admin/pulse/detect")
    async def admin_pulse_detect_status(x_admin_token: str = Header(default="")) -> dict:
        cur = _pulse_admin_gate(x_admin_token)
        return (await cur.get_state("detect_scan")) or {"status": "never_run"}

    @app.get("/pulse/recent")
    async def pulse_recent(limit: int = 20) -> dict:
        """PUBLIC what-changed feed (spec C3): recent approved change events — the visible proof
        the corpus stays current. No auth; approved events only; audit metadata redacted."""
        cur = _currency()
        if cur is None:
            raise HTTPException(status_code=404, detail="pulse not enabled")
        try:
            events = await cur.list_events(status="approved", limit=min(limit, 50))
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"pulse feed failed: {e}") from e
        return {"events": [{**{k: e[k] for k in
                            ("relation", "old_document_id", "new_document_id", "subjects",
                             "materiality", "brief_md", "created_at")},
                            "old_url": _pulse_doc_url(e.get("old_document_id", "")),
                            "new_url": _pulse_doc_url(e.get("new_document_id", ""))}
                           for e in events]}

    @app.get("/admin/pulse/events")
    async def admin_pulse_events(status: str | None = None, limit: int = 100,
                                 x_admin_token: str = Header(default="")) -> dict:
        """The auditable change ledger — every relation, its status, and when it was recorded."""
        cur = _pulse_admin_gate(x_admin_token)
        try:
            return {"events": await cur.list_events(status=status, limit=limit)}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"pulse list failed: {e}") from e

    @app.post("/admin/pulse/event")
    async def admin_pulse_event(body: PulseEventIn, x_admin_token: str = Header(default="")) -> dict:
        """Approve (stamps applied) or retract (stamps removed, event kept for audit) one event —
        the panel-required one-click reversal path for a wrong supersession."""
        cur = _pulse_admin_gate(x_admin_token)
        status = {"approve": "approved", "retract": "retracted_event"}.get(body.action)
        if status is None:
            raise HTTPException(status_code=400, detail="action must be approve|retract")
        try:
            ok = await cur.set_status(body.event_id, status)
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"pulse action failed: {e}") from e
        if not ok:
            raise HTTPException(status_code=404, detail="unknown event")
        return {"event_id": body.event_id, "status": status}

    # ---- Grounded Relationship Graph P0 (learnings/knowledgegraph.md): sync · list · related ---
    def _graph_admin_gate(x_admin_token: str):
        g = _graph()
        if g is None:
            raise HTTPException(status_code=404, detail="graph not enabled")
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="admin token required")
        return g

    async def _graph_invalidate(g) -> dict:
        """A5 sweep: demote/flag edges whose evidence documents were retracted/superseded.
        Reads the approved ledger when Pulse is on; a no-op result when it isn't."""
        cur = _currency()
        if cur is None:
            return {"edges_demoted": 0, "edges_flagged": 0, "note": "pulse off — no ledger"}
        events = await cur.list_events(status="approved", limit=500)
        return await g.invalidate_from_events(events)

    @app.post("/admin/graph/sync")
    async def admin_graph_sync(x_admin_token: str = Header(default="")) -> dict:
        """Idempotently upsert the vertical's curated edges (born active; never resurrects a
        manually demoted edge), then run the A5 invalidation sweep against the approved ledger."""
        g = _graph_admin_gate(x_admin_token)
        manifest = load_active_vertical()
        try:
            edges = list(getattr(manifest, "graph_edges", ()) or ())
            synced = await g.sync_curated(edges)
            # v3: edge endpoints are registry topics — mint any new ones as CONDITION kind
            # (masqueraders are real conditions; the stability contract applies). Best-effort:
            # the graph works without the registry row; Pulse consistency is what this buys.
            minted = 0
            cur = _currency()
            if cur is not None and edges:
                names = sorted({e["subject"] for e in edges} | {e["object"] for e in edges})
                minted = len(await cur.ensure_topics(names, source="seed", kind="condition"))
            invalidated = await _graph_invalidate(g)
            return {**synced, "endpoints_ensured": minted, **invalidated, **(await g.stats())}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"graph sync failed: {e}") from e

    @app.get("/admin/graph/edges")
    async def admin_graph_edges(status: str | None = None, needs_review: bool | None = None,
                                limit: int = 200,
                                x_admin_token: str = Header(default="")) -> dict:
        g = _graph_admin_gate(x_admin_token)
        try:
            return {"edges": await g.list_edges(status=status, needs_review=needs_review,
                                                limit=limit),
                    "stats": await g.stats()}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"graph list failed: {e}") from e

    @app.post("/admin/graph/edge")
    async def admin_graph_edge(body: GraphEdgeIn, x_admin_token: str = Header(default="")) -> dict:
        """activate | demote one edge — the one-click reversal path for a wrong edge."""
        g = _graph_admin_gate(x_admin_token)
        status = {"activate": "active", "demote": "demoted"}.get(body.action)
        if status is None:
            raise HTTPException(status_code=400, detail="action must be activate|demote")
        try:
            ok = await g.set_status(body.edge_id, status)
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"graph action failed: {e}") from e
        if not ok:
            raise HTTPException(status_code=404, detail="unknown edge")
        return {"edge_id": body.edge_id, "status": status}

    @app.post("/admin/backup/run")
    async def admin_backup_run(x_admin_token: str = Header(default="")) -> dict:
        """Recreatability backup NOW (admin token): dumps every irreplaceable table + the
        corpus text/facets to R2 under backups/<date>/ (background; poll GET)."""
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="admin token required")
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if not (dsn and os.environ.get("R2_BUCKET")):
            raise HTTPException(status_code=503, detail="needs ROSTER_CORPUS_DSN + R2_* env")
        from api.backup import R2Named, run_backup

        async def _run():
            cur = _currency()
            try:
                m = await run_backup(dsn, R2Named())
                if cur:
                    await cur.set_state("last_backup", {"at": m["finished_at"],
                                                        "manifest": {k: m[k] for k in
                                                                     ("date", "tables",
                                                                      "corpus_parts", "errors")}})
            except Exception as e:   # noqa: BLE001
                if cur:
                    await cur.set_state("last_backup", {"error": str(e)[:300]})
        app.state.backup_task = asyncio.create_task(_run())
        return {"status": "started"}

    @app.get("/admin/backup/run")
    async def admin_backup_status(x_admin_token: str = Header(default="")) -> dict:
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="admin token required")
        cur = _currency()
        return (await cur.get_state("last_backup")) if cur else {"note": "no state store"}

    @app.get("/admin/ingest/sources")
    async def admin_ingest_sources(x_admin_password: str = Header(default="")) -> dict:
        """Ingestion console payload (panel-password gate, READ-ONLY): every connector, live
        per-source corpus footprint (docs/blocks/newest), and the queue summary. PUSHING
        ingestion stays on POST /admin/corpus/ingest (admin TOKEN — it spends + mutates)."""
        if x_admin_password != _admin_ui_pw():
            raise HTTPException(status_code=401, detail="bad admin password")
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        stats: dict[str, dict] = {}
        if dsn:
            import asyncpg
            conn = await asyncpg.connect(dsn)
            try:
                rows = await conn.fetch(
                    """SELECT COALESCE(NULLIF(source_key,''),'(corpus)') AS sk, count(*) AS blocks,
                              count(DISTINCT document_id) AS docs, max(created_at) AS newest
                       FROM rs_block GROUP BY 1 ORDER BY 2 DESC""")
                stats = {r["sk"]: {"blocks": r["blocks"], "docs": r["docs"],
                                   "newest": r["newest"].isoformat() if r["newest"] else None}
                         for r in rows}
            finally:
                await conn.close()
        qsum = {}
        q = _gap_queue()
        if q is not None:
            try:
                qsum = await q.summary()
            except Exception:   # noqa: BLE001
                qsum = {}
        return {"connectors": [
                    {"key": k, "class": type(c).__name__,
                     "doc": (type(c).__doc__ or "").strip().split("\n")[0][:140],
                     **stats.get(k, {"blocks": 0, "docs": 0, "newest": None})}
                    for k, c in sorted(svc.connectors.items())],
                "other_sources": {k: v for k, v in stats.items() if k not in svc.connectors},
                "queue": qsum,
                "job_shape": {"connector": "one of the keys above", "query": "connector query",
                              "limit": "cap (<=400)", "kind": "label", "quality": "tag",
                              "source_country": "optional facet stamp (e.g. IN)"}}

    @app.get("/admin/graph/view")
    async def admin_graph_view(days: int = 30,
                               x_admin_password: str = Header(default="")) -> dict:
        """READ-ONLY graph console payload for the UI (panel-password gate — curated clinical
        relations + aggregate quality stats, no user data). Edge MUTATIONS stay behind the
        admin token (/admin/graph/edge). Includes the impact rollup: grounded rate and claim
        counts for answers with graph legs merged vs without — the standing 'is the graph
        helping, never hurting' watch."""
        if x_admin_password != _admin_ui_pw():
            raise HTTPException(status_code=401, detail="bad admin password")
        g = _graph()
        if g is None:
            raise HTTPException(status_code=404, detail="graph not enabled")
        try:
            edges = await g.list_edges(limit=500)
            stats = await g.stats()
            impact = None
            store = _store()
            if store is not None:
                impact = await store.graph_impact(days=min(max(days, 1), 365))
            return {"edges": edges, "stats": stats, "impact": impact,
                    "expand_mode": graph_expand_mode()}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"graph view failed: {e}") from e

    @app.get("/graph/related")
    async def graph_related(topic: str, limit: int = 8) -> dict:
        """Active graph neighbors of a topic (hierarchy-lifted, confidence-ranked) — the public
        read surface. Served from the in-process snapshot: no DB round trip at steady state.
        The graph steers search and Pulse; it is NEVER evidence — nothing here is citable."""
        g = _graph()
        if g is None:
            raise HTTPException(status_code=404, detail="graph not enabled")
        try:
            return {"topic": topic,
                    "related": await g.neighbors([topic], limit=min(max(limit, 1), 24))}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"graph lookup failed: {e}") from e

    @app.post("/auth/register")
    async def auth_register(body: RegisterIn) -> dict:
        """Adoption P0: register (or re-register) a user for the free verified-clinician tier.
        Upsert-on-email; returns the bearer token ONCE (the FE stores it and sends it with feedback).
        NPI (optional, US) is verified structurally against the public CMS registry. 404 when off."""
        if not accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts are not enabled")
        store = _accounts()
        if store is None:
            raise HTTPException(status_code=503, detail="no account store configured (ROSTER_CORPUS_DSN)")
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", body.email or ""):
            raise HTTPException(status_code=400, detail="invalid email")
        # SECURITY: register ALWAYS requires a >=12-char password, and REJECTS an email that already
        # has an account. (The store's upsert-on-email would otherwise let anyone re-claim an existing
        # account by email without proving the password — account takeover. Returning users sign in.)
        if len(body.password or "") < 12:
            raise HTTPException(status_code=400, detail="password must be at least 12 characters")
        if await store.email_exists(body.email):
            raise HTTPException(status_code=409,
                                detail="an account with that email already exists — please sign in")
        from api.accounts import hash_password
        pw_hash, pw_salt = hash_password(body.password)
        name = (body.name or "").strip() or (body.email.split("@")[0] if body.email else "")
        npi_ok = False
        if body.npi.strip():
            from api.accounts import verify_npi
            npi_ok = await verify_npi(body.npi)
        try:
            user, token = await store.register(
                email=body.email, name=name, profession=body.profession[:80],
                country=body.country[:40], npi=body.npi.strip()[:16], npi_verified=npi_ok,
                disclaimer_ack=body.disclaimer_ack, pw_hash=pw_hash, pw_salt=pw_salt)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"registration failed: {e}") from e
        return {"user": user, "token": token}

    @app.post("/auth/login")
    async def auth_login(body: LoginIn) -> dict:
        """Sign in with email + password → a fresh per-device bearer token (stored by the FE)."""
        if not accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts are not enabled")
        store = _accounts()
        if store is None:
            raise HTTPException(status_code=503, detail="no account store configured")
        try:
            res = await store.login(email=body.email, password=body.password)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"login failed: {e}") from e
        if res is None:
            raise HTTPException(status_code=401, detail="incorrect email or password")
        user, token = res
        return {"user": user, "token": token}

    @app.post("/auth/logout")
    async def auth_logout(x_roster_token: str = Header(default="")) -> dict:
        if accounts_enabled():
            store = _accounts()
            if store is not None:
                await store.logout(x_roster_token)
        return {"ok": True}

    async def _require_user(x_roster_token: str):
        """Shared guard for /me* routes: valid token → user dict, else 401 (or 404 when off)."""
        if not accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts are not enabled")
        store = _accounts()
        if store is None:
            raise HTTPException(status_code=503, detail="no account store configured")
        user = await store.user_by_token(x_roster_token)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in first")
        return store, user

    @app.get("/me")
    async def me(x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        return {"user": user}

    # ---- saved searches ----
    @app.get("/me/searches")
    async def me_searches(x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        return {"searches": await store.list_searches(user["id"])}

    @app.post("/me/searches")
    async def me_add_search(body: SavedSearchIn, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        await store.add_search(user["id"], body.query, body.mode)
        return {"ok": True}

    # ---- shortlist buckets ----
    @app.get("/me/buckets")
    async def me_buckets(x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        return {"buckets": await store.list_buckets(user["id"])}

    @app.post("/me/buckets")
    async def me_create_bucket(body: BucketIn, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        if not (body.name or "").strip():
            raise HTTPException(status_code=400, detail="name required")
        return await store.create_bucket(user["id"], body.name)

    @app.delete("/me/buckets/{bucket_id}")
    async def me_delete_bucket(bucket_id: int, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        return {"ok": await store.delete_bucket(user["id"], bucket_id)}

    @app.get("/me/buckets/{bucket_id}/items")
    async def me_bucket_items(bucket_id: int, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        items = await store.list_items(user["id"], bucket_id)
        if items is None:
            raise HTTPException(status_code=404, detail="bucket not found")
        return {"items": items}

    @app.post("/me/buckets/{bucket_id}/items")
    async def me_add_item(bucket_id: int, body: BucketItemIn, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        if body.kind not in ("person", "job"):
            raise HTTPException(status_code=400, detail="kind must be person or job")
        if not (body.ref_id or "").strip():
            raise HTTPException(status_code=400, detail="ref_id required")
        ok = await store.add_item(user["id"], bucket_id, kind=body.kind, ref_id=body.ref_id,
                                  label=body.label, payload=body.payload)
        if not ok:
            raise HTTPException(status_code=404, detail="bucket not found")
        return {"ok": True}

    @app.delete("/me/buckets/{bucket_id}/items/{item_id}")
    async def me_delete_item(bucket_id: int, item_id: int, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        return {"ok": await store.delete_item(user["id"], bucket_id, item_id)}

    # ---- candidate profile for smooth apply (superset of ATS fields; prefill/autofill only) ----
    @app.get("/me/profile")
    async def me_get_profile(x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        return await store.get_profile(user["id"])

    @app.put("/me/profile")
    async def me_set_profile(body: ProfileIn, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        # cap size so the JSONB blob can't be abused; PII stays server-side, never logged
        import json as _json
        if len(_json.dumps(body.profile or {})) > 60000:
            raise HTTPException(status_code=400, detail="profile too large")
        await store.set_profile(user["id"], body.profile or {})
        return {"ok": True}

    @app.post("/me/resume")
    async def me_upload_resume(body: ResumeIn, x_roster_token: str = Header(default="")) -> dict:
        store, user = await _require_user(x_roster_token)
        import base64
        try:
            data = base64.b64decode(body.data_b64 or "", validate=True)
        except Exception:
            raise HTTPException(status_code=400, detail="invalid file encoding")
        if not data:
            raise HTTPException(status_code=400, detail="empty file")
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="resume must be under 5 MB")
        name = (body.name or "resume")
        if not name.lower().endswith((".pdf", ".doc", ".docx", ".txt")):
            raise HTTPException(status_code=400, detail="resume must be PDF, DOC, DOCX, or TXT")
        await store.set_resume(user["id"], name=name, ctype=body.content_type, data=data)
        return {"ok": True, "name": name}

    @app.get("/me/resume")
    async def me_get_resume(x_roster_token: str = Header(default="")):
        store, user = await _require_user(x_roster_token)
        got = await store.get_resume(user["id"])
        from fastapi.responses import Response as _Resp
        if not got:
            raise HTTPException(status_code=404, detail="no resume on file")
        name, ctype, data = got
        safe = re.sub(r'[^A-Za-z0-9._ -]', "_", name)[:120] or "resume"   # no header injection
        return _Resp(content=data, media_type=ctype,
                     headers={"Content-Disposition": f'inline; filename="{safe}"'})

    @app.post("/feedback")
    async def post_feedback(body: FeedbackIn, x_roster_token: str = Header(default="")) -> dict:
        """Per-answer user feedback keyed to the W1–W9 warrant taxonomy (the same codes the eval and
        auditor use — one contract, three uses). Requires a registered token so feedback is
        attributable; modes are whitelisted structurally. 404 when accounts are off."""
        if not accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts are not enabled")
        store = _accounts()
        if store is None:
            raise HTTPException(status_code=503, detail="no account store configured")
        user = await store.user_by_token(x_roster_token)
        if user is None:
            raise HTTPException(status_code=401, detail="register to give feedback")
        if body.verdict not in ("up", "down", "flag"):
            raise HTTPException(status_code=400, detail="verdict must be up|down|flag")
        # W1–W9 = warrant taxonomy; U1 (unclear/ambiguous) + U2 (misunderstood question) = UX root causes
        modes = [m for m in body.modes if m in ({f"W{i}" for i in range(1, 10)} | {"U1", "U2"})]
        try:
            fid = await store.add_feedback(
                user=user, session_id=body.session_id, turn_index=max(0, body.turn_index),
                verdict=body.verdict, modes=modes, claim_index=body.claim_index,
                note=body.note, question=body.question)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"feedback failed: {e}") from e
        return {"ok": True, "id": fid}

    def _admin_ui_pw() -> str:
        # UI panel password (user-chosen; default per request). ALPHA-grade gate for non-destructive
        # product toggles only — change via ROSTER_ADMIN_UI_PASSWORD; never gate data access with this.
        return os.environ.get("ROSTER_ADMIN_UI_PASSWORD", "1111")

    async def _settings_payload() -> dict:
        st = _settings()
        over = {}
        if st is not None:
            try:
                over = await st.all(fresh=True)
            except Exception:   # noqa: BLE001
                over = {}
        from api.settings import SettingStore
        return {"store": st is not None, "settings": {
            k: {"override": over.get(k, ""), "env_default": fn(),
                "resolved": SettingStore.resolve_flag(over.get(k, ""), fn())}
            for k, fn in _LIVE_FLAGS.items()}}

    # ---- Evidence Pulse P1 user surface: watches + inbox ---------------------------------------
    async def _pulse_user(x_roster_token: str):
        cur = _currency()
        if cur is None:
            raise HTTPException(status_code=404, detail="pulse not enabled")
        store = _accounts()
        if store is None:
            raise HTTPException(status_code=503, detail="no account store configured")
        user = await store.user_by_token(x_roster_token)
        if user is None:
            raise HTTPException(status_code=401, detail="sign in to use watches")
        return cur, user

    @app.post("/pulse/watch")
    async def pulse_watch_add(body: WatchIn, x_roster_token: str = Header(default="")) -> dict:
        """Watch a topic. FREE-TEXT topics are canonicalized against the stable registry first
        ("afib" → "atrial fibrillation") so watches actually match event subjects; topics chosen
        from the suggested chips are already canonical and skip the call. Fails open to raw text."""
        cur, user = await _pulse_user(x_roster_token)
        topic = (body.topic or "").strip()
        if body.source == "manual" and topic:
            canon_prompt = getattr(load_active_vertical(), "watch_canonize_prompt", None)
            if canon_prompt:
                try:
                    if app.state.service is None:
                        app.state.service = build_default_service()

                    class _Canon(BaseModel):
                        topic: str = ""
                    registry = await _topic_registry(cur)
                    comp = await app.state.service.llm.complete(
                        system=canon_prompt + _registry_block(registry),
                        messages=[{"role": "user", "content": topic[:200]}],
                        response_format=_Canon, max_tokens=200)
                    canon = (comp.parsed.topic or "").strip()
                    if canon:
                        topic = (await cur.ensure_topics([canon]))[0]
                except Exception:   # noqa: BLE001 — canonicalization is an enhancer; raw text stands
                    pass
        try:
            await cur.add_watch(user_id=user["id"], topic=topic, source=body.source or "manual")
            return {"watches": await cur.list_watches(user_id=user["id"]), "stored_as": topic}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"watch failed: {e}") from e

    @app.delete("/pulse/watch")
    async def pulse_watch_remove(topic: str, x_roster_token: str = Header(default="")) -> dict:
        cur, user = await _pulse_user(x_roster_token)
        try:
            await cur.remove_watch(user_id=user["id"], topic=topic)
            return {"watches": await cur.list_watches(user_id=user["id"])}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"unwatch failed: {e}") from e

    @app.get("/pulse/inbox")
    async def pulse_inbox(days: int = 30, x_roster_token: str = Header(default="")) -> dict:
        """The Pulse hub payload: PER-TOPIC rollups answering (1) anything unseen? and (2) how much
        moved in the rolling window — plus the watch list. Detail loads via /pulse/topic-activity."""
        cur, user = await _pulse_user(x_roster_token)
        try:
            return {"watches": await cur.list_watches(user_id=user["id"]),
                    "days": min(max(days, 1), 365),
                    "summary": await _inbox_with_related(
                        cur, user_id=user["id"], days=min(max(days, 1), 365))}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"inbox failed: {e}") from e

    async def _inbox_with_related(cur, *, user_id: str, days: int) -> list[dict]:
        """Consumer C2 (dark behind ROSTER_GRAPH_PULSE): each watched-topic rollup gains a
        `related` list — approved events on GRAPH-ADJACENT topics, tagged with the relation
        they arrived through. Visually distinct + lower priority than direct hits (spec);
        best-effort: a graph failure never breaks the inbox."""
        summary = await cur.inbox_summary(user_id=user_id, days=days)
        if not graph_pulse_enabled():
            return summary
        g = _graph()
        if g is None:
            return summary
        try:
            for row in summary:
                related, seen_evs = [], {e["id"] for e in row.get("events", [])}
                for nb in await g.neighbors([row["topic"]], limit=4):
                    other = nb["object"] if nb["direction"] == "out" else nb["subject"]
                    if other.lower().strip() == row["topic"].lower().strip():
                        continue
                    for e in await cur.events_for_topic(other, days=days, limit=3):
                        if e["id"] in seen_evs:
                            continue
                        seen_evs.add(e["id"])
                        related.append({**e, "related_topic": other,
                                        "via_relation": nb["relation"]})
                row["related"] = related[:5]
        except Exception:   # noqa: BLE001
            pass
        return summary

    def _pulse_doc_url(document_id: str):
        """Best-effort canonical link for a pulse item (vertical URL mapper; None when no page)."""
        try:
            if app.state.service is None:
                app.state.service = build_default_service()
            fn = getattr(getattr(app.state.service, "ui", None), "source_url", None)
            return fn(document_id, "") if (fn and document_id) else None
        except Exception:   # noqa: BLE001
            return None

    def _pulse_enrich_events(events: list[dict]) -> list[dict]:
        for e in events:
            e["old_url"] = _pulse_doc_url(e.get("old_document_id", ""))
            e["new_url"] = _pulse_doc_url(e.get("new_document_id", ""))
        return events

    @app.get("/pulse/topic-activity")
    async def pulse_topic_activity(topic: str, days: int = 30,
                                   x_roster_token: str = Header(default="")) -> dict:
        """One topic's movement in the rolling window — TOPIC-AS-QUERY composition (generic to any
        vertical): relational change events matching the topic (structural containment) + NEW
        corpus sources relevant to it (the existing retrieval engine finds relevance; the corpus
        time axis supplies first_seen). One query embedding; zero LLM calls."""
        cur, user = await _pulse_user(x_roster_token)
        days = min(max(days, 1), 365)
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        try:
            events = await cur.events_for_topic(topic, days=days)
            new_docs = []
            try:
                corpus_key = getattr(svc, "corpus_source_key", "") or None
                hits = await svc.search(question=topic, tenant_id="demo",
                                        source_keys=[corpus_key] if corpus_key else None, k=40)
                doc_ids = list({h.document_id for h in hits if getattr(h, "document_id", "")})
                new_docs = await cur.docs_first_seen(doc_ids, days=days)
            except Exception as e:   # noqa: BLE001 — activity degrades to events-only
                __import__("logging").getLogger("api.pulse").warning("topic activity search failed: %r", e)
            for nd in new_docs:
                nd["url"] = _pulse_doc_url(nd.get("document_id", ""))
            return {"topic": topic, "days": days,
                    "events": _pulse_enrich_events(events), "new_documents": new_docs}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"topic activity failed: {e}") from e

    async def _topic_registry(cur):
        """The canonical topic registry, seeded once from the vertical's covered-condition names —
        the STABILITY substrate: LLM calls prefer exact reuse, so repeated runs converge.
        CONDITION-kind only (v3 C-5): graph-minted drug/finding rows must never contaminate the
        watchable-topic prompts this feeds."""
        topics = await cur.list_topics(kind="condition")
        if not topics:
            seed = list(getattr(load_active_vertical(), "watch_topic_seed", ()) or ())
            if seed:
                await cur.ensure_topics(seed, source="seed")
                topics = await cur.list_topics(kind="condition")
        return topics

    def _registry_block(topics: list[str]) -> str:
        return ("\n\nEXISTING CANONICAL TOPICS (prefer exact verbatim reuse):\n"
                + "\n".join(f"- {t}" for t in topics[:400])) if topics else ""

    @app.post("/pulse/topics")
    async def pulse_topics(body: TopicsIn, x_roster_token: str = Header(default="")) -> dict:
        """Suggest 2-5 WATCHABLE topics for a Q&A (LLM-owned judgment, Rule 18 — durable subjects,
        never patient specifics), converged onto the canonical registry: existing entries are
        reused verbatim; a genuinely novel subject is minted ONCE and becomes the stable form.
        User-initiated (the watch picker), token-gated, one small call."""
        cur, user = await _pulse_user(x_roster_token)
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        prompt = getattr(load_active_vertical(), "watch_topic_prompt", None)
        if not prompt:
            return {"topics": []}

        class _Topics(BaseModel):
            topics: list[str] = []
        try:
            registry = await _topic_registry(cur)
            comp = await svc.llm.complete(
                system=prompt + _registry_block(registry),
                messages=[{"role": "user", "content":
                           f"QUESTION:\n{(body.question or '')[:2000]}\n\nANSWER:\n{(body.answer or '')[:4000]}"}],
                response_format=_Topics, max_tokens=450)
            raw = [t.strip() for t in (comp.parsed.topics or []) if t and t.strip()][:5]
            return {"topics": await cur.ensure_topics(raw)}   # registry canonical form wins
        except Exception as e:   # noqa: BLE001 — picker degrades to free-text
            _log = __import__("logging").getLogger("api.pulse")
            _log.warning("watch-topic suggestion failed: %r", e)
            return {"topics": []}

    async def _compute_coverage_activity():
        """COVERAGE PULSE sweep: rolling-window movement for EVERY covered condition (the
        vertical's roadmap, already the topic-registry seed). Heavy-ish (one embed + corpus
        search per condition) → computed in the background, cached in roster_pulse_state,
        self-refreshing daily. Buckets 7/30/90d in one pass."""
        cur = _currency()
        if cur is None:
            return
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        seed = list(getattr(load_active_vertical(), "watch_topic_seed", ()) or ())
        if not seed:
            return
        import datetime as _dt
        await cur.set_state("coverage_scan", {"status": "running",
                            "started_at": _dt.datetime.utcnow().isoformat() + "Z"})
        out = []
        now = _dt.datetime.now(_dt.timezone.utc)
        try:
            corpus_key = getattr(svc, "corpus_source_key", "") or None
            for cond in seed:
                row = {"topic": cond, "e7": 0, "e30": 0, "e90": 0, "d7": 0, "d30": 0, "d90": 0}
                try:
                    evs = await cur.events_for_topic(cond, days=90)
                    for e in evs:
                        age = (now - _dt.datetime.fromisoformat(e["created_at"])).days
                        row["e90"] += 1
                        if age <= 30: row["e30"] += 1
                        if age <= 7: row["e7"] += 1
                    hits = await svc.search(question=cond, tenant_id="demo",
                                            source_keys=[corpus_key] if corpus_key else None, k=40)
                    ids = list({h.document_id for h in hits if getattr(h, "document_id", "")})
                    for nd in await cur.docs_first_seen(ids, days=90):
                        age = (now - _dt.datetime.fromisoformat(nd["first_seen"])).days
                        row["d90"] += 1
                        if age <= 30: row["d30"] += 1
                        if age <= 7: row["d7"] += 1
                except Exception:   # noqa: BLE001 — one condition's failure never kills the board
                    pass
                out.append(row)
            await cur.set_state("coverage_activity",
                {"computed_at": _dt.datetime.utcnow().isoformat() + "Z", "conditions": out})
            await cur.set_state("coverage_scan", {"status": "done",
                                "conditions": len(out),
                                "finished_at": _dt.datetime.utcnow().isoformat() + "Z"})
        except Exception as e:   # noqa: BLE001
            await cur.set_state("coverage_scan", {"status": "failed", "error": str(e)[:300]})

    @app.get("/pulse/coverage")
    async def pulse_coverage() -> dict:
        """PUBLIC coverage-pulse board: cached movement across every covered condition.
        Self-refreshing: a stale (>24h) or absent board kicks a background recompute; the
        current cache (possibly empty on first call) returns immediately."""
        cur = _currency()
        if cur is None:
            raise HTTPException(status_code=404, detail="pulse not enabled")
        import datetime as _dt
        board = await cur.get_state("coverage_activity") or {}
        stale = True
        if board.get("computed_at"):
            try:
                age = _dt.datetime.utcnow() - _dt.datetime.fromisoformat(
                    board["computed_at"].rstrip("Z"))
                stale = age.total_seconds() > 86400
            except Exception:   # noqa: BLE001
                stale = True
        scan = await cur.get_state("coverage_scan") or {}
        if stale and scan.get("status") != "running":
            app.state.pulse_coverage_task = asyncio.create_task(_compute_coverage_activity())
            board = {**board, "refreshing": True}
        return board

    @app.post("/admin/pulse/coverage-scan")
    async def admin_pulse_coverage_scan(x_admin_token: str = Header(default="")) -> dict:
        """Force a coverage-pulse recompute now (background; status in roster_pulse_state)."""
        _pulse_admin_gate(x_admin_token)
        app.state.pulse_coverage_task = asyncio.create_task(_compute_coverage_activity())
        return {"status": "started"}

    @app.get("/pulse/watch-suggestions")
    async def pulse_watch_suggestions(x_roster_token: str = Header(default="")) -> dict:
        """Cross-session watch suggestions: the recurring durable subjects in THIS user's question
        history (LLM judgment against the canonical registry; already-watched excluded). One small
        call, fired when the Pulse panel opens; degrades to an empty list on any failure."""
        cur, user = await _pulse_user(x_roster_token)
        prompt = getattr(load_active_vertical(), "watch_suggest_prompt", None)
        store = _store()
        if not prompt or store is None or not user.get("email"):
            return {"suggestions": []}
        if app.state.service is None:
            app.state.service = build_default_service()

        class _Sug(BaseModel):
            topics: list[str] = []
        try:
            rows = await store.list(tenant_id="demo", q=user["email"], limit=40)
            questions = [r.get("question", "") for r in rows if r.get("question")][:40]
            if not questions:
                return {"suggestions": []}
            watched = [w["topic"] for w in await cur.list_watches(user_id=user["id"])]
            registry = await _topic_registry(cur)
            body_txt = ("QUESTION HISTORY (most recent first):\n"
                        + "\n".join(f"- {q[:200]}" for q in questions)
                        + ("\n\nALREADY WATCHED (never re-suggest):\n"
                           + "\n".join(f"- {w}" for w in watched) if watched else ""))
            comp = await app.state.service.llm.complete(
                system=prompt + _registry_block(registry),
                messages=[{"role": "user", "content": body_txt}],
                response_format=_Sug, max_tokens=450)
            raw = [t.strip() for t in (comp.parsed.topics or []) if t and t.strip()][:5]
            watched_lc = {w.lower() for w in watched}
            canon = await cur.ensure_topics(raw)
            return {"suggestions": [t for t in canon if t.lower() not in watched_lc]}
        except Exception as e:   # noqa: BLE001
            __import__("logging").getLogger("api.pulse").warning("watch suggestions failed: %r", e)
            return {"suggestions": []}

    @app.post("/pulse/seen")
    async def pulse_seen(body: SeenIn, x_roster_token: str = Header(default="")) -> dict:
        cur, user = await _pulse_user(x_roster_token)
        try:
            await cur.mark_seen(user_id=user["id"], event_id=body.event_id)
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"seen failed: {e}") from e
        return {"ok": True}

    @app.get("/admin/settings")
    async def admin_settings_get(x_admin_password: str = Header(default="")) -> dict:
        """Live product settings (admin panel): per-flag override / env default / resolved value."""
        if x_admin_password != _admin_ui_pw():
            raise HTTPException(status_code=401, detail="bad admin password")
        return await _settings_payload()

    @app.post("/admin/settings")
    async def admin_settings_set(body: SettingIn, x_admin_password: str = Header(default="")) -> dict:
        """Flip a controlled flag live (no redeploy): value 'on' | 'off' | '' (follow env)."""
        if x_admin_password != _admin_ui_pw():
            raise HTTPException(status_code=401, detail="bad admin password")
        if body.key not in _LIVE_FLAGS:
            raise HTTPException(status_code=400, detail=f"unknown setting (known: {sorted(_LIVE_FLAGS)})")
        if body.value not in ("on", "off", ""):
            raise HTTPException(status_code=400, detail="value must be 'on', 'off', or '' (follow env)")
        st = _settings()
        if st is None:
            raise HTTPException(status_code=503, detail="no settings store (ROSTER_CORPUS_DSN unset)")
        try:
            await st.set(body.key, body.value)
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"settings store error: {e}") from e
        return await _settings_payload()

    @app.get("/admin/stream-test")
    async def admin_stream_test(minutes: int = 7, x_admin_password: str = Header(default="")):
        """LLM-free SSE endurance test: pings every 15s + a tick each minute for `minutes`. Lets us
        measure exactly if/when the edge cuts an actively-pinging stream (diagnosing mid-answer drops)
        without spending a single model call."""
        if x_admin_password != _admin_ui_pw():
            raise HTTPException(status_code=401, detail="bad admin password")
        from fastapi.responses import StreamingResponse
        run = _sse_run_new()

        async def runner() -> None:
            # Registry-backed like the real runs, so resume (GET /stream/{run_id}) is testable
            # end-to-end without a model call: let the edge cut this stream, then resume it.
            total = max(1, min(minutes, 20)) * 60
            for sec in range(0, total, 15):
                await asyncio.sleep(15)
                if (sec + 15) % 60 == 0:
                    _sse_push(run, {"type": "tick", "minute": (sec + 15) // 60})
            _sse_push(run, {"type": "done"})
            _sse_done(run)

        run["task"] = asyncio.create_task(runner())

        async def gen():
            yield ": open\n\n"
            yield f"data: {json.dumps({'type': 'run', 'run_id': run['id']})}\n\n"
            async for chunk in _sse_follow(run, 0):
                yield chunk

        return StreamingResponse(gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    @app.get("/admin/feedback")
    async def admin_feedback(limit: int = 25, x_admin_token: str = Header(default="")) -> dict:
        """The accumulating feedback signal, aggregated (totals · by verdict · by W-mode · by day ·
        recent rows · user counts) — how we watch what's building up over time. Same admin-token gate
        as corpus ingest."""
        if not accounts_enabled():
            raise HTTPException(status_code=404, detail="accounts are not enabled")
        want = os.environ.get("ROSTER_ADMIN_TOKEN", "")
        if want and x_admin_token != want:
            raise HTTPException(status_code=401, detail="bad admin token")
        store = _accounts()
        if store is None:
            raise HTTPException(status_code=503, detail="no account store configured")
        try:
            return await store.feedback_summary(limit=min(limit, 100))
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"feedback summary failed: {e}") from e

    @app.get("/sessions")
    async def list_sessions(tenant_id: str = "demo", limit: int = 100, q: str = "",
                            audience: str = "", kind: str = "") -> dict:
        """Recent saved Q&A for this vertical + tenant (history), optional search `q`, an optional
        `kind` filter ('panel'|'research') for the Past-Sessions tabs, and — when the patient-mode
        flag is on — an optional audience filter ('clinician'|'patient')."""
        store = _store()
        if store is None:
            return {"sessions": []}
        aud = audience if (patient_mode_enabled() and audience in ("clinician", "patient")) else None
        knd = kind if kind in ("panel", "research", "crossview") else None
        try:
            return {"sessions": await store.list(tenant_id=tenant_id, limit=min(limit, 300),
                                                 q=q or None, audience=aud, kind=knd)}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"session store error: {e}") from e

    @app.get("/videos")
    async def list_videos(tenant_id: str = "demo", limit: int = 200) -> dict:
        """All briefing videos across sessions (for the video catalogue)."""
        store = _store()
        if store is None:
            return {"videos": []}
        try:
            vids = await store.list_videos(tenant_id=tenant_id, limit=min(limit, 300))
            # hide videos whose file is gone (local + R2 both missing)
            from api.video import video_exists
            vids = await asyncio.to_thread(
                lambda: [v for v in vids if video_exists(v["video_filename"])])
            return {"videos": vids}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"session store error: {e}") from e

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict:
        """Full saved Q&A (answer, claims, and any linked video)."""
        store = _store()
        if store is None:
            raise HTTPException(status_code=404, detail="no session store")
        row = await store.get(session_id)
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")
        return row

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict:
        """Soft-delete a session (hidden from list/get; row retained)."""
        store = _store()
        if store is None:
            raise HTTPException(status_code=404, detail="no session store")
        if not await store.soft_delete(session_id):
            raise HTTPException(status_code=404, detail="session not found")
        return {"deleted": True}

    @app.post("/sessions/{session_id}/patient-flag")
    async def session_patient_flag(session_id: str, body: PatientFlagIn) -> dict:
        """Mark/unmark a session as a REAL-WORLD PATIENT case (orange ◉ in the session list)."""
        store = _store()
        if store is None:
            raise HTTPException(status_code=404, detail="no session store")
        if not await store.set_real_patient(session_id, body.real_patient):
            raise HTTPException(status_code=404, detail="session not found")
        return {"id": session_id, "real_patient": body.real_patient}

    @app.get("/admin/coverage")
    async def admin_coverage() -> dict:
        """Live corpus coverage: what's ingested (per source/kind + per-download runs) and
        the declared roadmap (covered vs remaining conditions) from the active vertical."""
        if app.state.service is None:
            app.state.service = build_default_service()
        svc = app.state.service
        ui = getattr(svc, "ui", None)
        plan = ui.coverage_plan() if ui and hasattr(ui, "coverage_plan") else {}
        live: dict = {"by_source": {}, "by_kind": {}, "by_country": {}, "total_blocks": 0,
                      "total_docs": 0, "runs": []}
        dsn = os.environ.get("ROSTER_CORPUS_DSN")
        if dsn:
            import json
            import asyncpg
            conn = await asyncpg.connect(dsn)
            try:
                for r in await conn.fetch(
                    "SELECT source_key, count(*) blocks, count(DISTINCT document_id) docs "
                    "FROM rs_block GROUP BY source_key"):
                    live["by_source"][r["source_key"] or "?"] = {"blocks": r["blocks"], "docs": r["docs"]}
                for r in await conn.fetch(
                    "SELECT facets->>'source_kind' kind, count(*) blocks FROM rs_block GROUP BY 1"):
                    if r["kind"]:
                        live["by_kind"][r["kind"]] = r["blocks"]
                for r in await conn.fetch(
                    "SELECT facets->>'source_country' country, count(*) blocks FROM rs_block GROUP BY 1"):
                    live["by_country"][r["country"] or "?"] = r["blocks"]
                # by SECTOR facet (the tech subject-scope) — verifies the per-job facet override landed.
                live["by_sector"] = {}
                for r in await conn.fetch(
                    "SELECT facets->>'sector' sector, count(*) blocks FROM rs_block GROUP BY 1"):
                    live["by_sector"][r["sector"] or "(none)"] = r["blocks"]
                # TEXT-LENGTH health (panel: verify substance, not just row counts): flag empty/near-empty blocks.
                th = await conn.fetchrow(
                    "SELECT min(length(text)) mn, avg(length(text))::int av, "
                    "count(*) FILTER (WHERE length(text) < 40) tiny FROM rs_block")
                live["text_health"] = {"min_chars": th["mn"], "avg_chars": th["av"], "tiny_blocks": th["tiny"]}
                live["total_blocks"] = await conn.fetchval("SELECT count(*) FROM rs_block") or 0
                live["total_docs"] = await conn.fetchval("SELECT count(DISTINCT document_id) FROM rs_block") or 0
                if await conn.fetchval("SELECT to_regclass('rs_ingest_run')"):
                    for r in await conn.fetch(
                        "SELECT condition, by_source, total_blocks, created_at FROM rs_ingest_run "
                        "ORDER BY created_at DESC LIMIT 200"):
                        bs = r["by_source"]
                        live["runs"].append({
                            "condition": r["condition"],
                            "by_source": json.loads(bs) if isinstance(bs, str) else (bs or {}),
                            "total_blocks": r["total_blocks"],
                            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        })
            except Exception as e:  # noqa: BLE001
                live["error"] = str(e)
            finally:
                await conn.close()
        return {"vertical": getattr(svc, "vertical_name", ""), "plan": plan, "live": live}

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(accept_encoding: str = Header(default="")):
        return _html_response("admin.html", accept_encoding)

    @app.get("/admin/users")
    async def admin_users(limit: int = 500, x_admin_password: str = Header(default="")) -> dict:
        """Registered users (name/email/profession/country/verified/registered/last-seen). PII →
        admin-password gated."""
        if x_admin_password != _admin_ui_pw():
            raise HTTPException(status_code=401, detail="bad admin password")
        acc = _accounts()
        if acc is None:
            return {"users": [], "count": 0, "note": "accounts not enabled / no corpus DSN"}
        try:
            users = await acc.list_users(limit=max(1, min(int(limit), 5000)))
            return {"users": users, "count": len(users)}
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"users query failed: {e}") from e

    @app.get("/admin/people-coverage")
    async def people_coverage():
        """Coverage summary for the Coverage page: total people, by-source breakdown, per-dimension
        coverage, distinct companies, and jobs indexed. What the people/jobs graph actually holds."""
        store = _claim_store_cached()
        if store is None:
            return {"total": 0, "sources": [], "dimensions": [], "distinct_companies": 0,
                    "jobs": 0, "job_companies": 0}
        return await store.people_coverage()

    @app.get("/admin/perf", response_class=HTMLResponse)
    def perf_page(accept_encoding: str = Header(default="")):
        return _html_response("perf.html", accept_encoding)

    @app.get("/admin/perf-data")
    async def perf_data(hours: int = 168, kind: str | None = None,
                        x_admin_password: str = Header(default="")) -> dict:
        """Aggregated performance metrics from the per-Q&A instrumentation (avg/P50/P95/P99 latency,
        phase split, failure + stopped-reason distributions, recent runs). Admin-password gated."""
        if x_admin_password != _admin_ui_pw():
            raise HTTPException(status_code=401, detail="bad admin password")
        ps = _perf()
        if ps is None:
            return {"n": 0, "window_hours": hours, "note": "no perf store (no corpus DSN)"}
        try:
            return await ps.stats(hours=max(1, min(int(hours), 24 * 90)),
                                  kind=(kind if kind in ("qa", "panel") else None))
        except Exception as e:   # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"perf stats error: {e}") from e

    return app
