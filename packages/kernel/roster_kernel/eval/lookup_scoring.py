"""Pure scoring math for the precision-lookup eval. No I/O, no DB, no domain.

Ported verbatim-in-spirit from the prior system's lookup/scoring.py — the
normalization + row-matching + cell-accuracy logic is already domain-free; only
the schema types are made generic here.
"""
from __future__ import annotations

import re

from .schema import LookupCase, LookupScore, LookupTrace

# Strip symbols that make "189.80" == "$189.80" == "189.8" for the same value.
_NORM_STRIPS = re.compile(r"[\$\s,]")


def _norm(s: str | None, *, strict: bool) -> str:
    if s is None:
        return ""
    t = str(s).strip().lower()
    if strict:
        return re.sub(r"\s+", " ", t)
    t = _NORM_STRIPS.sub("", t).rstrip("%")
    if "." in t:
        t = t.rstrip("0").rstrip(".") or "0"
    return t


def _row_key_id(d: dict[str, str]) -> tuple:
    return tuple(sorted((k, str(v).strip().lower()) for k, v in d.items()))


def score_lookup(case: LookupCase, trace: LookupTrace) -> LookupScore:
    emitted_by_key = {_row_key_id(r.row_key): r for r in trace.rows}

    matched = 0
    correct_cells = 0
    total_cells = 0
    for exp in case.expected_rows:
        total_cells += len(exp.cells)
        got = emitted_by_key.get(_row_key_id(exp.row_key))
        if got is None:
            continue
        matched += 1
        for col, want in exp.cells.items():
            strict = col in case.strict_columns
            if _norm(got.cells.get(col), strict=strict) == _norm(want, strict=strict):
                correct_cells += 1

    cell_accuracy = (correct_cells / total_cells) if total_cells else 0.0
    fully_correct = (
        trace.plan_emitted
        and not trace.failed
        and matched == len(case.expected_rows)
        and correct_cells == total_cells
    )
    return LookupScore(
        case_id=case.id,
        category=case.category,
        adversarial=case.adversarial,
        plan_emitted=trace.plan_emitted,
        plan_executed=not trace.failed,
        expected_rows=len(case.expected_rows),
        matched_rows=matched,
        cell_accuracy=cell_accuracy,
        fully_correct=fully_correct,
    )
