"""The facets ENGINE (docs/specs/facet-contract-evaluator.md §3, §4): schema-driven extraction (the model reads
the source with the vocabulary supplied), code-owned normalization (compensation), and brief → contract
compilation. Every model call goes through an injected `llm_json(system, user) -> dict` so tests run without
a provider and the prompt is rendered from the schema, never hand-written vocabulary."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from roster_kernel.facets import UNKNOWN, Contract, FacetSchema, FacetType, validate_contract

HOURS_PER_YEAR, MONTHS_PER_YEAR = 2080.0, 12.0
_ANNUALIZE = {"year": 1.0, "yearly": 1.0, "annual": 1.0, "annually": 1.0, "per year": 1.0, "hour": HOURS_PER_YEAR, "hourly": HOURS_PER_YEAR,
              "per hour": HOURS_PER_YEAR, "month": MONTHS_PER_YEAR, "monthly": MONTHS_PER_YEAR, "per month": MONTHS_PER_YEAR}


def normalize_comp(raw: dict | None, *, employment_type: str = "") -> dict | None:
    """Stated pay → {min, max, number (the midpoint, annual USD), currency, period, display, undisclosed}.
    Assumptions are explicit: hourly × 2080, monthly × 12; only USD is annualized into a band; a contract or
    part-time posting is never annualized (undisclosed band, raw display kept); a bare word ('competitive')
    is undisclosed. Never estimates — figures must come from the source."""
    if not raw:
        return None
    if isinstance(raw, str):
        return {"display": raw.strip()[:120], "undisclosed": True} if raw.strip() else None
    lo, hi = raw.get("min"), raw.get("max")
    try:
        lo = float(lo) if lo not in (None, "") else None
        hi = float(hi) if hi not in (None, "") else None
    except (TypeError, ValueError):
        lo = hi = None
    cur = str(raw.get("currency") or "USD").upper().strip()
    period = str(raw.get("period") or "year").lower().strip()
    display = str(raw.get("display") or "").strip()[:120]
    if lo is None and hi is None:
        return {"display": display, "currency": cur, "period": period, "undisclosed": True} if display else None
    if not display:
        fmt = lambda x: f"{x:,.0f}"
        display = (f"{cur} {fmt(lo)}–{fmt(hi)}" if lo is not None and hi is not None else f"{cur} {fmt(lo if lo is not None else hi)}") + f" per {period}"
    factor = _ANNUALIZE.get(period)
    if employment_type in ("contract", "part_time") or factor is None or cur != "USD":
        return {"min": lo, "max": hi, "currency": cur, "period": period, "display": display, "undisclosed": True}
    a_lo = lo * factor if lo is not None else None
    a_hi = hi * factor if hi is not None else None
    mid = (a_lo + a_hi) / 2 if a_lo is not None and a_hi is not None else (a_lo if a_lo is not None else a_hi)
    return {"min": a_lo, "max": a_hi, "number": mid, "currency": cur, "period": period, "display": display, "undisclosed": False,
            "assumption": ("hourly × 2080" if factor == HOURS_PER_YEAR else "monthly × 12" if factor == MONTHS_PER_YEAR else "")}


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def clean_text(v) -> str:
    """Postgres jsonb / text reject NUL and other control bytes that scraped postings sometimes carry."""
    return _CTRL.sub("", str(v if v is not None else ""))


def _sys_prompt(kind: str, schema: FacetSchema) -> str:
    return ("You classify " + ("job postings" if kind == "job" else "professional profiles" if kind == "person" else "companies")
            + " into typed facets. For EACH item return an object {\"i\": index, <facet key>: <value>} using ONLY the keys and vocabularies below; "
            f"for numeric keys return an object {{\"min\", \"max\", \"currency\", \"period\"}} copied from the text; for list keys return a list. "
            f"Use '{UNKNOWN}' whenever the text does not state it — never guess, never estimate.\n\nKEYS:\n" + schema.to_prompt_block(kind)
            + "\n\nReturn STRICT JSON: {\"items\": [...]} with every index present.")


def _validate_item(kind: str, item: dict, schema: FacetSchema, *, provenance: str) -> dict:
    """The model's raw object → validated facet lists; off-vocabulary → dropped (categorical / ordinal are
    stored as unknown by absence); numeric → normalized in code."""
    facets: dict[str, list] = {}
    for k in schema.for_kind(kind):
        if k.via or k.key not in item:
            continue
        raw = item.get(k.key)
        if k.type is FacetType.numeric:
            comp = normalize_comp(raw if isinstance(raw, (dict, str)) else None, employment_type=str(item.get("employment_type") or ""))
            if comp and not comp.get("undisclosed") and comp.get("number") is not None:
                facets[k.key] = [{"number": comp["number"], "display": clean_text(comp["display"]), "confidence": 0.7, "provenance": provenance, "meta": comp}]
            elif comp and comp.get("display"):
                facets[k.key] = [{"value": UNKNOWN, "display": clean_text(comp["display"]), "confidence": 0.7, "provenance": provenance, "meta": comp}]
            continue
        vals = raw if isinstance(raw, list) else [raw]
        keep = []
        for v in vals:
            if v is None or str(v).strip().lower() == UNKNOWN:
                continue
            nv = schema.validate_value(k.key, clean_text(v))
            if nv is not None and nv not in keep:
                keep.append(nv)
        if keep:
            facets[k.key] = [{"value": v, "confidence": 0.7, "provenance": provenance} for v in keep[: (8 if k.type is FacetType.set else 3)]]
    return facets


def extract_envelopes(kind: str, items: list[dict], schema: FacetSchema, llm_json, *, provenance: str = "posting") -> list[dict]:
    """One model call for a batch → one envelope per item (aligned). `items` are the text fields the caller
    chose (for a job: title, company, department, location, head, tail, structured). Structured fields the
    caller passes under `structured` override the model for the same key with provenance 'ats_field'."""
    payload = [{"i": i, **{k: (clean_text(v) if isinstance(v, str) else v) for k, v in it.items() if k not in ("structured",)}} for i, it in enumerate(items)]
    try:
        out = llm_json(_sys_prompt(kind, schema), json.dumps({"items": payload}))
        got = {int(o.get("i")): o for o in (out.get("items") or []) if isinstance(o, dict) and str(o.get("i", "")).lstrip("-").isdigit()}
    except Exception:   # noqa: BLE001 — a failed batch writes nothing; the caller retries next pass
        return []
    env = []
    stamp = datetime.now(timezone.utc).isoformat()
    for i, it in enumerate(items):
        facets = _validate_item(kind, got.get(i) or {}, schema, provenance=provenance)
        # INVENTED PAY cannot pass: a model-read figure needs a dollar figure in the source text (verbatim-span rule)
        if "comp" in facets and facets["comp"] and facets["comp"][0].get("number") is not None:
            src = " ".join(str(it.get(k) or "") for k in ("head", "tail", "text"))
            if not pay_figures_present(src):
                facets.pop("comp", None)
        for key, val in (it.get("structured") or {}).items():          # structured beats text
            k = schema.key(key)
            if k is None or val in (None, ""):
                continue
            if k.type is FacetType.numeric:
                comp = normalize_comp(val, employment_type=str((it.get("structured") or {}).get("employment_type") or ""))
                if comp and not comp.get("undisclosed") and comp.get("number") is not None:
                    facets[key] = [{"number": comp["number"], "display": comp["display"], "confidence": 1.0, "provenance": "ats_field", "meta": comp}]
                continue
            nv = schema.validate_value(key, val)
            if nv is not None:
                facets[key] = [{"value": nv, "confidence": 1.0, "provenance": "ats_field"}]
        env.append({"schema_version": schema.version(), "extracted_at": stamp, "facets": facets})
    return env


_COMPILE_SYS = ("You compile a search brief into a facet CONTRACT. Return STRICT JSON with keys: must (object: facet key → list of values the results "
                "MUST have — ONLY hard requirements the brief states explicitly: a named company, a place, remote/hybrid, a pay floor, 'only senior' — a "
                "role or a field the brief merely describes is a PREFERENCE, not a must), prefer (object: key → values that should rank higher — the role, "
                "field, function, work type and level the brief describes go here), avoid (object: key → values to rank down), center (object {key, value, "
                "span} for ONE ordinal key when the brief names a level, span 1), angles (2–4 alternative phrasings or adjacent titles that would surface "
                "strong matches), intent (one sentence). Use ONLY the keys and vocabularies listed; omit what the brief does not say; never invent constraints.")

# A must is a promise the index must be able to keep: open-vocabulary keys (free phrases) cannot be promised
# exactly, so a compiled must on them becomes a prefer — except the employer and named skills, which ARE exact.
_EXACT_SET_KEYS = ("company", "skill", "country", "state", "metro")


def compile_contract(kind: str, text: str, schema: FacetSchema, llm_json, *, limit: int = 60, scope: dict | None = None) -> Contract:
    """Brief → contract via the model, validated by the schema: illegal keys / values are DROPPED (with the
    brief's text and angles kept), so a compile can only narrow legally, never fail."""
    c = Contract(kind=kind, text=(text or "").strip(), limit=limit, scope=dict(scope or {}))
    try:
        out = llm_json(_COMPILE_SYS + "\n\nKEYS:\n" + schema.to_prompt_block(kind), json.dumps({"brief": text}))
    except Exception:   # noqa: BLE001
        return c
    for section in ("must", "prefer", "avoid"):
        for key, vals in (out.get(section) or {}).items():
            k = schema.key(key)
            if k is None or kind not in k.kinds:
                continue
            if section == "must" and k.type is FacetType.set and key not in _EXACT_SET_KEYS:
                section_target = "prefer"                      # an open phrase cannot be a promise
            else:
                section_target = section
            if isinstance(vals, dict) and k.type is FacetType.numeric:
                rng = {b: float(vals[b]) for b in ("min", "max") if isinstance(vals.get(b), (int, float))}
                if rng:
                    getattr(c, section_target)[key] = rng
                continue
            vals = vals if isinstance(vals, list) else [vals]
            keep = [schema.validate_value(key, v) for v in vals]
            keep = [v for v in keep if v]
            if keep:
                cur = getattr(c, section_target).get(key)
                getattr(c, section_target)[key] = sorted(set((cur if isinstance(cur, list) else []) + keep))
    ctr = out.get("center") or {}
    if isinstance(ctr, dict) and ctr.get("key") and schema.key(str(ctr["key"])) and schema.key(str(ctr["key"])).type is FacetType.ordinal:
        v = schema.validate_value(str(ctr["key"]), ctr.get("value"))
        if v:
            c.center = {"key": str(ctr["key"]), "value": v, "span": int(ctr.get("span", 1) or 1)}
    c.angles = [str(a).strip() for a in (out.get("angles") or []) if str(a).strip()][:4]
    if validate_contract(c, schema):                 # belt and braces: anything still illegal is stripped
        for section in ("must", "prefer", "avoid"):
            for key in list(getattr(c, section)):
                if any(key in e for e in validate_contract(c, schema)):
                    del getattr(c, section)[key]
        if any("center" in e for e in validate_contract(c, schema)):
            c.center = None
    return c


def job_extraction_item(job: dict, *, head_chars: int = 400, tail_chars: int = 600) -> dict:
    """What the extractor sees for one posting: the head AND the tail of the body (pay ranges live at the
    end), plus structured board fields when the ingest captured them."""
    body = str(job.get("body") or "")
    item = {"title": str(job.get("title") or "")[:120], "company": str(job.get("company") or "")[:60], "department": str(job.get("department") or "")[:60],
            "location": str(job.get("location") or "")[:80], "head": body[:head_chars], "tail": (body[-tail_chars:] if len(body) > head_chars else "")}
    structured = {}
    for key in ("work_mode", "employment_type"):
        if job.get(key):
            structured[key] = job[key]
    if isinstance(job.get("comp_structured"), dict):
        structured["comp"] = job["comp_structured"]
    if structured:
        item["structured"] = structured
    return item


_MONEY = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}k)", re.I)


def pay_figures_present(text: str) -> bool:
    """A cheap gate before the model may return pay: the posting text must actually contain a dollar figure
    (a verbatim-span rule — invented pay cannot pass)."""
    return bool(_MONEY.search(text or ""))



def downgrade_uncovered_musts(contract: Contract, coverage_known: dict[str, float], *, min_known: float = 0.5) -> list[str]:
    """A must on a key the index barely knows (share of entities with a value < min_known) would filter by
    absence, not by fact — downgrade it to a prefer and say so. Returns the keys moved."""
    moved = []
    for key in list(contract.must):
        known = coverage_known.get(key)
        if known is not None and known < min_known and isinstance(contract.must[key], list):
            vals = contract.must.pop(key)
            cur = contract.prefer.get(key)
            contract.prefer[key] = sorted(set((cur if isinstance(cur, list) else []) + list(vals)))
            moved.append(key)
    return moved
