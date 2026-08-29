"""Post-hoc answer VISUALIZATION — turn a finished, grounded answer into conceptual/structural
visuals (mechanism flows, decision/differential trees, timelines) that augment its explanatory
power. On-demand, user-triggered (never adds answer latency), like /explain and /terms.

DESIGN (validated by a 3-model design panel + 3 SOTA-research sweeps, 2026-08-14):
- The LLM emits CONSTRAINED JSON PRIMITIVES, never raw SVG/HTML (injection + hallucination) and
  never a diagram DSL like Mermaid (ungroundable free text, no per-element citation, XSS history,
  theme-fighting). The frontend renders each primitive with hand-built, theme-aware SVG.
- NON-DUPLICATION: the inline ROSTER_ANSWER_CHARTS path owns NUMERIC encodings (bar/line/pie of
  cited findings); ROSTER_ANSWER_VISUALS owns prose comparison TABLES / ranked lists. This subsystem
  owns SPATIAL/STRUCTURAL visuals markdown can't express — and MUST NOT plot numbers as a
  quantitative encoding (a number may appear only as a label/annotation).
- GROUNDING (no new facts — the same invariant as /explain, /terms): the visual is a RESTRUCTURING
  of the already-grounded answer, never a new retrieval. Every node, every EDGE, and every timeline
  event carries a VERBATIM `quote` copied from the answer; code validates the quote is present in the
  answer text and DROPS anything unsupported. The EDGE is the hallucination surface (the "invented
  arrow"): an edge needs its OWN grounded basis, not just two grounded endpoints. Fail-safe: a visual
  that falls below its minimum after validation is dropped; if nothing survives, return [] (abstain,
  never a decorative filler — meaningful-or-empty).

Primitives: `flow`, `tree`, `timeline`, and `map` — a concept-relationship graph (Novak-style) for
exploratory "how do these relate" answers, where nodes are concepts/entities and the LABELED edges
carry the relationship (causes/inhibits/treated-by/associated-with). `map` is the highest-value but
highest-risk primitive: the labeled edge IS the relationship claim, so each edge carries its own
verbatim basis quote and isolated (edgeless) nodes are dropped; it must survive as an interconnected
web or it is discarded. Layout is a dependency-free force simulation on the frontend (node counts are
small and capped). Planned hardening: a per-edge LLM entailment gate on top of the quote anchor.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from roster_kernel.providers.llm import LLMClient

# --- structural caps (mirror _validate_charts discipline: a too-dense visual is dropped) ---
_MAX_NODES = 10
_MAX_EDGES = 16
_MAX_EVENTS = 10
_MAX_VISUALS = 4
KINDS = ("flow", "tree", "timeline", "map")
_MAP_MIN_NODES = 3    # a concept map must show a WEB (else it's a flow) — floor on nodes AND edges
_MAP_MIN_EDGES = 2

# Numeric/dose/%/date tokens (Rule 18: STRUCTURAL check, not a semantic heuristic — only verifies a
# number the model wrote into a LABEL also exists in the answer it is restructuring).
_NUM = re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|mg|mcg|g|ml|units?|mmhg|kg)?\b", re.I)


class VNode(BaseModel):
    id: str = Field(description="short stable id, unique within the visual")
    label: str = Field(description="display text (may lightly abstract the quote)")
    quote: str = Field(default="", description="VERBATIM span from the answer that supports this node")
    parent: str = Field(default="", description="tree only: id of the parent node ('' = root)")
    note: str = Field(default="", description="optional one-line detail, also grounded in the answer")


class VEdge(BaseModel):
    src: str = Field(description="source node id")
    dst: str = Field(description="target node id")
    label: str = Field(default="", description="relationship label, e.g. 'leads to', 'if positive'")
    quote: str = Field(default="", description="VERBATIM span from the answer supporting THIS link")


class VEvent(BaseModel):
    when: str = Field(default="", description="time/phase label, e.g. 'Week 0', 'Acute phase'")
    label: str = Field(description="what happens at this point")
    quote: str = Field(default="", description="VERBATIM span from the answer supporting this event")


class Visual(BaseModel):
    kind: str = Field(description="one of: flow | tree | timeline")
    title: str = Field(default="", description="short title for the visual")
    caption: str = Field(default="", description="one-line takeaway; must be grounded in the answer")
    nodes: list[VNode] = Field(default_factory=list, description="flow/tree nodes")
    edges: list[VEdge] = Field(default_factory=list, description="flow edges (directed)")
    events: list[VEvent] = Field(default_factory=list, description="timeline events, in order")


class VisualSet(BaseModel):
    visuals: list[Visual] = Field(default_factory=list)


def _norm(s: str) -> str:
    """Normalize for grounding comparison: drop markdown emphasis + [n] citations, lowercase,
    collapse whitespace. Structural only — no semantic interpretation."""
    s = re.sub(r"\[\d+\]", " ", s or "")
    s = re.sub(r"[*_`#>~\-]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _grounded(quote: str, hay: str) -> bool:
    """A quote is grounded iff its normalized form is a substring of the normalized answer.
    Short/empty quotes never ground (forces the model to cite a real span)."""
    q = _norm(quote)
    return len(q) >= 8 and q in hay


def _label_numbers_ok(label: str, hay_raw: str) -> bool:
    """Any number/dose/% the model put in a LABEL must appear in the answer (can't invent figures)."""
    hay = hay_raw.lower()
    for m in _NUM.finditer(label or ""):
        tok = m.group(0).strip().lower()
        if tok and tok not in hay:
            return False
    return True


def _clean_visual(v: Visual, answer: str) -> dict | None:
    """Validate + ground ONE visual against the answer. Returns a render-ready dict, or None if it
    fails the grounding/structure floor (fail-safe drop)."""
    if v.kind not in KINDS:
        return None
    hay_norm, hay_raw = _norm(answer), answer or ""
    title = (v.title or "").strip()
    caption = (v.caption or "").strip()
    if caption and not (_grounded(caption, hay_norm) or _label_numbers_ok(caption, hay_raw)):
        caption = ""   # ungrounded caption dropped, visual kept (guard the caption)

    if v.kind == "timeline":
        events = []
        for e in v.events[:_MAX_EVENTS]:
            if _grounded(e.quote, hay_norm) and _label_numbers_ok(e.label, hay_raw):
                events.append({"when": (e.when or "").strip(), "label": (e.label or "").strip(),
                               "quote": e.quote.strip()})
        if len(events) < 2:
            return None
        return {"kind": "timeline", "title": title, "caption": caption, "events": events}

    # flow / tree / map share node grounding
    nodes, ok_ids = [], set()
    for n in v.nodes[:_MAX_NODES]:
        if _grounded(n.quote, hay_norm) and _label_numbers_ok(n.label, hay_raw):
            nd = {"id": n.id, "label": (n.label or "").strip(), "quote": n.quote.strip()}
            if _label_numbers_ok(n.note, hay_raw) and _grounded(n.note, hay_norm):
                nd["note"] = n.note.strip()
            if v.kind == "tree":
                nd["parent"] = (n.parent or "").strip()
            nodes.append(nd)
            ok_ids.add(n.id)
    if len(nodes) < 2:
        return None

    if v.kind == "tree":
        # keep only parent refs that point at a surviving node ('' = root)
        for nd in nodes:
            if nd["parent"] and nd["parent"] not in ok_ids:
                nd["parent"] = ""
        return {"kind": "tree", "title": title, "caption": caption, "nodes": nodes}

    # flow / map: an EDGE needs its OWN grounded basis (the labeled relationship is the diagram's
    # hallucination surface — the "invented arrow"), AND both endpoints must have survived.
    edges = []
    for e in v.edges[:_MAX_EDGES]:
        if e.src in ok_ids and e.dst in ok_ids and _grounded(e.quote, hay_norm):
            edges.append({"src": e.src, "dst": e.dst, "label": (e.label or "").strip()})

    if v.kind == "map":
        # a concept map earns its place only as an interconnected WEB — drop isolated nodes (no
        # grounded edge touches them), then require a real graph, else it's a flow/list in disguise.
        linked = {e["src"] for e in edges} | {e["dst"] for e in edges}
        nodes = [n for n in nodes if n["id"] in linked]
        if len(nodes) < _MAP_MIN_NODES or len(edges) < _MAP_MIN_EDGES:
            return None
        return {"kind": "map", "title": title, "caption": caption, "nodes": nodes, "edges": edges}

    if not edges:
        return None
    return {"kind": "flow", "title": title, "caption": caption, "nodes": nodes, "edges": edges}


async def visualize_answer(
    *, llm: LLMClient, visuals_prompt: str, question: str, answer: str,
    max_tokens: int = 4000,
) -> list[dict]:
    """Generate grounded conceptual visuals for an answer. [] when nothing qualifies (abstain)."""
    clean = (answer or "").strip()
    if not clean:
        return []
    user = (
        f"QUESTION:\n{question}\n\n"
        f"ANSWER TO VISUALIZE (your ONLY source — restructure it, add nothing):\n{clean}\n\n"
        "Emit only visuals that genuinely add explanatory power beyond the prose. Every node, edge, "
        "and event MUST carry a `quote` copied VERBATIM from the answer above. If nothing qualifies, "
        "return an empty list."
    )
    res = await llm.complete(
        system=visuals_prompt,
        messages=[{"role": "user", "content": user}],
        response_format=VisualSet, max_tokens=max_tokens)
    out: list[dict] = []
    for v in (res.parsed.visuals or [])[:_MAX_VISUALS]:
        cleaned = _clean_visual(v, clean)
        if cleaned:
            out.append(cleaned)
    return out
