"""DIRECTIONS — the few ways this search could usefully go next (kernel; docs/specs/intent-convergence-loop.md §3).

The owner's ask: don't answer a vague query once and stop. Return some results, then offer a handful of
ways to steer — drawn from the query AND from what actually came back — and let the reader pick one,
ignore them, or type something else.

Almost none of the machinery is new. `leverage()` already returns the schema keys whose values split
the current pool; `eligible()` already judges whether a dimension organises THESE rows; `token_groups()`
already finds the distinctions inside a result set that no schema key names. This module is the small
part that was missing: putting those two very different scores on one scale, deciding which handful is
worth offering, and refusing to offer one at all when the results are already good.

Three rules the panel argued for and this encodes:

- **A direction may narrow, exclude, or re-centre.** "Results don't look right" is often answered best
  by *not this* — and the cluster to drop is the kind of thing only `token_groups` can see.
- **Nothing is offered that cannot deliver.** Every candidate carries the size of the slice it would
  leave. A direction that empties the results, or that changes nothing, is not a choice.
- **Silence is a valid answer.** A tight, well-matched result set gets no directions, because a
  clarifying question asked when nothing was unclear is a cost with no benefit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .grouping import eligible


@dataclass
class Direction:
    """One offer. `effect` is a contract edit, not a UI instruction: the caller applies it and re-runs."""
    key: str                       # the facet key, or "" for an emergent cluster
    label: str                     # what the reader sees
    values: list = field(default_factory=list)
    section: str = "must"          # must | avoid | center
    hits: int = 0                  # rows in the CURRENT set this would keep (or drop, when avoiding)
    score: float = 0.0
    source: str = "facet"          # facet | cluster
    why: str = ""


def _balance_score(share: float) -> float:
    """How useful is a split that keeps `share` of the set? Best near a half, useless at either end.

    This is the one number that lets a schema key and an emergent cluster be compared: both are judged
    by how much they would actually change, not by where they came from."""
    if share <= 0.0 or share >= 1.0:
        return 0.0
    return 1.0 - abs(share - 0.5) / 0.5


def facet_directions(counts: dict, schema, kind: str, *, exclude: set = frozenset(),
                     labels: dict | None = None, min_share: float = 0.08,
                     max_share: float = 0.92) -> list[Direction]:
    """Candidate directions from the COUNTS over the current slice.

    Counts are slice-wide, not page-wide, which is why they — and not the ten rows on screen — are the
    right input: a direction computed from ten rows would vanish the moment the batch got small, which
    is exactly when steering matters most."""
    out: list[Direction] = []
    lab = (labels or {}).get("keys") or {}
    for key, dist in (counts or {}).items():
        k = schema.key(key)
        if k is None or key in exclude or not isinstance(dist, dict):
            continue
        known = {v: int(n) for v, n in dist.items() if v != "unknown" and int(n) > 0}
        total = sum(known.values())
        if total <= 0 or len(known) < 2:
            continue
        for value, n in sorted(known.items(), key=lambda kv: -kv[1])[:4]:
            share = n / total
            if share < min_share or share > max_share:
                continue          # would change almost nothing, or would remove almost nothing
            out.append(Direction(key=key, label=f"{lab.get(key, key).replace('_', ' ')}: {str(value).replace('_', ' ')}",
                                 values=[value], section="must", hits=n,
                                 score=round(_balance_score(share), 4), source="facet",
                                 why=f"{n} of {total} here"))
    return out


def cluster_directions(groups: list, n_rows: int, *, min_share: float = 0.10,
                       max_share: float = 0.75) -> list[Direction]:
    """Candidate directions from the emergent clusters in the rows — `token_groups`' output.

    These are the ones no facet key can express: *these are mostly agency reposts*, *half of these are
    tour guides*. They are offered BOTH ways round — keep only these, or drop them — because a cluster
    the reader did not want is at least as informative as one they did."""
    out: list[Direction] = []
    for g in (groups or []):
        ids = list(getattr(g, "ids", None) or (g.get("ids") if isinstance(g, dict) else []) or [])
        name = str(getattr(g, "name", None) or (g.get("name") if isinstance(g, dict) else "") or "").strip()
        if not ids or not name or not n_rows:
            continue
        share = len(ids) / n_rows
        if share < min_share or share > max_share:
            continue
        sc = round(_balance_score(share), 4)
        out.append(Direction(key="", label=name, values=[name], section="must", hits=len(ids),
                             score=sc, source="cluster", why=f"{len(ids)} of {n_rows} here"))
        out.append(Direction(key="", label=f"not {name}", values=[name], section="avoid",
                             hits=n_rows - len(ids), score=round(sc * 0.9, 4), source="cluster",
                             why=f"drops {len(ids)} of {n_rows}"))
    return out


def worth_steering(coverage: dict | None, *, ambiguous: bool = False, min_pool: int = 12,
                   tight_match: int = 70) -> tuple[bool, str]:
    """Should anything be offered at all?

    The literature on clarifying questions is blunt: a low-quality one measurably disturbs people, and
    asking only the good ones beats asking all of them. So this fails CLOSED — the default is silence,
    and a reason is returned either way so the surface can say what it decided.

    A weak `best_match` on its own is deliberately NOT enough. It usually means the index holds nothing
    good, not that the reader was misunderstood, and steering on it offers chips for a coverage gap —
    blaming the reader for the corpus."""
    cov = coverage or {}
    pool = int(cov.get("pool") or 0)
    if pool < min_pool:
        return False, "too few results to split"
    best = cov.get("best_match")
    if best is not None and int(best) >= tight_match and not ambiguous:
        return False, "the top results already match closely"
    if ambiguous:
        return True, "the query could be read more than one way"
    if cov.get("weak") or cov.get("diagnosis"):
        return True, "nothing matched strongly"
    return False, "the results look settled"


def rank_directions(candidates: list, *, top: int = 3, per_key: int = 1) -> list[Direction]:
    """The handful actually offered: highest scoring, at most one per key so three chips are three
    different questions rather than three values of the same one, and never two halves of one cluster."""
    seen_key: dict = {}
    seen_cluster: set = set()
    out: list[Direction] = []
    for d in sorted(candidates or [], key=lambda x: -float(x.score or 0.0)):
        if d.source == "cluster":
            if d.label.removeprefix("not ") in seen_cluster:
                continue
            seen_cluster.add(d.label.removeprefix("not "))
        else:
            if seen_key.get(d.key, 0) >= per_key:
                continue
            seen_key[d.key] = seen_key.get(d.key, 0) + 1
        out.append(d)
        if len(out) >= top:
            break
    return out
