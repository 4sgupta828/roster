"""THE LEXICON — the deterministic first read of a query (kernel; docs/specs/query-intent-decoding.md §4).

Measured on prod: a query that IS a facet value, verbatim, was dropped. `remote` is a legal value of a
closed `work_mode` vocabulary and compiled to nothing; `staff` is a legal `level`; `austin` is a place
the vertical already knows how to canonicalise. The compiler is a model prompted to extract only what a
brief "states explicitly as a requirement", and one bare word states nothing — it is doing what it was
asked. The fix is not a better prompt. It is to look the words up first.

What this module is:

- **Deterministic.** No model, no I/O, no randomness. The same query yields the same spans forever.
- **Domain-free.** It reads the vocabularies out of the `FacetSchema` it is handed and the alias tables
  the caller supplies. It names no job, no place and no company; a legal or biotech vertical gets the
  same behaviour by supplying its own schema and aliases.
- **Not a decision.** It returns CANDIDATES with spans. Whether a candidate becomes a `must`, a
  `prefer`, or a question for the reader is `plan_from_spans` below, and its rules are stated there —
  because a lexicon hit is emphatically not always right: "remote possibility of travel",
  "staff the front desk", a person named Austin, companies literally named Square, Apple and Remote.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import FacetSchema, FacetType

_WORD = re.compile(r"[a-z0-9][a-z0-9+#._&-]*", re.I)


@dataclass(frozen=True)
class Span:
    """One stretch of the query that means a facet value."""
    text: str                    # the words as the reader typed them
    key: str                     # the facet key they name
    value: str                   # the CANONICAL value, ready for a contract
    start: int                   # token index, inclusive
    end: int                     # token index, exclusive
    source: str = "vocabulary"   # vocabulary | alias
    ambiguous: tuple[str, ...] = ()   # other keys this same text could legally mean


@dataclass
class LexiconPlan:
    """What the lexicon believes, and how strongly."""
    spans: list[Span] = field(default_factory=list)
    must: dict = field(default_factory=dict)      # only when the whole query is accounted for
    prefer: dict = field(default_factory=dict)    # otherwise, or when anything is uncertain
    residual: str = ""                            # the words no span claimed — the semantic text
    covered: bool = False                         # the query is nothing but facet values and filler
    notes: list[str] = field(default_factory=list)

    @property
    def blank_text(self) -> bool:
        """May the caller drop the semantic text and let the must-slice BE the pool?

        Only when the query is fully accounted for AND something actually narrows it. Blanking on a
        plan that produced only preferences would hand the evaluator an `enumerate` leg over the whole
        index and let its cap decide which arbitrary rows the reader sees — worse than the diffuse
        embedding it was meant to replace."""
        return self.covered and bool(self.must)


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).lower(), m.start(), m.end()) for m in _WORD.finditer(text or "")]


def find_spans(text: str, schema: FacetSchema, kind: str, *, aliases: dict | None = None,
               max_ngram: int = 3) -> list[Span]:
    """Every n-gram of `text` that is a legal value of a closed vocabulary for `kind`, or an alias of one.

    `aliases` is the vertical's own table: `{key: {surface form: canonical value}}`. It is how
    "new york" reaches `metro=nyc`, "head of" reaches `level=leadership` and "wfh" reaches
    `work_mode=remote` — none of which the schema's own value list contains.

    Longest match wins, left to right; a token is claimed once. A surface form that is legal under more
    than one key is returned once, with the alternatives recorded in `ambiguous` — deciding between them
    is not the lexicon's job."""
    toks = _tokens(text)
    if not toks:
        return []
    al = {k: {str(s).lower(): str(v) for s, v in (m or {}).items()} for k, m in (aliases or {}).items()}
    # the closed vocabularies, as {surface: key}
    vocab: dict[str, list[str]] = {}
    for k in schema.for_kind(kind):
        if k.type in (FacetType.categorical, FacetType.ordinal):
            for v in k.values:
                vocab.setdefault(str(v).lower().replace("_", " "), []).append(k.key)
    for key, m in al.items():
        if schema.key(key) is None:
            continue
        for surface in m:
            vocab.setdefault(surface, []).append(key)

    out: list[Span] = []
    i = 0
    while i < len(toks):
        hit = None
        for n in range(min(max_ngram, len(toks) - i), 0, -1):
            phrase = " ".join(t[0] for t in toks[i:i + n])
            keys = vocab.get(phrase)
            if not keys:
                continue
            key = keys[0]
            canon = al.get(key, {}).get(phrase) or phrase.replace(" ", "_")
            src = "alias" if phrase in al.get(key, {}) else "vocabulary"
            hit = Span(text=phrase, key=key, value=canon, start=i, end=i + n, source=src,
                       ambiguous=tuple(dict.fromkeys(keys[1:])))
            break
        if hit:
            out.append(hit)
            i = hit.end
        else:
            i += 1
    return out


def plan_from_spans(text: str, spans: list[Span], *, filler: frozenset = frozenset(),
                    must_keys: frozenset = frozenset(), risky_values: frozenset = frozenset()) -> LexiconPlan:
    """Spans → what the search should actually do with them.

    A LEXICON HIT IS A CANDIDATE, NEVER AN AUTOMATIC MUST. The first draft of this design said a
    closed-vocabulary match "cannot be wrong". It can: *remote* possibility of travel, *staff* the front
    desk, a candidate named *Austin*, a company named *Square*. A span is promoted to a `must` only when
    every one of these holds:

      1. the spans plus `filler` account for the WHOLE query — nothing was left unexplained;
      2. the key is one the vertical allows to be hardened (`must_keys`);
      3. the span is not ambiguous across keys;
      4. its surface form is not on the vertical's `risky_values` list — the values that are also
         ordinary English words, where a bare match is as likely to be grammar as intent — UNLESS the
         span is the entire query. "remote possibility of travel" leaves words unexplained and so never
         gets here; a lone "remote" has no grammar around it to misread, and filtering is what the
         reader plainly meant.

    Anything else ranks instead of filters, which is the fail-safe direction: a wrong `prefer` costs a
    few points, a wrong `must` empties the result set.

    `covered` is the other half of the fix. When the query is nothing but facet values, the search has
    no semantic content left — and the evaluator builds its pool from the must-slice when `text` is
    empty, and from a one-word embedding's diffuse neighbourhood when it is not. That is the difference
    between `remote` returning remote jobs and returning "Remote Opportunity — Take Back Control of Your
    Time"."""
    toks = _tokens(text)
    claimed = set()
    for s in spans:
        claimed.update(range(s.start, s.end))
    leftover = [t for i, t in enumerate(toks) if i not in claimed]
    residual_toks = [t[0] for t in leftover if t[0] not in filler]
    covered = bool(spans) and not residual_toks

    plan = LexiconPlan(spans=list(spans), covered=covered)
    alone = covered and len(spans) == 1          # the query IS this span, with nothing else in it
    for s in spans:
        hard = (covered and s.key in must_keys and not s.ambiguous
                and (s.text not in risky_values or alone))
        bag = plan.must if hard else plan.prefer
        vals = bag.setdefault(s.key, [])
        if s.value not in vals:
            vals.append(s.value)
        if not hard and covered:
            if s.ambiguous:
                plan.notes.append(f"“{s.text}” could be {s.key} or {', '.join(s.ambiguous)} — ranking on it, not filtering")
            elif s.text in risky_values:
                plan.notes.append(f"“{s.text}” is an ordinary word as well as a {s.key} — ranking on it, not filtering")
    # the words nobody claimed are what is left for the semantic leg
    plan.residual = " ".join(residual_toks)
    return plan
