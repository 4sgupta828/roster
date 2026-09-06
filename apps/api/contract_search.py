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

import asyncio
import hashlib
import json
from typing import Awaitable, Callable

from roster_kernel.facets import Contract
from roster_kernel.facets.contract_search import blind, collapsing_musts, cooccurrence_readings, head_precision, order_by_verdicts, recipes, rrf_fuse, survivors

COOC_KEYS = ("specialty", "skill")
COOC_TARGET = "field"
TIER_KEY, TIER_VIEW = "level", "work_type"     # two views of one tier: a stated level rules out contradicting work types


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

    # 0) tier consistency: a preferred work type that contradicts the stated level is the compiler's slip, not a
    # wish (a CTO brief once preferred 'ic' and pulled individual contributors everywhere it ran)
    if TIER_VIEW in c.prefer and TIER_VIEW not in user_keys:
        from roster_vertical.intake import peer_work_types
        levels = [str(v) for v in (c.must.get(TIER_KEY) or [])] + [str(v) for v in (c.prefer.get(TIER_KEY) or [])]
        if c.center and str(c.center.get("key")) == TIER_KEY and c.center.get("value"):
            levels.append(str(c.center["value"]))
        before = [str(v) for v in (c.prefer.get(TIER_VIEW) or [])]
        kept = peer_work_types(levels, before)
        if kept != before:
            if kept:
                c.prefer[TIER_VIEW] = kept
            else:
                c.prefer.pop(TIER_VIEW, None)
            notes.append({"rule": "tier", "key": TIER_VIEW, "values": [v for v in before if v not in kept], "action": "dropped",
                          "why": f"contradicts the stated {TIER_KEY} ({', '.join(sorted(set(levels)))})"})

    # 1) disjunction: a place alongside a mode ranks, never filters (unless the user set it). The reading is noted
    # even when the compile already left the place under prefer — the card says why the place is not a filter
    if place_or_mode and "metro" not in user_keys:
        if "metro" in c.must:
            _to_prefer("metro", "the brief names the place as an alternative to remote / hybrid", "place_or_mode")
        else:
            notes.append({"rule": "place_or_mode", "key": "metro", "values": list(c.prefer.get("metro") or []),
                          "why": "the brief names the place as an alternative to remote / hybrid", "action": "ranks"})

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


# ---------------------------------------------------------------------------------------------------------------
# STEP 2 — MERGED SEARCH (spec §12.3, §12.5, §12.6): recipes → probes → survivors evaluated → RRF fusion → one blind
# judge over the head → merged order. Flag ROSTER_INTAKE_CONTRACT_SEARCH or a per-request `merge` on the contract.
# ---------------------------------------------------------------------------------------------------------------

def merge_options(cdict: dict | None) -> dict:
    """The per-request merge options riding on a contract dict: {"mode": "merged" | "single" | None, "off": [recipe
    names switched off]}. Unknown keys are ignored by Contract.from_dict, so they travel with the contract."""
    m = (cdict or {}).get("merge") if isinstance((cdict or {}).get("merge"), dict) else {}
    mode = str(m.get("mode") or "").lower() or None
    return {"mode": mode if mode in ("merged", "single") else None, "off": [str(x) for x in (m.get("off") or []) if str(x)][:8]}


