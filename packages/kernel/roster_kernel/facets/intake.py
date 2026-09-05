"""INTAKE gates (kernel — docs/specs/guided-intake.md §2.1, §3): the shortest path from what a user has to
what a search needs. Pure functions over opaque names: the caller (vertical / app) supplies the artifact's
checklist state, which items and keys are REQUIRED, which keys are OPTIONAL, and the counts over the current
slice. Code decides WHETHER to ask (this module); a model decides HOW (the words) elsewhere.

Order of questions: missing REQUIRED checklist items → REQUIRED keys unknown in the contract → OPTIONAL keys
whose counts are spread. Each class has a budget. "Search now" forces READY from any stage."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from .contract import Contract, edit
from .schema import UNKNOWN

DEFAULT_BUDGETS = {"gaps": 4, "required": 2, "optional": 2}
MIN_SPREAD, MIN_KNOWN = 0.35, 0.5
ITEM_STATES = ("present", "missing", "weak", "answered", "skipped")


@dataclass
class Question:
    kind: str                                   # "item" (an artifact checklist item) | "key" (a contract key)
    name: str
    options: list = field(default_factory=list)  # [(value, count)] from the counts (unknown excluded), best first
    free_text: bool = True
    klass: str = ""                             # gaps | required | optional — the budget this question charges

    def to_dict(self) -> dict:
        return {"kind": self.kind, "name": self.name, "options": [[v, int(n)] for v, n in self.options], "free_text": self.free_text, "klass": self.klass}


@dataclass
class IntakeState:
    direction: str = ""
    artifact: dict = field(default_factory=dict)        # {"kind", "status": missing|present, "ref"}
    checklist: dict = field(default_factory=dict)       # item → ITEM_STATES
    contract: dict = field(default_factory=dict)        # Contract.to_dict()
    answers: dict = field(default_factory=dict)         # item / key → value (None = declined)
    asked: list = field(default_factory=list)           # names asked, in order
    counts_asked: dict = field(default_factory=lambda: {"gaps": 0, "required": 0, "optional": 0})
    budgets: dict = field(default_factory=lambda: dict(DEFAULT_BUDGETS))
    stage: str = "direction"                            # direction | artifact | questions | ready

    def to_dict(self) -> dict:
        return {"direction": self.direction, "artifact": dict(self.artifact), "checklist": dict(self.checklist), "contract": copy.deepcopy(self.contract),
                "answers": dict(self.answers), "asked": list(self.asked), "counts_asked": dict(self.counts_asked), "budgets": dict(self.budgets), "stage": self.stage}

    @classmethod
    def from_dict(cls, d: dict) -> "IntakeState":
        d = d or {}
        return cls(direction=str(d.get("direction") or ""), artifact=dict(d.get("artifact") or {}), checklist=dict(d.get("checklist") or {}),
                   contract=copy.deepcopy(d.get("contract") or {}), answers=dict(d.get("answers") or {}), asked=list(d.get("asked") or []),
                   counts_asked={**{"gaps": 0, "required": 0, "optional": 0}, **dict(d.get("counts_asked") or {})},
                   budgets={**DEFAULT_BUDGETS, **dict(d.get("budgets") or {})}, stage=str(d.get("stage") or "direction"))


def spread(counts: dict | None) -> tuple[float, float]:
    """(spread, known_share): spread = 1 − the largest share among KNOWN values; known_share = known / all."""
    counts = counts or {}
    known = {k: int(v) for k, v in counts.items() if k != UNKNOWN and int(v) > 0}
    total_known = sum(known.values())
    total = total_known + int(counts.get(UNKNOWN, 0) or 0)
    if not total_known or not total:
        return 0.0, 0.0
    return 1.0 - max(known.values()) / total_known, total_known / total


def worth_asking(counts: dict | None, *, constrained: bool, min_spread: float = MIN_SPREAD, min_known: float = MIN_KNOWN) -> bool:
    """An OPTIONAL question is worth asking only when the answer would change the results."""
    if constrained:
        return False
    s, known = spread(counts)
    return s >= min_spread and known >= min_known


def constrained(c: Contract | dict, key: str) -> bool:
    d = c.to_dict() if isinstance(c, Contract) else (c or {})
    for section in ("must", "prefer", "avoid"):
        if key in (d.get(section) or {}):
            return True
    return bool(d.get("center") and str((d.get("center") or {}).get("key")) == key)


def _options(counts: dict | None, top: int = 6) -> list:
    items = [(str(k), int(v)) for k, v in (counts or {}).items() if k != UNKNOWN and int(v) > 0]
    items.sort(key=lambda kv: -kv[1])
    return items[:top]


def next_question(state: IntakeState, *, required_items: list[str], required_keys: list[str], optional_keys: list[str],
                  counts: dict | None) -> Question | None:
    """The next question by the spec's order, or None when the intake is READY — in which case the state's
    stage is set to "ready" in place (the one mutation here; everything else is pure)."""
    counts = counts or {}
    c = state.contract or {}
    if state.stage == "ready":
        return None
    # 1) missing REQUIRED checklist items (weak / present / answered / skipped are never asked)
    if state.counts_asked.get("gaps", 0) < state.budgets.get("gaps", 0):
        for item in required_items:
            if state.checklist.get(item) == "missing" and item not in state.asked:
                return Question(kind="item", name=item, options=_options(counts.get(item)), klass="gaps")
    # 2) REQUIRED keys the contract leaves unknown
    if state.counts_asked.get("required", 0) < state.budgets.get("required", 0):
        for key in required_keys:
            if key not in state.asked and not constrained(c, key) and state.answers.get(key, "") is not None:
                return Question(kind="key", name=key, options=_options(counts.get(key)), klass="required")
    # 3) OPTIONAL keys whose counts are spread
    if state.counts_asked.get("optional", 0) < state.budgets.get("optional", 0):
        for key in optional_keys:
            if key in state.asked:
                continue
            if worth_asking(counts.get(key), constrained=constrained(c, key)):
                return Question(kind="key", name=key, options=_options(counts.get(key)), klass="optional")
    state.stage = "ready"
    return None


def apply_answer(state: IntakeState, q: Question, value, *, contract_key: str | None = None, mode: str = "must", klass: str | None = None) -> IntakeState:
    """A new state with the answer recorded: an item answer marks the checklist `answered` (`skipped` when the
    value is None) and, when `contract_key` names a key, lands in the contract under `mode`; a key answer edits
    the contract directly. Budgets are charged by class. Pure."""
    n = IntakeState.from_dict(state.to_dict())
    n.asked.append(q.name)
    n.answers[q.name] = value
    cls = klass or q.klass or ("gaps" if q.kind == "item" else "required")
    n.counts_asked[cls] = n.counts_asked.get(cls, 0) + 1
    key = contract_key if q.kind == "item" else q.name
    if q.kind == "item":
        n.checklist[q.name] = "skipped" if value is None else "answered"
    if key and value is not None and str(value).strip():
        c = Contract.from_dict(n.contract)
        for v in (value if isinstance(value, (list, tuple)) else [value]):
            c = edit(c, key, str(v), mode)
        n.contract = c.to_dict()
    if n.stage in ("direction", "artifact"):
        n.stage = "questions"
    return n


def search_now(state: IntakeState) -> IntakeState:
    n = IntakeState.from_dict(state.to_dict())
    n.stage = "ready"
    return n
