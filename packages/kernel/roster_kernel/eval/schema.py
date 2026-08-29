"""Generic (domain-agnostic) eval types.

The scoring MATH is the kernel's; the gold DATA and any domain vocab come from a
vertical. Row-key dimensions are caller-defined strings — the kernel never names
a domain column.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExpectedRow:
    """One gold row: a row_key (dimension→value) + expected cell values."""
    row_key: dict[str, str]
    cells: dict[str, str]  # column_name → expected value


@dataclass(frozen=True)
class LookupCase:
    id: str
    expected_rows: tuple[ExpectedRow, ...]
    category: str = ""
    adversarial: bool = False
    # Per-column strict literal match (adversarial cases); default loose numeric.
    strict_columns: frozenset[str] = frozenset()


@dataclass(frozen=True)
class EmittedRow:
    row_key: dict[str, str]
    cells: dict[str, str]


@dataclass
class LookupTrace:
    """What the system emitted for one case."""
    case_id: str
    rows: list[EmittedRow] = field(default_factory=list)
    plan_emitted: bool = True
    failed: bool = False


@dataclass
class LookupScore:
    case_id: str
    category: str
    adversarial: bool
    plan_emitted: bool
    plan_executed: bool
    expected_rows: int
    matched_rows: int
    cell_accuracy: float
    fully_correct: bool
