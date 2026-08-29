"""Entity discovery / sourcing — the generic "who is working on X" mechanic (domain-free).

Turns a retrieval result (a flat pool of BlockHits for a capability query) into a RANKED list of the
ENTITIES (companies/orgs) most associated with it, each with grounded verbatim evidence. The kernel
owns the aggregation; the VERTICAL supplies `entity_of(hit) -> (name, type) | None` (how to read the
entity a hit is about — e.g. a document's issuer, a project's owner), so no domain noun enters the kernel.

This is the scouting counterpart to the ReAct answer loop: instead of one prose answer, it returns
targets. Grounding is intrinsic — every evidence quote is a real corpus block (already span-real).
"""
from __future__ import annotations

from typing import Callable


def aggregate_entities(
    hits: list,
    entity_of: Callable[[object], "tuple[str, str] | None"],
    *,
    top: int = 20,
    max_evidence: int = 3,
    tier_of: Callable[[object], float] | None = None,
) -> list[dict]:
    """Group hits by their owning entity and rank by evidence strength.

    Score = Σ (hit relevance × (1 + tier bonus)) over the entity's hits — a company tied to the query
    by many strong, high-tier sources ranks above one tied by a single weak mention. Returns, per
    entity: name, type, score, n_hits, the source kinds it appears in, its sector(s), and up to
    `max_evidence` verbatim quotes with their document ids (for citation).
    """
    groups: dict[str, dict] = {}
    for h in hits:
        ent = entity_of(h)
        if not ent:
            continue
        name, etype = ent
        if not name:
            continue
        key = name.strip().lower()
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"name": name, "type": etype, "score": 0.0, "n_hits": 0,
                               "sources": set(), "sectors": set(), "evidence": []}
        tier_bonus = (tier_of(h) if tier_of else 0.0)
        g["score"] += float(getattr(h, "score", 0.0) or 0.0) * (1.0 + tier_bonus)
        g["n_hits"] += 1
        if getattr(h, "source_key", ""):
            g["sources"].add(h.source_key)
        sec = (getattr(h, "facets", {}) or {}).get("sector")
        if sec:
            g["sectors"].add(sec)
        if len(g["evidence"]) < max_evidence:
            g["evidence"].append({
                "quote": (getattr(h, "text", "") or "")[:300].strip(),
                "document_id": getattr(h, "document_id", ""),
                "source_key": getattr(h, "source_key", ""),
            })
    ranked = sorted(groups.values(), key=lambda g: -g["score"])[:top]
    for g in ranked:
        g["sources"] = sorted(g["sources"])
        g["sectors"] = sorted(g["sectors"])
        g["score"] = round(g["score"], 4)
    return ranked
