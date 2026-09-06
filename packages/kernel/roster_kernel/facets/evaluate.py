"""The EVALUATOR (kernel — spec §5): contract × store → rows, counts, coverage. Deterministic for a given
index state: no model call inside. Musts are re-applied to every row that reaches the pool, so a store
leg that widens (keywords, a lenient adapter) can never leak a row past the contract."""
from __future__ import annotations

from dataclasses import dataclass, field

from .contract import Contract, validate_contract
from .schema import UNKNOWN, FacetSchema, FacetType
from .store import FacetStore, _vals, matches_must

_DEFAULT_FLOOR, _SPAN = 0.40, 0.25


@dataclass
class FacetWeights:
    """Vertical-supplied ranking weights (the only judgment in the evaluator, and it is a small table)."""
    prefer: dict = field(default_factory=dict)     # key → weight per hit
    avoid: dict = field(default_factory=dict)      # key → weight per hit
    default_prefer: float = 0.10
    default_avoid: float = 0.10
    center_per_step: float = 0.06                  # per ordinal step beyond `span`
    max_hits_per_key: int = 3


def calibrated_pct(sim: float, floor: float | None = None, span: float = _SPAN) -> int:
    f = _DEFAULT_FLOOR if floor is None else float(floor)
    return int(max(1, min(99, round(100.0 * (float(sim or 0.0) - f) / span))))


def _label(schema: FacetSchema, key: str) -> str:
    k = schema.key(key)
    return (k.label or key).lower() if k else key