async def merged_search(c: Contract, *, kind: str, user_keys: set, notes: list[dict] | None, evaluate_fn: Callable[[dict], Awaitable[dict]],
                        slice_fn: Callable[[str, dict], Awaitable[int]], llm_json: Callable[[str, str], dict], lines_fn=None,
                        off: list[str] | None = None, top: int = 60, log_fn=None) -> dict:
    """One search over several recipes, merged. Returns the evaluate shape (rows / counts / coverage / contract /
    labels — counts and coverage are the ratified contract's, the rail's shared layer) plus `merge`: the recipes
    with their numbers, the judge's tallies, WEAK. Rows carry `fit`, `fit_why`, `found_by`."""
    from roster_vertical.intake import JUDGE_HEAD, MERGE_RRF_K, RELAXABLE_KEYS, SELF_STATED_EVIDENCE, WEAK_FITS, judge_prompt, judge_row, reading_alternatives
    off = [str(x) for x in (off or [])]
    ladder = recipes(c, user_keys=set(user_keys or ()), relaxable_keys=set(RELAXABLE_KEYS), readings=reading_alternatives(notes))
    kept = [r for r in ladder if r.name not in off] or ladder[:1]
    sizes = await asyncio.gather(*[_safe_size(slice_fn, kind, r.contract.must) for r in kept])
    surv = survivors(kept, {r.name: n for r, n in zip(kept, sizes)})
    if not surv:
        surv = kept[:1]
    for r in surv:
        r.contract.limit = top
    outs = await asyncio.gather(*[evaluate_fn(r.contract.to_dict()) for r in surv])
    lists = {r.name: list(o.get("rows") or []) for r, o in zip(surv, outs)}
    fused = rrf_fuse(lists, k=MERGE_RRF_K)
    head = fused[:JUDGE_HEAD]
    # the judge: blind, normalized, once
    lines = {}
    if kind == "person" and lines_fn is not None and head:
        try:
            lines = await lines_fn([_rid(r) for r in head]) or {}
        except Exception:   # noqa: BLE001
            lines = {}
    seed = int(hashlib.sha1((c.text or "").encode("utf-8")).hexdigest()[:8], 16)
    items, mapping = blind(head, seed=seed, id_of=_rid)
    verdicts: dict = {}
    judge_error = None
    if items:
        user = ("BRIEF: " + (c.text or "")[:1200] + "\n\nROWS:\n" + "\n".join(judge_row(kind, bid, r, lines.get(_rid(r), "")) for bid, r in items))
        try:
            d = await asyncio.to_thread(llm_json, judge_prompt(kind), user)
            for v in (d.get("verdicts") or []):
                if not isinstance(v, dict):
                    continue
                rid = mapping.get(str(v.get("id") or ""))
                fit = str(v.get("fit") or "").lower()
                if rid and fit in ("yes", "partial", "no"):
                    verdicts[rid] = {"fit": fit, "why": str(v.get("why") or "")[:80]}
        except Exception as e:   # noqa: BLE001 — no judge → fused order stands, honestly ungraded
            judge_error = str(e)[:120]
    ordered = order_by_verdicts(fused, verdicts, head=JUDGE_HEAD, id_of=_rid)
    weak_ids = {_rid(r) for r in head if all(str(e) in SELF_STATED_EVIDENCE for e in ((r.get("facets") or {}).get("evidence") or [""]))}
    tallies = {k: sum(1 for v in verdicts.values() if v["fit"] == k) for k in ("yes", "partial", "no")}
    per_recipe = []
    for r, o in zip(surv, outs):
        rows_r = lists[r.name]
        ids_r = {_rid(x) for x in rows_r}
        per_recipe.append({"name": r.name, "why": r.why, "pool": r.pool if r.pool is not None else (o.get("coverage") or {}).get("pool"),
                           "evaluated": len(rows_r), "head_fits": sum(1 for rid, v in verdicts.items() if v["fit"] == "yes" and rid in ids_r),
                           "prec10": head_precision(rows_r, verdicts, k=10, weak_ids=weak_ids, id_of=_rid), "must": r.contract.must, "prefer": r.contract.prefer})
    strict = outs[0]
    rows = []
    for r in ordered[:top]:
        r = dict(r)
        r["fit"] = r.pop("_fit", None); r["fit_why"] = r.pop("_why", ""); r["found_by"] = r.pop("_found_by", {}); r.pop("_fused", None)
        rows.append(r)
    merge = {"recipes": per_recipe, "off": [n for n in off if any(x.name == n for x in ladder)], "ladder": [x.name for x in ladder],
             "head": len(head), "graded": len(verdicts), "fits": tallies["yes"], "partials": tallies["partial"], "nos": tallies["no"],
             "weak": bool(items) and tallies["yes"] < WEAK_FITS, "judge_error": judge_error,
             "prec10": head_precision(ordered, verdicts, k=10, weak_ids=weak_ids, id_of=_rid), "union": len(fused)}
    out = {"rows": rows, "counts": strict.get("counts") or {}, "coverage": dict(strict.get("coverage") or {}), "contract": strict.get("contract") or c.to_dict(),
           "labels": strict.get("labels"), "merge": merge}
    if log_fn is not None:
        try:
            await log_fn({"kind": kind, "brief_hash": hashlib.sha1((c.text or "").encode("utf-8")).hexdigest()[:16], "text": (c.text or "")[:300],
                          "recipes": [{k: v for k, v in x.items() if k not in ("must", "prefer")} for x in per_recipe], "tallies": tallies, "weak": merge["weak"], "off": merge["off"]})
        except Exception:   # noqa: BLE001 — the log is an aid
            pass
    return out


def _rid(r: dict) -> str:
    return str(r.get("id") or r.get("entity_id") or "")


async def _safe_size(slice_fn, kind, must):
    try:
        return await slice_fn(kind, dict(must))
    except Exception:   # noqa: BLE001
        return None


_LOG_DDL = """
CREATE TABLE IF NOT EXISTS roster_intake_choice (
  id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  kind TEXT NOT NULL,
  brief_hash TEXT NOT NULL,
  text TEXT,
  record JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ric_created ON roster_intake_choice (created_at DESC);
"""


def choice_logger(get_pool):
    """The learning log (spec §12.7, additive DDL): one row per merged search with the recipes' numbers and the
    judge's tallies. v1 reads it only in the eval report."""
    state = {"ddl": False}

    async def log(record: dict) -> None:
        pool = await get_pool()
        if pool is None:
            return
        async with pool.acquire() as conn:
            if not state["ddl"]:
                await conn.execute(_LOG_DDL); state["ddl"] = True
            await conn.execute("INSERT INTO roster_intake_choice (kind, brief_hash, text, record) VALUES ($1, $2, $3, $4::jsonb)",
                               str(record.get("kind") or ""), str(record.get("brief_hash") or ""), str(record.get("text") or ""), json.dumps(record))
    return log
