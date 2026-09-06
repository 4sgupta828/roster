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


# ---------------------------------------------------------------------------------------------------------------
# STEP 2 — recipes, survivors, blind judging, merged order (spec §12.3, §12.5, §12.6). Pure; the app measures.
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class Recipe:
    name: str
    contract: "object"       # a Contract (kept opaque here)
    why: str
    pool: int | None = None  # filled by the caller's probe


def recipes(c, *, user_keys: set, relaxable_keys: set, readings: list[dict] | None = None, max_recipes: int = 6) -> list[Recipe]:
    """The candidate ladder for one contract: `strict` (as ratified); `relaxed:<key>` per compiled must on a
    relaxable key (that must ranks instead); `loose` when two or more such keys exist (all of them rank);
    `reading:<key>=<value>` per alternative reading (a different value for a key the user did not set — the value
    is swapped in whichever section holds the key, or added as a must). The user's own keys are never touched.
    Identical contracts collapse; `strict` is always first."""
    from .contract import Contract, state_of  # local: keep the module importable without a cycle at load
    user_keys = set(user_keys or ())
    out: list[Recipe] = [Recipe("strict", Contract.from_dict(c.to_dict()), "as ratified")]
    seen = {_canon(out[0].contract)}

    def _add(name: str, contract, why: str):
        key = _canon(contract)
        if key in seen or len(out) >= max_recipes:
            return
        seen.add(key); out.append(Recipe(name, contract, why))

    relax = [k for k in c.must if k not in user_keys and k in set(relaxable_keys or ())]
    for k in relax:
        r = Contract.from_dict(c.to_dict())
        vals = r.must.pop(k)
        if isinstance(vals, list):
            r.prefer[k] = sorted(set([str(v) for v in (r.prefer.get(k) or [])] + [str(v) for v in vals]))
        _add(f"relaxed:{k}", r, f"{k} ranks instead of filtering")
    if len(relax) >= 2:
        r = Contract.from_dict(c.to_dict())
        for k in relax:
            vals = r.must.pop(k)
            if isinstance(vals, list):
                r.prefer[k] = sorted(set([str(v) for v in (r.prefer.get(k) or [])] + [str(v) for v in vals]))
        _add("loose", r, "every relaxable filter ranks instead")
    for rd in (readings or []):
        k, v = str(rd.get("key") or ""), str(rd.get("value") or "")
        if not k or not v or k in user_keys:
            continue
        cur = [str(x) for x in (c.must.get(k) or c.prefer.get(k) or [])]
        if cur == [v]:
            continue
        r = Contract.from_dict(c.to_dict())
        if k in r.must:
            r.must[k] = [v]
        elif k in r.prefer:
            r.prefer[k] = [v]
        else:
            r.must[k] = [v]
        _add(f"reading:{k}={v}", r, str(rd.get("why") or f"an alternative reading of {k}"))
    return out


def _canon(contract) -> str:
    d = contract.to_dict()
    return repr(sorted((k, repr(sorted(v.items()) if isinstance(v, dict) else v)) for k, v in d.items() if k not in ("limit", "exclude_ids")))


def survivors(rs: list[Recipe], sizes: dict, *, max_keep: int = 5) -> list[Recipe]:
    """Recipes whose probed pool is non-empty (an unprobed recipe is kept — the evaluate decides), `strict` first,
    then in ladder order, capped."""
    out = []
    for r in rs:
        n = sizes.get(r.name)
        r.pool = (int(n) if n is not None else None)
        if n is not None and int(n) <= 0:
            continue
        out.append(r)
    out.sort(key=lambda r: 0 if r.name == "strict" else 1)
    return out[:max_keep]


def blind(rows: list[dict], *, seed: int = 0, id_of=None) -> tuple[list[tuple[str, dict]], dict]:
    """Shuffle rows deterministically (seeded) and give each an opaque id — the judge sees neither source nor
    rank. Returns ([(blind_id, row), …], {blind_id: real id})."""
    import random
    id_of = id_of or (lambda r: str(r.get("id") or r.get("entity_id") or ""))
    order = list(rows)
    random.Random(seed).shuffle(order)
    items, mapping = [], {}
    for i, r in enumerate(order, start=1):
        bid = f"r{i}"
        items.append((bid, r)); mapping[bid] = id_of(r)
    return items, mapping


def resolve_blind_id(mapping: dict, raw) -> str | None:
    """The real id behind a judge's id as written: 'r3', '[r3]', 'R3', 3 or '3' all mean the third blind row."""
    t = str(raw if raw is not None else "").strip().strip("[]").strip().lower()
    if not t:
        return None
    if t.isdigit():
        t = "r" + t
    return mapping.get(t)


def order_by_verdicts(fused: list[dict], verdicts: dict, *, head: int, id_of=None) -> list[dict]:
    """Merged order: judged fits, then partial fits, then the ungraded tail in fused order; rows judged 'no' sink
    below the tail with the verdict attached. Every row carries `_fit` (yes | partial | no | None) and `_why`."""
    id_of = id_of or (lambda r: str(r.get("id") or r.get("entity_id") or ""))
    yes, partial, tail, no = [], [], [], []
    for i, r in enumerate(fused):
        v = verdicts.get(id_of(r)) if i < head else None
        fit = str((v or {}).get("fit") or "").lower() if v else None
        r = dict(r); r["_fit"] = fit if fit in ("yes", "partial", "no") else None; r["_why"] = str((v or {}).get("why") or "")[:80] if v else ""
        ({"yes": yes, "partial": partial, "no": no}.get(r["_fit"], tail)).append(r)
    return yes + partial + tail + no


def head_precision(rows: list[dict], verdicts: dict, *, k: int, partial: float = 0.4, weak_yes: float = 0.8, weak_ids: set | None = None, id_of=None) -> float:
    """Σ w(row) over the top k / k: yes = 1 (weak_yes when the row's evidence is only self-stated), partial =
    `partial`, no / ungraded = 0."""
    id_of = id_of or (lambda r: str(r.get("id") or r.get("entity_id") or ""))
    weak_ids = set(weak_ids or ())
    tot = 0.0
    for r in rows[:k]:
        rid = id_of(r)
        fit = str((verdicts.get(rid) or {}).get("fit") or "").lower()
        if fit == "yes":
            tot += weak_yes if rid in weak_ids else 1.0
        elif fit == "partial":
            tot += partial
    return round(tot / max(k, 1), 3)