async def evaluate(contract: Contract, store: FacetStore, schema: FacetSchema, weights: FacetWeights | None = None,
                   *, noise_floor: float | None = None, depth: dict | None = None) -> dict:
    w = weights or FacetWeights()
    errs = validate_contract(contract, schema)
    if errs:
        raise ValueError("; ".join(errs))
    must = contract.must or {}
    # 1) POOL — every leg is asked with the musts; the union keeps the best similarity per id
    pool: dict[str, dict] = {}
    legs = {"semantic": 0, "enumerate": 0, "angles": 0}
    cap = max(int(contract.limit) * 6, 200)

    def _take(rows: list[dict], leg: str) -> None:
        for r in rows:
            rid = str(r.get("id"))
            if not rid:
                continue
            legs[leg] += 1
            cur = pool.get(rid)
            if cur is None or float(r.get("sim") or 0.0) > float(cur.get("sim") or 0.0):
                pool[rid] = dict(r)

    if contract.text:
        _take(await store.semantic(contract.kind, contract.text, must, cap=cap), "semantic")
        for a in (contract.angles or [])[:5]:
            _take(await store.semantic(contract.kind, str(a), must, cap=cap // 2), "angles")
    if not contract.text:
        # no text → the must-slice itself is the pool. WITH text the pool is the semantic neighbourhood only: an
        # enumerate leg would pour unrelated rows (sim 0) into it, and rank_by an ordinal then sorts them first
        _take(await store.enumerate(contract.kind, must, cap=cap), "enumerate")
    # 2) the contract is the law: re-filter (leaky-pool invariant) + explicit exclusions
    excl = {str(x) for x in (contract.exclude_ids or [])}
    rows = [r for rid, r in pool.items() if rid not in excl and matches_must(r, must, schema)]
    # 3) SCORE
    floor = noise_floor
    if floor is None and contract.text:
        try:
            floor = await store.noise_floor(contract.kind, contract.text)
        except Exception:   # noqa: BLE001
            floor = None
    center = contract.center or {}
    ckey, cval, cspan = str(center.get("key") or ""), str(center.get("value") or ""), int(center.get("span", 1) or 0) if center else 0
    unknown_by_key: dict[str, int] = {}
    for r in rows:
        sim = float(r.get("sim") or 0.0)
        score, reasons = sim, []
        avoid_pen = 0.0
        for key, vals in (contract.prefer or {}).items():
            hits = [v for v in _vals(r, key) if v in {str(x) for x in vals}]
            if hits:
                n = min(len(hits), w.max_hits_per_key)
                score += n * float(w.prefer.get(key, w.default_prefer)); reasons.append(f"prefers {_label(schema, key)}: " + ", ".join(hits[:3]))
        for key, vals in (contract.avoid or {}).items():
            hits = [v for v in _vals(r, key) if v in {str(x) for x in vals}]
            if hits:
                n = min(len(hits), w.max_hits_per_key)
                pen = n * float(w.avoid.get(key, w.default_avoid))
                score -= pen; avoid_pen += pen; reasons.append(f"avoid {_label(schema, key)}: " + ", ".join(hits[:3]))
        if ckey:
            have = _vals(r, ckey)
            d = min((schema.ordinal_distance(ckey, h, cval) for h in have if schema.ordinal_distance(ckey, h, cval) is not None), default=None)
            if d is None:
                reasons.append(f"{ckey}: unknown")
            elif d == 0:
                reasons.append(f"{ckey}: {have[0]} (at {cval})")
            elif d <= cspan:
                reasons.append(f"{ckey}: {have[0]} is {d} step{'s' if d > 1 else ''} from {cval} (nearby)")
            else:
                score -= (d - cspan) * float(w.center_per_step); reasons.append(f"{ckey}: {have[0]} is {d} steps from {cval}")
        for k in schema.for_kind(contract.kind):
            if k.navigable and k.type is not FacetType.set and not _vals(r, k.key):
                unknown_by_key[k.key] = unknown_by_key.get(k.key, 0) + 1
        r["score"] = round(score, 6)
        r["match_pct"] = calibrated_pct(sim, floor)
        # the relevance BAND: similarity less the avoid penalties (an avoid is a judgment that crosses bands — the
        # wrong field ranks below the right one however similar); prefers and the centre only reorder within it
        r["_band"] = int(calibrated_pct(sim - avoid_pen, floor)) // 5
        r["reasons"] = reasons
    # 4) RANK — with text, RELEVANCE is the primary axis: 5-point bands of the calibrated match; preferences,
    # avoids and the centre reorder WITHIN a band (a +0.1 bonus per preferred value must never lift a 25 %
    # match above a 60 % one — prod 2026-09-05: generic managers with repos outranked the Salesforce people)
    rb = contract.rank_by or "match"
    if rb == "match":
        if contract.text:
            rows.sort(key=lambda r: (-int(r.get("_band", 0)), -r["score"], str(r.get("id"))))
        else:
            rows.sort(key=lambda r: (-r["score"], str(r.get("id"))))
    else:
        k = schema.key(rb)
        if k is not None and k.type is FacetType.numeric:
            def _num(r):
                x = (r.get("numeric") or {}).get(rb)
                return (0, -float(x)) if x is not None else (1, 0.0)
            rows.sort(key=lambda r: (_num(r), -r["score"], str(r.get("id"))))
        else:
            def _ord(r):
                idx = [schema.ordinal_index(rb, v) for v in _vals(r, rb)]
                idx = [i for i in idx if i is not None]
                return (0, -max(idx)) if idx else (1, 0)
            rows.sort(key=lambda r: (_ord(r), -r["score"], str(r.get("id"))))
    # 5) COUNTS over the must-filtered index slice (the store's job) + COVERAGE
    counts = await store.counts(contract.kind, must, schema, depth=depth)
    coverage = {"pool": len(rows), "legs": legs, "excluded": len(excl), "unknown": {k.key: unknown_by_key.get(k.key, 0) for k in schema.for_kind(contract.kind) if k.navigable and k.type is not FacetType.set},
                "noise_floor": floor}
    # an EMPTY (or near-empty) slice with musts → say which must is doing it (leave-one-out; a few counts calls)
    slice_total = pool_from_counts(counts, schema, contract.kind)
    best = max((int(r.get("match_pct") or 0) for r in rows), default=0)
    weak = bool(rows) and bool(contract.text) and best < 45
    coverage["best_match"] = best
    coverage["weak"] = weak
    if must and (not rows or weak or (slice_total is not None and slice_total < 5)):
        try:
            coverage["diagnosis"] = await diagnose_musts(contract, lambda k, m: store.counts(k, m, schema), schema,
                                                         semantic_fn=(lambda k, t, m: store.semantic(k, t, m, cap=60)) if weak else None, floor=floor)
            coverage["diagnosis"]["reason"] = "empty" if not rows else ("weak" if weak else "small")
        except Exception:   # noqa: BLE001 — a diagnosis is an aid
            pass
    for r in rows:
        r.pop("_band", None)
    return {"rows": rows[: int(contract.limit)], "counts": counts, "coverage": coverage, "contract": contract.to_dict()}


def pool_from_counts(counts: dict | None, schema: FacetSchema, kind: str) -> int | None:
    """The must-slice size read off the counts: the total (known + unknown) of any navigable single-valued key.
    None when the counts carry no such key."""
    for k in schema.for_kind(kind):
        if k.navigable and k.type is not FacetType.set and not k.via and (counts or {}).get(k.key):
            return int(sum(int(v) for v in counts[k.key].values()))
    return None


async def diagnose_musts(contract: Contract, counts_fn, schema: FacetSchema, semantic_fn=None, floor: float | None = None) -> dict:
    """WHY is the result empty, tiny or WEAK? Leave-one-out over the musts: the slice size with each must removed
    and with each alone (`counts_fn(kind, must) -> counts`), and — when `semantic_fn(kind, text, must) -> rows`
    is given and the contract has text — the BEST calibrated match without each must. The caller words it; this
    only measures. Keys sorted by the best match restored, then by the rows restored."""
    must = dict(contract.must or {})
    kind = contract.kind
    base = pool_from_counts(await counts_fn(kind, must), schema, kind) or 0
    keys = []
    for key in must:
        without = {k: v for k, v in must.items() if k != key}
        alone = {key: must[key]}
        n_without = pool_from_counts(await counts_fn(kind, without), schema, kind) or 0
        n_alone = pool_from_counts(await counts_fn(kind, alone), schema, kind) or 0
        vals = must[key]
        item = {"key": key, "values": (list(vals) if isinstance(vals, list) else vals), "without": n_without, "alone": n_alone,
                "label": (schema.key(key).label if schema.key(key) else key)}
        if semantic_fn is not None and contract.text:
            try:
                rows = await semantic_fn(kind, contract.text, without)
                item["best_without"] = max((calibrated_pct(float(r.get("sim") or 0.0), floor) for r in rows), default=0)
            except Exception:   # noqa: BLE001
                pass
        keys.append(item)
    keys.sort(key=lambda d: (-d.get("best_without", 0), -d["without"], d["alone"]))
    return {"pool": base, "keys": keys}
