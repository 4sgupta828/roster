"""The CONTRACT grammar (kernel — docs/specs/facet-contract-evaluator.md §4): what a search / navigation /
calibration / refresh asks for, as data. Within a key OR, across keys AND; `must` filters, `prefer` /
`avoid` rank, `center` ranks by ordinal distance, `rank_by` sorts. Editing is a pure function; the rail's
tap cycles off → must → prefer → avoid → off. Serialization is canonical so contracts hash and diff."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from .schema import UNKNOWN, FacetSchema, FacetType

MODES = ("off", "must", "prefer", "avoid")
_CYCLE = {"off": "must", "must": "prefer", "prefer": "avoid", "avoid": "off"}


@dataclass
class Contract:
    kind: str
    text: str = ""
    must: dict = field(default_factory=dict)      # key → [values] | {"min":..,"max":..} (numeric)
    prefer: dict = field(default_factory=dict)    # key → [values]
    avoid: dict = field(default_factory=dict)     # key → [values]
    center: dict | None = None                    # {"key","value","span"} over an ordinal key
    rank_by: str = "match"                        # "match" | a numeric / ordinal key
    scope: dict = field(default_factory=dict)
    exclude_ids: list = field(default_factory=list)
    limit: int = 60
    angles: list = field(default_factory=list)    # extra semantic legs the compile step proposed

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "text": self.text, "must": _canon_map(self.must), "prefer": _canon_map(self.prefer),
             "avoid": _canon_map(self.avoid), "center": (dict(self.center) if self.center else None), "rank_by": self.rank_by,
             "scope": dict(sorted((self.scope or {}).items())), "exclude_ids": sorted(str(x) for x in self.exclude_ids),
             "limit": int(self.limit), "angles": list(self.angles)}
        return {k: d[k] for k in sorted(d)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    def digest(self) -> str:
        import hashlib
        return hashlib.sha1(self.to_json().encode()).hexdigest()[:12]

    @classmethod
    def from_dict(cls, d: dict) -> "Contract":
        d = d or {}
        return cls(kind=str(d.get("kind") or ""), text=str(d.get("text") or ""), must=dict(d.get("must") or {}),
                   prefer=dict(d.get("prefer") or {}), avoid=dict(d.get("avoid") or {}), center=(dict(d["center"]) if d.get("center") else None),
                   rank_by=str(d.get("rank_by") or "match"), scope=dict(d.get("scope") or {}),
                   exclude_ids=list(d.get("exclude_ids") or []), limit=int(d.get("limit") or 60), angles=list(d.get("angles") or []))


def _canon_map(m: dict) -> dict:
    out = {}
    for k in sorted(m or {}):
        v = m[k]
        if isinstance(v, dict):
            out[k] = {kk: v[kk] for kk in sorted(v)}
        else:
            out[k] = sorted({str(x) for x in (v or [])})
    return out


def validate_contract(c: Contract, schema: FacetSchema) -> list[str]:
    """Every problem, in words; an empty list means the contract is legal for this schema."""
    errs: list[str] = []
    if c.kind not in schema.kinds():
        errs.append(f"kind {c.kind!r} is not in the schema")
    for section in ("must", "prefer", "avoid"):
        for key, vals in (getattr(c, section) or {}).items():
            k = schema.key(key)
            if k is None or (c.kind in schema.kinds() and c.kind not in k.kinds):
                errs.append(f"{section}: unknown key {key!r} for kind {c.kind!r}")
                continue
            if isinstance(vals, dict):
                if k.type is not FacetType.numeric:
                    errs.append(f"{section}: a range is only legal on a numeric key ({key!r} is {k.type.value})")
                for b in ("min", "max"):
                    if b in vals and not isinstance(vals[b], (int, float)):
                        errs.append(f"{section}: {key}.{b} must be a number")
                continue
            for v in vals or []:
                if str(v) == UNKNOWN:
                    errs.append(f"{section}: {key!r} cannot target '{UNKNOWN}'")
                elif schema.validate_value(key, v) is None:
                    errs.append(f"{section}: {v!r} is not a legal value of {key!r}")
    if c.center:
        key = str(c.center.get("key") or "")
        k = schema.key(key)
        if k is None or k.type is not FacetType.ordinal:
            errs.append(f"center: key {key!r} is not an ordinal key")
        elif schema.validate_value(key, c.center.get("value")) is None:
            errs.append(f"center: {c.center.get('value')!r} is not a legal value of {key!r}")
        try:
            int(c.center.get("span", 1))
        except (TypeError, ValueError):
            errs.append("center: span must be an integer")
    if c.rank_by != "match":
        k = schema.key(c.rank_by)
        if k is None or k.type not in (FacetType.numeric, FacetType.ordinal):
            errs.append(f"rank_by: {c.rank_by!r} must be 'match' or a numeric / ordinal key")
    if int(c.limit) <= 0:
        errs.append("limit must be positive")
    return errs


def state_of(c: Contract, key: str, value: str) -> str:
    v = str(value)
    for mode in ("must", "prefer", "avoid"):
        vals = (getattr(c, mode) or {}).get(key)
        if isinstance(vals, list) and v in {str(x) for x in vals}:
            return mode
    return "off"


def edit(c: Contract, key: str, value: str, mode: str = "cycle") -> Contract:
    """A new contract with `value` set to `mode` on `key` (or advanced one step around the cycle).
    Pure: the input is never mutated."""
    if mode == "cycle":
        mode = _CYCLE[state_of(c, key, value)]
    if mode not in MODES:
        raise ValueError(f"unknown edit mode {mode!r}; expected one of {MODES} or 'cycle'")
    n = copy.deepcopy(c)
    v = str(value)
    for section in ("must", "prefer", "avoid"):
        m = getattr(n, section)
        vals = m.get(key)
        if isinstance(vals, list):
            m[key] = [x for x in vals if str(x) != v]
            if not m[key]:
                del m[key]
    if mode != "off":
        m = getattr(n, mode)
        cur = m.get(key)
        m[key] = (list(cur) if isinstance(cur, list) else []) + [v]
    return n
