"""CONTRACT SEARCH mechanics (kernel — docs/specs/guided-intake.md §12): pure functions over opaque keys that let
the caller choose or merge contracts by MEASURING the index instead of guessing. The vertical decides which keys
are "relaxable" and the thresholds; the app supplies the counts. Nothing here names a domain.

- collapsing_musts: which compiled musts empty the pool (scarcity-gated, never the user's own, never a decisive
  must on a well-covered key).
- cooccurrence_readings: where a value actually lives (share, lift over background, support).
- rrf_fuse: merge several ranked lists into one (reciprocal rank fusion), remembering who found what."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Collapse:
    key: str
    pool_with: int
    pool_without: int
    reason: str


def collapsing_musts(pool_with: int, without: dict, *, user_keys: set, relaxable_keys: set,
                     min_ratio: float = 5.0, scarcity: int = 20) -> list[Collapse]:
    """Musts to demote to prefer. `without[key]` = the pool with that must removed. A must collapses the pool when
    removing it multiplies the pool by ≥ min_ratio AND the pool with it is below `scarcity` — the viability floor
    (a small but real pool is an answer, not a collapse). Never a user's own must; only keys the caller marked relaxable — a rare but decisive
    must on a well-covered key may be the target."""
    out = []
    base = max(int(pool_with), 0)
    for key, n in (without or {}).items():
        if key in user_keys or key not in relaxable_keys:
            continue
        n = int(n or 0)
        if base >= scarcity:
            continue
        if n >= max(base, 1) * min_ratio and n > base:
            out.append(Collapse(key=key, pool_with=base, pool_without=n, reason=f"without it {n} instead of {base}"))
    out.sort(key=lambda c: -c.pool_without)
    return out


@dataclass
class Reading:
    key: str            # the key the value was conditioned on (e.g. a specialty key)
    value: str
    target_key: str     # the key whose distribution was read (e.g. the field key)
    target_value: str
    share: float        # P(target_value | value) among carriers
    lift: float         # share / background share of target_value
    support: int        # carriers with both


def cooccurrence_readings(value_key: str, value: str, dist: dict, background: dict, *, target_key: str,
                          min_share: float = 0.25, min_lift: float = 1.5, min_support: int = 20) -> list[Reading]:
    """`dist` = counts of target values among entities carrying `value` (unknown excluded by the caller or here);
    `background` = counts of target values index-wide. A target value qualifies when its share among carriers ≥
    min_share, its lift over background ≥ min_lift and its support ≥ min_support. A value that lives 45 / 45 in
    two targets yields two readings; the index's mere majority does not qualify on its own."""
    d = {k: int(v) for k, v in (dist or {}).items() if k != "unknown" and int(v) > 0}
    b = {k: int(v) for k, v in (background or {}).items() if k != "unknown" and int(v) > 0}
    tot_d, tot_b = sum(d.values()), sum(b.values())
    if not tot_d or not tot_b:
        return []
    out = []
    for tv, n in d.items():
        share = n / tot_d
        bg = (b.get(tv, 0) / tot_b) if tot_b else 0.0
        lift = (share / bg) if bg > 0 else float("inf")
        if share >= min_share and lift >= min_lift and n >= min_support:
            out.append(Reading(key=value_key, value=value, target_key=target_key, target_value=tv, share=round(share, 3),
                               lift=(round(lift, 2) if lift != float("inf") else 99.0), support=n))
    out.sort(key=lambda r: (-r.share, -r.lift))
    return out


def rrf_fuse(lists: dict, *, k: int = 60, weights: dict | None = None, id_of=None) -> list[dict]:
    """Reciprocal rank fusion over named ranked lists ({name: [row, ...]}). Returns rows (one per id, the first
    seen copy) with `_fused` (the RRF score) and `_found_by` ({name: rank}) — the provenance the UI shows.
    Sorted by fused score desc, then by the best single rank, then id."""
    id_of = id_of or (lambda r: str(r.get("id") or r.get("entity_id") or ""))
    acc: dict[str, dict] = {}
    for name, rows in (lists or {}).items():
        w = float((weights or {}).get(name, 1.0))
        for rank, r in enumerate(rows or [], start=1):
            rid = id_of(r)
            if not rid:
                continue
            slot = acc.get(rid)
            if slot is None:
                slot = acc[rid] = {"row": dict(r), "score": 0.0, "found": {}}
            slot["score"] += w / (k + rank)
            slot["found"][name] = min(rank, slot["found"].get(name, rank))
    out = []
    for rid, slot in acc.items():
        row = slot["row"]
        row["_fused"] = round(slot["score"], 6)
        row["_found_by"] = dict(sorted(slot["found"].items(), key=lambda kv: kv[1]))
        out.append(row)
    out.sort(key=lambda r: (-r["_fused"], min(r["_found_by"].values()), str(id_of(r))))
    return out
