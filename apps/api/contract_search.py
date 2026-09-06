"""INDEX-AWARE COMPILE (docs/specs/guided-intake.md §12, step 1 — deterministic, no model): after the compile,
before anything runs, the contract is checked against the index and corrected by three rules whose thresholds
live in the kernel mechanics:

1. DISJUNCTION — a place named as an alternative to a mode ("remote or Seattle"; the compile reports
   `place_or_mode`) makes the metro a preference, never a must (for people there is no work-mode key at all).
2. COLLAPSING MUSTS — a compiled must (never the user's) that leaves fewer than the viability floor (20) while
   its removal multiplies the pool ≥ 5×, on a relaxable key (an open set key; a low-coverage key; a place with a
   mode), is demoted to prefer. Thirty compiler engineers is a valid small answer, not a collapse.
3. CO-OCCURRENCE — where the brief's specialties / skills actually live: when the model's field has almost no
   support among the value's carriers and a data field qualifies (share, lift, support), the field is corrected;
   otherwise the data readings ride along as notes for the card.

Every change is recorded in `notes` so the ready card can say what happened and why."""
from __future__ import annotations

from typing import Awaitable, Callable

from roster_kernel.facets import Contract
from roster_kernel.facets.contract_search import collapsing_musts, cooccurrence_readings

COOC_KEYS = ("specialty", "skill")
COOC_TARGET = "field"


async def index_aware(c: Contract, *, kind: str, schema, user_keys: set, slice_fn: Callable[[str, dict], Awaitable[int]],
                      counts_fn: Callable[[str, dict], Awaitable[dict]], coverage: dict | None, place_or_mode: bool = False,
                      min_ratio: float = 5.0, scarcity: int = 20, weak_model_share: float = 0.10, max_values: int = 3) -> tuple[Contract, list[dict]]:
    notes: list[dict] = []
    c = Contract.from_dict(c.to_dict())
    user_keys = set(user_keys or ())
    coverage = coverage or {}

    def _to_prefer(key: str, why: str, rule: str):
        vals = c.must.pop(key, None)
        if isinstance(vals, list) and vals:
            c.prefer[key] = sorted(set([str(v) for v in (c.prefer.get(key) or [])] + [str(v) for v in vals]))
        notes.append({"rule": rule, "key": key, "values": vals, "why": why, "action": "must → prefer"})

    # 1) disjunction: a place alongside a mode ranks, never filters (unless the user set it)
    if place_or_mode and "metro" in c.must and "metro" not in user_keys:
        _to_prefer("metro", "the brief names the place as an alternative to remote / hybrid", "place_or_mode")

    # 2) collapsing musts (scarcity-gated, relaxable keys only)
    compiled = [k for k in c.must if k not in user_keys]
    if compiled:
        try:
            pool_with = await slice_fn(kind, dict(c.must))
        except Exception:   # noqa: BLE001
            pool_with = None
        if pool_with is not None and pool_with < scarcity:
            without: dict = {}
            for key in compiled:
                m2 = {k: v for k, v in c.must.items() if k != key}
                try:
                    without[key] = await slice_fn(kind, m2)
                except Exception:   # noqa: BLE001
                    continue
            from roster_vertical.intake import RELAXABLE_KEYS
            relaxable = set()
            for key in compiled:
                k = schema.key(key)
                if k is None:
                    continue
                if key in RELAXABLE_KEYS or float(coverage.get(key, 1.0)) < 0.5 or (key == "metro" and place_or_mode):
                    relaxable.add(key)
            for col in collapsing_musts(pool_with, without, user_keys=user_keys, relaxable_keys=relaxable, min_ratio=min_ratio, scarcity=scarcity):
                _to_prefer(col.key, col.reason, "collapsing")

    # 3) co-occurrence: where the named specialties / skills live
    readings: list[dict] = []
    values = [(key, str(v)) for key in COOC_KEYS for v in list((c.must.get(key) or []) if isinstance(c.must.get(key), list) else []) + list(c.prefer.get(key) or [])]
    values = values[:max_values]
    if values and schema.key(COOC_TARGET) is not None:
        try:
            background = (await counts_fn(kind, {})).get(COOC_TARGET) or {}
        except Exception:   # noqa: BLE001
            background = {}
        model_fields = set(str(v) for v in (c.must.get(COOC_TARGET) or [])) | set(str(v) for v in (c.prefer.get(COOC_TARGET) or []))
        for key, v in values:
            try:
                dist = (await counts_fn(kind, {key: [v]})).get(COOC_TARGET) or {}
            except Exception:   # noqa: BLE001
                continue
            rs = cooccurrence_readings(key, v, dist, background, target_key=COOC_TARGET)                       # alternatives: need LIFT
            home = cooccurrence_readings(key, v, dist, background, target_key=COOC_TARGET, min_share=0.5, min_lift=0.0)   # the value's home field: share alone
            known = sum(int(n) for k2, n in dist.items() if k2 != "unknown")
            model_share = max((int(dist.get(f, 0)) / known) for f in model_fields) if (model_fields and known) else None
            seen_fields = set()
            for r in list(home) + list(rs):                       # the value's home field first, then lifted alternatives
                if r.target_value in seen_fields:
                    continue
                seen_fields.add(r.target_value)
                readings.append({"rule": "cooccurrence", "value_key": key, "value": v, "field": r.target_value, "share": r.share, "lift": r.lift, "support": r.support,
                                 "home": r in home, "model_share": (round(model_share, 3) if model_share is not None else None)})
            # CORRECT the field when the model's field has almost no support among the value's carriers and the value has
            # a clear home field (≥ 50 % of carriers; lift not required — the home may be the index's majority)
            if home and model_fields and model_share is not None and model_share < weak_model_share and COOC_TARGET not in user_keys:
                best = home[0]
                for section in (c.must, c.prefer):
                    if COOC_TARGET in section:
                        section[COOC_TARGET] = [best.target_value]
                notes.append({"rule": "cooccurrence", "key": COOC_TARGET, "values": sorted(model_fields), "action": f"field → {best.target_value}",
                              "why": f"'{v}' lives {int(best.share * 100)} % in {best.target_value}; {sorted(model_fields)} holds {int((model_share or 0) * 100)} % of its carriers"})
                model_fields = {best.target_value}
    if readings:
        notes.append({"rule": "readings", "readings": readings[:6]})
    return c, notes
