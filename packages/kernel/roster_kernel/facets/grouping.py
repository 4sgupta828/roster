"""GROUPING mechanics (docs/specs/result-grouping.md) — pure, domain-free.

Two ways to cut a result set, and one set of rules both must obey:

- `eligible` — may a DIMENSION be offered at all for these rows? Judged on the rows in front of the user, never on a
  corpus average: enough of them know the value, a usable number of groups, no group swallowing the set, and only a
  small "not stated" remainder. A dimension that fails is hidden, never shown as a junk drawer.
- `enforce` — a proposed segmentation (a model's, or the token fallback's) made safe: every item in exactly one group,
  a bounded number of groups, unknown ids dropped, leftovers returned separately, and the dominant-group check that
  says whether it is worth another attempt.
- `token_groups` — the free, deterministic fallback: the distinctions INSIDE the set. A token nearly everything shares
  is the query, not a distinction, so it is dropped; what is left is scored by how usefully it splits the set.

Nothing here knows what a job, a company or a skill is."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Group:
    name: str
    ids: list
    why: str = ""
    key: str = ""          # the value or token that formed it (a facet grouping's own key)

    def to_dict(self) -> dict:
        return {"name": self.name, "ids": list(self.ids), "why": self.why, "key": self.key}


@dataclass
class Eligibility:
    ok: bool
    reason: str = ""
    known: float = 0.0
    groups: int = 0
    largest: float = 0.0
    score: float = 0.0
    counts: dict = field(default_factory=dict)


def eligible(values: list, *, kind: str = "categorical", min_groups: int = 2, max_groups: int = 8,
             max_largest: float = 0.60, max_unstated: float = 0.25, min_repeats: int = 3) -> Eligibility:
    """`values[i]` is the row's value for one dimension ("" / None when it has none). A dimension earns a place in the
    menu only if it actually organises THESE rows.

    Two kinds of dimension, because they fail in opposite ways. A CATEGORICAL one (level, industry, work mode) is useful
    with a handful of balanced groups and useless as thirty; an IDENTITY one (company, place) is useful precisely when
    it has many groups — the value is spotting that one employer holds five of these — and useless only when every row
    stands alone. `score` ranks the survivors."""
    n = len(values or [])
    if not n:
        return Eligibility(False, "no rows")
    counts: dict = {}
    unstated = 0
    for v in values:
        key = str(v).strip() if v not in (None, "") else ""
        if not key:
            unstated += 1
            continue
        counts[key] = counts.get(key, 0) + 1
    known = (n - unstated) / n
    g = len(counts)
    largest = (max(counts.values()) / (n - unstated)) if counts and n - unstated else 1.0
    e = Eligibility(True, "", round(known, 3), g, round(largest, 3), 0.0, counts)
    if unstated / n > max_unstated:      # one rule, not two: "enough rows know it" IS "few rows do not"
        return Eligibility(False, f"{unstated / n:.0%} not stated", e.known, g, e.largest, 0.0, counts)
    if g < min_groups:
        return Eligibility(False, "one group only", e.known, g, e.largest, 0.0, counts)
    if largest > max_largest:
        return Eligibility(False, f"one group holds {largest:.0%}", e.known, g, e.largest, 0.0, counts)
    if kind == "identity":
        repeats = sum(1 for v in counts.values() if v > 1)
        if repeats < min_repeats:
            return Eligibility(False, "every row stands alone", e.known, g, e.largest, 0.0, counts)
        # the value is concentration: how much of the set sits in groups that actually repeat
        share = sum(v for v in counts.values() if v > 1) / max(n - unstated, 1)
        e.score = round(share * known, 4)
        return e
    if g > max_groups:
        return Eligibility(False, f"{g} groups — too many to scan", e.known, g, e.largest, 0.0, counts)
    # a useful split: several groups, none dominant, little unstated
    balance = 1.0 - abs(largest - 0.35) / 0.65
    e.score = round(max(0.0, balance) * known * min(g, 6) / 6, 4)
    return e


def enforce(proposed: list, n: int, *, max_groups: int = 7, min_size: int = 2, dominant: float = 0.60) -> tuple:
    """A proposed segmentation → (groups, leftovers, notes). Single membership: the first group claiming an id keeps it.
    Ids outside the set are dropped. `notes` says what was corrected and whether one group dominates."""
    groups: list = []
    seen: set = set()
    notes: list = []
    for p in (proposed or []):
        name = str((p or {}).get("name") or "").strip()[:48]
        raw = (p or {}).get("ids") or []
        ids = []
        for i in raw:
            try:
                k = int(i)
            except (TypeError, ValueError):
                notes.append(f"dropped a non-numeric id in {name!r}"); continue
            if not (0 <= k < n):
                notes.append(f"dropped out-of-range id {k} in {name!r}"); continue
            if k in seen:
                continue
            seen.add(k); ids.append(k)
        if not name or len(ids) < min_size:
            if ids:
                seen -= set(ids)                 # give them back to "everything else"
            continue
        groups.append(Group(name=name, ids=ids, why=str((p or {}).get("why") or "").strip()[:80]))
    if len(groups) > max_groups:
        for g in groups[max_groups:]:
            seen -= set(g.ids)
        notes.append(f"kept {max_groups} of {len(groups)} groups")
        groups = groups[:max_groups]
    groups.sort(key=lambda g: -len(g.ids))
    leftovers = [i for i in range(n) if i not in seen]
    if groups and len(groups[0].ids) > dominant * n:
        notes.append(f"dominant: {groups[0].name!r} holds {len(groups[0].ids)}/{n}")
    return groups, leftovers, notes


def token_groups(token_sets: list, *, max_groups: int = 7, min_size: int = 2, ceiling: float = 0.6) -> tuple:
    """The free fallback. `token_sets[i]` is the row's tokens. A token held by more than `ceiling` of the rows is the
    query itself and explains nothing; the rest are scored by how usefully they split the set (a healthy minority, and
    a more specific token beats a bare word). Deterministic: same input, same groups."""
    n = len(token_sets or [])
    if not n:
        return [], []
    df: dict = {}
    for ts in token_sets:
        for t in (ts or ()):
            df[t] = df.get(t, 0) + 1

    def score(t: str) -> float:
        k = df[t]
        if k < min_size or k > max(min_size, ceiling * n):
            return -1.0
        share = k / n
        balance = 1.0 - abs(share - 0.25) / 0.75
        return balance * (1.0 + 0.35 * (" " in t) + 0.05 * min(len(t), 20) / 20)

    ranked = sorted((t for t in df if score(t) > 0), key=lambda t: (-score(t), -df[t], t))
    groups: list = []
    taken: set = set()
    for t in ranked:
        if len(groups) >= max_groups:
            break
        ids = [i for i, ts in enumerate(token_sets) if t in (ts or ()) and i not in taken]
        if len(ids) < min_size:
            continue
        taken |= set(ids)
        groups.append(Group(name=t, ids=ids, key=t, why="share this"))
    groups.sort(key=lambda g: -len(g.ids))
    return groups, [i for i in range(n) if i not in taken]
