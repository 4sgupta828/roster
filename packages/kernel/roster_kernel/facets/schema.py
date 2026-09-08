"""Facet SCHEMA mechanics (kernel, domain-free — docs/specs/facet-contract-evaluator.md §2).

A schema is a tuple of typed keys. The kernel knows five structural TYPES and how each filters, ranks and
counts; it never knows what a key MEANS — keys, vocabularies, labels and guidance are supplied by the
vertical manifest. `unknown` is a first-class value for every type: it is counted, never guessed, never
satisfies a must, and is neutral under prefer / avoid / centre."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum

UNKNOWN = "unknown"


class FacetType(str, Enum):
    categorical = "categorical"
    ordinal = "ordinal"
    numeric = "numeric"
    set = "set"
    hierarchical = "hierarchical"


@dataclass(frozen=True)
class FacetKey:
    key: str
    type: FacetType
    kinds: tuple[str, ...]                 # entity kinds that carry this key
    label: str = ""
    guidance: str = ""                     # one line for the extractor / compiler prompt (vertical-written)
    values: tuple[str, ...] = ()           # categorical vocabulary; ordinal vocabulary IN ORDER
    bands: tuple[tuple[str, float | None, float | None], ...] = ()   # numeric: (band, lo inclusive, hi exclusive)
    unit: str = ""
    via: str = ""                          # facet-through-relation: this key is read off the related entity named by `via`
    navigable: bool = True                 # rendered in the rail and counted
    top_n: int = 12                        # set / long-tail keys: how many values the counts keep

    def __post_init__(self) -> None:
        if self.type in (FacetType.categorical, FacetType.ordinal) and not self.values:
            raise ValueError(f"facet key {self.key!r}: {self.type.value} needs a vocabulary")
        if self.type is FacetType.numeric and not self.bands:
            raise ValueError(f"facet key {self.key!r}: numeric needs bands")


_WS = re.compile(r"\s+")


def _norm_text(v: object) -> str:
    return _WS.sub(" ", str(v if v is not None else "")).strip().lower()


def _norm_token(v: object) -> str:
    return _norm_text(v).replace(" ", "_")


def _norm_path(v: object) -> str:
    return "/".join(seg for seg in (_norm_token(p) for p in _norm_text(v).split("/")) if seg)


@dataclass(frozen=True)
class FacetSchema:
    keys: tuple[FacetKey, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        seen = set()
        for k in self.keys:
            if k.key in seen:
                raise ValueError(f"duplicate facet key {k.key!r}")
            seen.add(k.key)

    # ---- lookup ----
    def key(self, key: str) -> FacetKey | None:
        for k in self.keys:
            if k.key == key:
                return k
        return None

    def for_kind(self, kind: str) -> list[FacetKey]:
        return [k for k in self.keys if kind in k.kinds]

    def kinds(self) -> tuple[str, ...]:
        out: list[str] = []
        for k in self.keys:
            for kd in k.kinds:
                if kd not in out:
                    out.append(kd)
        return tuple(out)

    # ---- values ----
    def validate_value(self, key: str, value: object) -> str | None:
        """The normalized form of `value` for `key`, or None when it is not a legal value (the caller
        stores UNKNOWN for categorical / ordinal / numeric; drops it for sets)."""
        k = self.key(key)
        if k is None or value is None:
            return None
        if k.type in (FacetType.categorical, FacetType.ordinal):
            t = _norm_token(value)
            return t if t in k.values else None
        if k.type is FacetType.numeric:
            t = _norm_token(value)
            return t if t in {b[0] for b in k.bands} else None
        if k.type is FacetType.set:
            t = _norm_text(value)
            return t or None
        if k.type is FacetType.hierarchical:
            t = _norm_path(value)
            return t or None
        return None

    def ordinal_distance(self, key: str, a: str, b: str) -> int | None:
        k = self.key(key)
        if k is None or k.type is not FacetType.ordinal:
            return None
        try:
            return abs(k.values.index(_norm_token(a)) - k.values.index(_norm_token(b)))
        except ValueError:
            return None

    def ordinal_index(self, key: str, value: str) -> int | None:
        k = self.key(key)
        if k is None or k.type is not FacetType.ordinal:
            return None
        try:
            return k.values.index(_norm_token(value))
        except ValueError:
            return None

    def band_of(self, key: str, number: float | int | None) -> str:
        k = self.key(key)
        if k is None or k.type is not FacetType.numeric or number is None:
            return UNKNOWN
        x = float(number)
        for name, lo, hi in k.bands:
            if (lo is None or x >= lo) and (hi is None or x < hi):
                return name
        return UNKNOWN

    def band_bounds(self, key: str, band: str) -> tuple[float | None, float | None] | None:
        k = self.key(key)
        if k is None:
            return None
        for name, lo, hi in k.bands:
            if name == band:
                return lo, hi
        return None

    @staticmethod
    def contains(key: str, path: str, prefix: str) -> bool:
        """Hierarchical containment: `path` lies under `prefix` (segment-wise; 'usa' is not under 'us')."""
        p, q = _norm_path(path).split("/"), _norm_path(prefix).split("/")
        return len(p) >= len(q) and p[: len(q)] == q

    # ---- identity ----
    def version(self) -> str:
        """A stable hash of the schema: stamped on every extraction so a vocabulary change is detectable."""
        canon = [{"key": k.key, "type": k.type.value, "kinds": list(k.kinds), "values": list(k.values),
                  "bands": [list(b) for b in k.bands], "unit": k.unit, "via": k.via} for k in self.keys]
        return hashlib.sha1(json.dumps(canon, sort_keys=True).encode()).hexdigest()[:12]

    # ---- prompt rendering (structure only; the words are the vertical's) ----
    def to_prompt_block(self, kind: str, *, include_via: bool = False) -> str:
        """The keys a model is told about. VIA keys are read off a related entity and are never extracted from this
        text, so an EXTRACTION prompt omits them — but a SEARCH compile may set them, so it asks for them."""
        lines = []
        for k in self.for_kind(kind):
            if k.via and not include_via:
                continue                         # read off the related entity, never extracted from this text
            if k.type is FacetType.ordinal:
                vocab = " < ".join(k.values)
            elif k.type is FacetType.categorical:
                vocab = " | ".join(k.values)
            elif k.type is FacetType.numeric:
                vocab = "a number" + (f" in {k.unit}" if k.unit else "") + " (or unknown)"
            elif k.type is FacetType.set:
                vocab = "a short list of normalized lowercase phrases"
            else:
                vocab = "a path like a/b/c"
            lines.append(f"- {k.key} ({k.type.value}{', ' + k.label if k.label else ''}): {vocab}"
                         + (f" — {k.guidance}" if k.guidance else "") + f"; use '{UNKNOWN}' when the text does not say")
        return "\n".join(lines)
