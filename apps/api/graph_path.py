"""Slice-1 edge model: the bounded, grounded connection pathfinder (app-level; kernel-bound in Slice 5).

"How is X connected to Y" over the claim graph's entity-edges. A PATH is an ordered list of grounded
1-hop edges; each hop carries its own active-evidence citation, so a path is grounded iff every hop
is (Rule: grounding is intrinsic — no fabricated hops).

The pathfinder is PURE and store-agnostic: it takes an injected async `neighbors(entity_id) ->
list[Edge]` that returns the 1-hop grounded edges INCIDENT to a node (either endpoint). The store
adapter (`ClaimGraphStore.neighbors`) supplies real bidirectional edges from active-evidence claims;
here we only traverse. Edges are undirected for CONNECTIVITY but each hop preserves its true
direction (subject/predicate/object) + citation so the answer explains HOW, not just whether.

Bounded on purpose (a person/company graph has hubs): `max_depth` caps hop count, `degree_cap` skips
hub nodes (never expanded — the O(N^2) guard the panel required), `max_paths` caps the result set.
Simple paths only (a node is never revisited within one path).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass(frozen=True)
class Edge:
    """A grounded 1-hop directed edge (an entity-object claim) with its winning evidence citation."""
    subject_id: str
    predicate: str
    object_id: str
    claim_id: str = ""
    citation: dict = field(default_factory=dict)

    def other(self, node: str) -> str:
        """The endpoint of this edge that is NOT `node` (edges traverse undirected)."""
        return self.object_id if self.subject_id == node else self.subject_id

    def incident(self, node: str) -> bool:
        return node in (self.subject_id, self.object_id)


@dataclass(frozen=True)
class Path:
    """A grounded connection path from `source` to `target`: an ordered list of incident hops."""
    source: str
    target: str
    hops: tuple[Edge, ...]

    def to_dict(self) -> dict:
        """Prod/JSON shape: the query endpoints, hop count, and every grounded hop with its citation."""
        cur = self.source
        hops = []
        for h in self.hops:
            frm, to = (h.subject_id, h.object_id) if h.subject_id == cur else (h.object_id, h.subject_id)
            hops.append({
                "from": frm, "to": to, "subject_id": h.subject_id, "predicate": h.predicate,
                "object_id": h.object_id, "claim_id": h.claim_id, "citation": h.citation,
            })
            cur = h.other(cur)
        return {"source": self.source, "target": self.target, "length": len(self.hops), "hops": hops}


Neighbors = Callable[[str], Awaitable[list[Edge]]]


async def find_paths(
    neighbors: Neighbors,
    source: str,
    target: str,
    *,
    max_depth: int = 4,
    degree_cap: int = 200,
    max_paths: int = 10,
) -> list[Path]:
    """Return up to `max_paths` grounded connection paths from `source` to `target`, shortest first.

    BFS over simple paths so results come out in nondecreasing hop-length order. A node whose
    incident-edge count exceeds `degree_cap` is treated as a HUB and never expanded (its edges are
    not followed) — the explosion guard. `source == target` yields no path (no self-connection edge).
    """
    if source == target or max_depth < 1 or max_paths < 1:
        return []

    found: list[Path] = []
    # Frontier of partial simple paths: (current_node, hops_so_far, visited_nodes).
    start: tuple[str, tuple[Edge, ...], frozenset[str]] = (source, (), frozenset({source}))
    queue: deque[tuple[str, tuple[Edge, ...], frozenset[str]]] = deque([start])

    while queue and len(found) < max_paths:
        node, hops, visited = queue.popleft()
        if len(hops) >= max_depth:
            continue
        incident = await neighbors(node)
        # Hub-skip: a node with too many incident edges is not expanded (bounded traversal).
        if len(incident) > degree_cap:
            continue
        for edge in incident:
            if not edge.incident(node):
                continue
            nxt = edge.other(node)
            if nxt in visited:
                continue  # simple paths only — never revisit a node
            new_hops = hops + (edge,)
            if nxt == target:
                found.append(Path(source=source, target=target, hops=new_hops))
                if len(found) >= max_paths:
                    break
                continue  # a completed path is not extended further
            queue.append((nxt, new_hops, visited | {nxt}))

    found.sort(key=lambda p: len(p.hops))
    return found[:max_paths]
