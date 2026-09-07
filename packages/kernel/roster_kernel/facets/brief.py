"""THE BRIEF and THE CONSULTANT MOVE (kernel — docs/specs/guided-consultant-v3.md §2–§4). Domain-free mechanics for a
conversational intake that is not a form:

- Brief: one structured understanding; every field = value + SOURCE (+ the evidence span). A field is KNOWN when its
  source is a document, a stored record, the user's words, or an answer; INFERRED is a guess; SKIPPED / UNKNOWN are
  gaps. The vertical names the fields; the kernel never does.
- Move: the planner's structured turn, PARSED AND GATED here: one question at most, ≤ N sentences, never a question
  on a known field (unless the move is `confirm`), every option's contract effect normalized against the schema,
  unknown fields rejected — whatever the model returned.
- Gates: a fork is asked only when its options survive the index (no zero-pool option) and actually differ;
  readiness = the required fields known or explicitly skipped; the budget of forks per intake.
- contract_from_brief: the brief → the ONE contract. A known field may filter (its mapped mode); an inferred field
  only ranks (prefer / center), never a must — the hardness rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .contract import Contract
from .intake import spread

KNOWN_SOURCES = ("document", "stored", "stated", "asked")
SOFT_SOURCES = ("inferred",)
GAP_SOURCES = ("skipped", "unknown")
SOURCES = KNOWN_SOURCES + SOFT_SOURCES + GAP_SOURCES
MOVES = ("infer", "confirm", "fork", "challenge", "trade_off", "draft", "split", "ready", "restart")
PLANNER_SOURCES = ("inferred", "stated")       # a planner may guess or record the user's words; `asked` / `document` are set by code


@dataclass
class Field:
    value: object
    source: str = "unknown"
    span: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "span": self.span, "note": self.note}


class Brief:
    def __init__(self, fields: dict | None = None):
        self.fields: dict[str, Field] = {}
        for k, v in (fields or {}).items():
            if isinstance(v, Field):
                self.fields[k] = v
            elif isinstance(v, dict) and "value" in v:
                self.fields[k] = Field(v.get("value"), str(v.get("source") or "unknown"), str(v.get("span") or ""), str(v.get("note") or ""))

    # ---- reading ----
    def get(self, key: str) -> Field | None:
        return self.fields.get(key)

    def value(self, key: str):
        f = self.fields.get(key)
        return f.value if f else None

    def source(self, key: str) -> str:
        f = self.fields.get(key)
        return f.source if f else "unknown"

    def known(self, key: str) -> bool:
        return self.source(key) in KNOWN_SOURCES and self.value(key) not in (None, "", [])

    def settled(self, key: str) -> bool:
        """Known or explicitly skipped — nothing left to ask."""
        return self.known(key) or self.source(key) == "skipped"

    def known_keys(self) -> list[str]:
        return [k for k in self.fields if self.known(k)]

    # ---- writing (pure: returns a new Brief) ----
    def with_field(self, key: str, value, source: str, span: str = "", note: str = "") -> "Brief":
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        n = Brief(self.to_dict())
        n.fields[key] = Field(value, source, span, note)
        return n

    def skipped(self, key: str) -> "Brief":
        n = Brief(self.to_dict())
        n.fields[key] = Field(None, "skipped")
        return n

    def to_dict(self) -> dict:
        return {k: f.to_dict() for k, f in self.fields.items()}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Brief":
        return cls(d or {})


def apply_brief_delta(brief: Brief, delta: dict, *, allowed_fields: set, sources: tuple = PLANNER_SOURCES) -> tuple[Brief, list[str]]:
    """A planner's brief delta, applied under the source rules: only `allowed_fields`; only `sources`; a KNOWN field is
    never overwritten by an inference (a guess cannot erase a fact); a stated value overwrites anything."""
    n = Brief(brief.to_dict())
    notes: list[str] = []
    for key, raw in (delta or {}).items():
        if key not in allowed_fields:
            notes.append(f"dropped unknown field {key!r}"); continue
        if not isinstance(raw, dict) or "value" not in raw:
            notes.append(f"dropped malformed {key!r}"); continue
        src = str(raw.get("source") or "inferred")
        if src not in sources:
            notes.append(f"dropped {key!r}: a planner cannot set source {src!r}"); continue
        if src == "inferred" and n.known(key):
            notes.append(f"kept the known {key!r} over an inference"); continue
        if raw.get("value") in (None, "", []):
            continue
        n.fields[key] = Field(raw.get("value"), src, str(raw.get("span") or ""), str(raw.get("note") or ""))
    return n, notes


# ---------------------------------------------------------------------------------------------------------------
# the move
# ---------------------------------------------------------------------------------------------------------------

@dataclass
class Question:
    field: str
    text: str
    why: str = ""
    options: list = field(default_factory=list)     # [{label, value, effect: {must|prefer|avoid: {key: [values]}, center?: {key, value}}}]
    multi: bool = False
    free_text: bool = True


@dataclass
class Move:
    move: str
    say: str
    brief_delta: dict = field(default_factory=dict)
    question: Question | None = None
    contract_delta: dict = field(default_factory=dict)
    ready: bool = False
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        q = None
        if self.question:
            q = {"field": self.question.field, "text": self.question.text, "why": self.question.why, "options": self.question.options,
                 "multi": self.question.multi, "free_text": self.question.free_text}
        return {"move": self.move, "say": self.say, "brief_delta": self.brief_delta, "question": q, "contract_delta": self.contract_delta,
                "ready": self.ready, "notes": list(self.notes)}


_SENT = re.compile(r"(?<=[.!?])\s+")


def truncate_sentences(text: str, n: int) -> str:
    parts = [p for p in _SENT.split(str(text or "").strip()) if p]
    return " ".join(parts[:n]).strip()


def normalize_effect(effect: dict, schema) -> tuple[dict, list[str]]:
    """{must|prefer|avoid: {key: [values]}, center: {key, value}} with every value normalized against the schema; what the
    index does not know is dropped (the model WILL invent values)."""
    out: dict = {}
    notes: list[str] = []
    for mode in ("must", "prefer", "avoid"):
        m = (effect or {}).get(mode)
        if not isinstance(m, dict):
            continue
        for key, vals in m.items():
            if schema.key(key) is None:
                notes.append(f"dropped unknown key {key!r}"); continue
            vals = vals if isinstance(vals, list) else [vals]
            legal = []
            for v in vals:
                t = schema.validate_value(key, v)
                if t is None:
                    notes.append(f"dropped {key}={v!r}: not in the index vocabulary")
                else:
                    legal.append(t)
            if legal:
                out.setdefault(mode, {})[key] = sorted(set(legal))
    c = (effect or {}).get("center")
    if isinstance(c, dict) and c.get("key") and schema.key(str(c["key"])) is not None:
        t = schema.validate_value(str(c["key"]), c.get("value"))
        if t is not None:
            out["center"] = {"key": str(c["key"]), "value": t, "span": int(c.get("span") or 1)}
        else:
            notes.append(f"dropped center {c.get('key')}={c.get('value')!r}")
    return out, notes


def parse_move(raw: dict, *, brief: Brief, schema, allowed_fields: set, max_sentences: int = 2, max_options: int = 4) -> Move:
    """The planner's output, made safe: a legal move name; `say` cut to `max_sentences`; ONE question at most, never
    on a known field unless the move is `confirm`; options' effects normalized; brief / contract deltas normalized;
    unknown keys dropped — every correction is a note."""
    raw = raw if isinstance(raw, dict) else {}
    notes: list[str] = []
    move = str(raw.get("move") or "").strip().lower()
    if move not in MOVES:
        notes.append(f"unknown move {move!r} → infer"); move = "infer"
    say = truncate_sentences(str(raw.get("say") or ""), max_sentences)
    if not say:
        say = "Tell me a little more about what you're after."
        notes.append("empty say → a safe prompt")
    # the question (one; the model may return a list)
    qraw = raw.get("question")
    if isinstance(qraw, list):
        if len(qraw) > 1:
            notes.append(f"{len(qraw)} questions → the first only")
        qraw = qraw[0] if qraw else None
    question = None
    if isinstance(qraw, dict) and str(qraw.get("text") or "").strip():
        fld = str(qraw.get("field") or "").strip()
        if fld and fld not in allowed_fields:
            notes.append(f"question on unknown field {fld!r} dropped"); fld = ""
        elif fld and brief.known(fld) and move != "confirm":
            notes.append(f"question on the known field {fld!r} dropped"); fld = ""
        elif fld and brief.source(fld) == "skipped" and move != "confirm":
            notes.append(f"question on the skipped field {fld!r} dropped"); fld = ""
        if fld or not qraw.get("field"):
            opts = []
            for o in (qraw.get("options") or [])[:max_options]:
                if not isinstance(o, dict) or not str(o.get("label") or "").strip():
                    continue
                eff, en = normalize_effect(o.get("effect") or {}, schema)
                notes += en
                opts.append({"label": str(o["label"]).strip()[:80], "value": o.get("value", o.get("label")), "effect": eff})
            question = Question(field=fld, text=str(qraw["text"]).strip()[:300], why=str(qraw.get("why") or qraw.get("why_it_matters") or "").strip()[:200],
                                options=opts, multi=bool(qraw.get("multi")), free_text=bool(qraw.get("free_text", True)))
    if question is None and move in ("fork", "confirm", "challenge", "trade_off"):
        notes.append(f"{move} without a question → infer"); move = "infer"
    # deltas
    bd: dict = {}
    for k, v in ((raw.get("brief_delta") or {}) if isinstance(raw.get("brief_delta"), dict) else {}).items():
        if k in allowed_fields and isinstance(v, dict):
            bd[k] = {"value": v.get("value"), "source": str(v.get("source") or "inferred"), "span": str(v.get("span") or "")[:240], "note": str(v.get("note") or "")[:120]}
        else:
            notes.append(f"dropped brief field {k!r}")
    cd, cn = normalize_effect(raw.get("contract_delta") or {}, schema)
    notes += cn
    return Move(move=move, say=say, brief_delta=bd, question=question, contract_delta=cd, ready=bool(raw.get("ready")) or move == "ready", notes=notes)


# ---------------------------------------------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------------------------------------------

def gate_fork(options: list[dict], pools: dict) -> tuple[list[dict], bool, str]:
    """Options that survive the index (pool > 0 when probed), and whether the fork is worth asking: at least two
    survive and their effects differ. Returns (kept, worth_asking, why)."""
    kept = []
    for o in options or []:
        n = pools.get(o.get("label"))
        if n is not None and int(n) <= 0:
            continue
        kept.append(o)
    if len(kept) < 2:
        return kept, False, "fewer than two options survive the index"
    effects = {repr(sorted((o.get("effect") or {}).items())) for o in kept}
    if len(effects) < 2:
        return kept, False, "the options would run the same search"
    return kept, True, ""


def readiness(brief: Brief, required: tuple, *, accept_inferred: bool = False) -> list[str]:
    """The required fields still open: neither known nor skipped — nor, when `accept_inferred`, inferred with a value (an
    inference is surfaced as an assumption and never filters; it does not have to be asked)."""
    def _open(k):
        if brief.settled(k):
            return False
        return not (accept_inferred and brief.source(k) in SOFT_SOURCES and brief.value(k) not in (None, "", []))
    return [k for k in required if _open(k)]


def leverage(counts: dict | None, schema, *, exclude: set = frozenset(), top: int = 3, min_spread: float = 0.3) -> list[dict]:
    """The keys whose values split the current pool the most (spread) — the measured forks a planner may pick from."""
    out = []
    for key, dist in (counts or {}).items():
        k = schema.key(key)
        if k is None or key in exclude or not isinstance(dist, dict):
            continue
        s, known = spread(dist)
        if s < min_spread or known < 0.5:
            continue
        vals = sorted(((v, int(n)) for v, n in dist.items() if v != "unknown"), key=lambda kv: -kv[1])[:4]
        out.append({"key": key, "spread": round(s, 2), "values": vals})
    out.sort(key=lambda x: -x["spread"])
    return out[:top]


def contract_from_brief(brief: Brief, mapping: dict, schema, *, kind: str, text: str, scope: dict | None = None, limit: int = 60) -> tuple[Contract, list[str]]:
    """The brief → the contract. `mapping` = {brief field: (contract key, mode)} with mode ∈ must | prefer | avoid |
    center. HARDNESS: a KNOWN field takes its mapped mode; an INFERRED field only ranks (must → prefer); skipped /
    unknown fields contribute nothing. Every value normalized; illegal values dropped with a note."""
    c = Contract(kind=kind, text=text[:500], scope=dict(scope or {}), limit=limit)
    notes: list[str] = []
    for fld, (key, mode) in (mapping or {}).items():
        f = brief.get(fld)
        if f is None or f.source in GAP_SOURCES or f.value in (None, "", []):
            continue
        if schema.key(key) is None:
            continue
        eff_mode = mode
        if f.source in SOFT_SOURCES and mode == "must":
            eff_mode = "prefer"; notes.append(f"{fld}: inferred → ranks, not filters")
        vals = f.value if isinstance(f.value, list) else [f.value]
        legal = [t for t in (schema.validate_value(key, v) for v in vals) if t is not None]
        if not legal:
            notes.append(f"{fld}: {vals!r} not in the index vocabulary"); continue
        if eff_mode == "center":
            c.center = {"key": key, "value": legal[0], "span": 1}
        else:
            sec = getattr(c, eff_mode)
            sec[key] = sorted(set(list(sec.get(key) or []) + legal))
    return c, notes
