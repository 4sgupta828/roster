"""Slice-1 edge model: the bounded, grounded connection pathfinder.

TDD — these tests define the contract for `find_paths` BEFORE it exists. The pathfinder is PURE
logic over an injected async `neighbors(entity_id) -> list[Edge]` callable, so it is fully testable
offline (no DB): the store's real `neighbors` is the integration-tested adapter, tested separately.

Async idiom matches the repo (no pytest-asyncio installed): sync tests drive coroutines via
`asyncio.run`.

Contract (see docs/specs/edge-model.md, Slice 1):
- A connection PATH is an ordered list of grounded hops between two entities. Every hop is a real
  1-hop edge carrying its own active-evidence citation — a path is grounded iff every hop is.
- Traversal is bounded: max_depth (hops), degree_cap (skip HUB nodes to avoid explosion), max_paths.
- Edges are traversed as UNDIRECTED for connectivity, but each hop RECORDS its true direction +
  predicate + citation (so "how are they connected" is answerable, not just "are they").
- No fabricated hops; no node revisited within a path (simple paths, cycle-safe).
"""
from __future__ import annotations

import asyncio

from api.graph_path import Edge, find_paths


def _run(coro):
    return asyncio.run(coro)


def _edge(subj: str, pred: str, obj: str) -> Edge:
    """A grounded 1-hop edge with a citation, as the store would return it."""
    return Edge(
        subject_id=subj, predicate=pred, object_id=obj,
        claim_id=f"c:{subj}:{pred}:{obj}",
        citation={"document_id": f"d:{subj}:{obj}", "block_id": "b1",
                  "quote": f"{subj} {pred} {obj}", "authority_tier": 2},
    )


def _adjacency(edges: list[Edge]):
    """Build an async neighbors(entity_id) returning every edge INCIDENT to the node (either
    endpoint), mirroring the store primitive (bidirectional 1-hop grounded edges)."""
    async def neighbors(entity_id: str) -> list[Edge]:
        return [e for e in edges if e.subject_id == entity_id or e.object_id == entity_id]
    return neighbors


# ---- fixtures: a small grounded graph ----
# Acme --has_founder--> Alice ;  Beta --has_founder--> Alice ;  Beta --has_investor--> Vc1
# Acme --compared_to--> Beta
_G = [
    _edge("Acme", "has_founder", "Alice"),
    _edge("Beta", "has_founder", "Alice"),
    _edge("Beta", "has_investor", "Vc1"),
    _edge("Acme", "compared_to", "Beta"),
]


def test_direct_edge_is_a_one_hop_path():
    paths = _run(find_paths(_adjacency(_G), "Acme", "Alice", max_depth=4))
    assert paths, "a directly-connected pair must yield at least one path"
    shortest = min(paths, key=lambda p: len(p.hops))
    assert len(shortest.hops) == 1
    hop = shortest.hops[0]
    assert {hop.subject_id, hop.object_id} == {"Acme", "Alice"}
    assert hop.predicate == "has_founder"
    assert hop.citation["quote"] and hop.citation["document_id"]  # grounding


def test_two_hop_path_via_shared_neighbor():
    paths = _run(find_paths(_adjacency(_G), "Acme", "Vc1", max_depth=4))
    assert paths
    # Acme -> Beta (compared_to) -> Vc1 (has_investor) is 2 hops.
    shortest = min(paths, key=lambda p: len(p.hops))
    assert len(shortest.hops) == 2
    assert _endpoints(shortest) == ("Acme", "Vc1")
    _assert_chained(shortest)
    assert all(h.citation["quote"] for h in shortest.hops)  # every hop grounded


def test_no_path_returns_empty():
    lonely = _G + [_edge("Zeta", "has_founder", "Zed")]  # island disconnected from Acme
    assert _run(find_paths(_adjacency(lonely), "Acme", "Zed", max_depth=4)) == []


def test_max_depth_bounds_traversal():
    chain = [_edge("A", "r", "B"), _edge("B", "r", "C"), _edge("C", "r", "D"), _edge("D", "r", "E")]
    assert _run(find_paths(_adjacency(chain), "A", "E", max_depth=2)) == []
    assert _run(find_paths(_adjacency(chain), "A", "E", max_depth=4))  # reachable at depth 4


def test_degree_cap_skips_hub_nodes():
    hub = [_edge("A", "r", "HUB")] + [_edge("HUB", "r", f"leaf{i}") for i in range(500)] \
        + [_edge("HUB", "r", "B")]
    assert _run(find_paths(_adjacency(hub), "A", "B", max_depth=4, degree_cap=50)) == [], \
        "a node exceeding degree_cap must not be expanded (hub-skip)"
    assert _run(find_paths(_adjacency(hub), "A", "B", max_depth=4, degree_cap=1000)), \
        "with a high cap the hub is traversable"


def test_no_node_revisited_within_a_path():
    ring = [_edge("A", "r", "B"), _edge("B", "r", "C"), _edge("C", "r", "A")]
    for p in _run(find_paths(_adjacency(ring), "A", "C", max_depth=6)):
        ids = _node_sequence(p)
        assert len(ids) == len(set(ids)), f"path revisits a node: {ids}"


def test_max_paths_caps_result_count():
    many = []
    for i in range(20):
        many += [_edge("A", "r", f"mid{i}"), _edge(f"mid{i}", "r", "B")]
    paths = _run(find_paths(_adjacency(many), "A", "B", max_depth=3, max_paths=5))
    assert 0 < len(paths) <= 5


def test_same_source_and_target_is_trivial_empty():
    assert _run(find_paths(_adjacency(_G), "Acme", "Acme", max_depth=4)) == []


# ---- helpers ----
def _node_sequence(path) -> list[str]:
    cur = path.source
    seq = [cur]
    for h in path.hops:
        cur = h.object_id if h.subject_id == cur else h.subject_id
        seq.append(cur)
    return seq


def _endpoints(path):
    seq = _node_sequence(path)
    return (seq[0], seq[-1])


def _assert_chained(path):
    cur = path.source
    for h in path.hops:
        assert cur in (h.subject_id, h.object_id), "hop not incident to the running node"
        cur = h.object_id if h.subject_id == cur else h.subject_id
