"""THE INTENT DEBUGGER (kernel — docs/specs/intent-convergence-loop.md §3, rev 2).

The first version of this offered facet chips — "Level: senior", "State: ca" — and the owner's verdict
was immediate and correct: *"I can do the same from existing chips. What am I getting more with these
directions that the existing rail chips can't get me?"* Nothing. A chip FILTERS WHAT CAME BACK, which is
what the rail already does, key by key, with counts. Curating three of them is not a new capability.

What is missing is upstream of filtering: **did we understand the question at all?** That is not a
count, it is a reading — and a reading has to be argued in words, because the whole point is that two
readings of the same words retrieve different things. "Platform engineer" is infrastructure, or
developer tooling, or a product API surface; no facet distinguishes those, and no chip can ask.

So this module models a debugger, not a filter:

- it states the HYPOTHESIS currently in force — what the search believes you meant — because a debugger
  that will not show its current value is just a prompt;
- it offers a few DISTINCT READINGS in prose, each carrying the contract edit it implies, so choosing
  one re-runs the search rather than narrowing the last one;
- it leaves the door open — the reader can answer in their own words instead of picking.

The mechanics are here; every word shown to a person comes from the vertical's prompt, and every facet
value the model proposes is validated against the schema before it can reach a contract. A reading that
cannot be grounded in the vocabulary is dropped, exactly as a compiled contract's illegal values are.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .schema import FacetSchema, FacetType


@dataclass
class Reading:
    """One way to understand the query, and what the search would become if it is the right one."""
    label: str                                  # a few words: "Infrastructure platform"
    says: str = ""                              # one sentence, in the reader's language
    must: dict = field(default_factory=dict)    # validated facet values
    prefer: dict = field(default_factory=dict)
    text: str = ""                              # the semantic query this reading would run

    def to_dict(self) -> dict:
        return {"label": self.label, "says": self.says, "must": dict(self.must),
                "prefer": dict(self.prefer), "text": self.text}


@dataclass
class IntentCheck:
    """A consultant's turn: what it has established, what it notices, what it believes, what it asks.

    The four parts are deliberately separate, because they are four different kinds of claim and a
    reader has to be able to correct any of them independently. `understanding` is what it now takes
    as settled — the notes a consultant reads back to you; `noticed` is an observation about the
    result set that a person would have had to scroll to see; `believed` is the hypothesis currently
    driving the search; `question` and `readings` are the one thing it wants to know next."""
    understanding: list = field(default_factory=list)   # established, and carried forward
    noticed: str = ""                           # what the results themselves say, read like an analyst
    believed: str = ""                          # the hypothesis in force, in words
    question: str = ""                          # the single thing it wants to know next
    readings: list = field(default_factory=list)
    ask: str = ""                               # the open door: answer in your own words

    def to_dict(self) -> dict:
        return {"understanding": list(self.understanding), "noticed": self.noticed,
                "believed": self.believed, "question": self.question,
                "readings": [r.to_dict() for r in self.readings], "ask": self.ask}


def history_pack(history: list | None, *, max_turns: int = 6) -> list[dict]:
    """The conversation so far, reduced to what changes the next question.

    A debugger that forgets the last exchange asks the same question twice, and worse, re-offers a
    reading the reader has already turned down. Each turn keeps only four things: what they asked, what
    was offered, which reading they took, and anything they said in their own words — the last of which
    is the most valuable signal in the whole loop, because it is unprompted."""
    out: list[dict] = []
    for t in (history or [])[-max_turns:]:
        if not isinstance(t, dict):
            continue
        turn = {"asked": str(t.get("asked") or "").strip()[:200]}
        offered = [str(x)[:60] for x in (t.get("offered") or []) if str(x).strip()][:4]
        if offered:
            turn["offered"] = offered
        if str(t.get("chose") or "").strip():
            turn["chose"] = str(t["chose"]).strip()[:60]
        if str(t.get("told") or "").strip():
            turn["told"] = str(t["told"]).strip()[:200]
        if t.get("pool"):
            turn["pool"] = int(t["pool"])          # so the next turn can say what changed
        est = [str(u).strip()[:120] for u in (t.get("understood") or []) if str(u).strip()][:6]
        if est:
            turn["understood"] = est
        if turn.get("asked") or turn.get("chose") or turn.get("told"):
            out.append(turn)
    return out


def evidence(query: str, contract, rows: list, counts: dict | None, schema: FacetSchema,
             *, kind: str, max_titles: int = 12, history: list | None = None) -> dict:
    """The compact, typed picture a reader would need to judge whether the search understood them.

    Deliberately small: what was asked, what the search decided, what actually came back — and the
    conversation so far, so the next question builds on the last instead of restarting it."""
    titles, companies = [], []
    for r in (rows or [])[:max_titles]:
        t = str((r or {}).get("title") or "").strip()
        if t:
            titles.append(t)
        c = str((r or {}).get("company") or "").strip()
        if c:
            companies.append(c.replace("_", " "))
    spread = {}
    for key, dist in (counts or {}).items():
        k = schema.key(key)
        if k is None or not isinstance(dist, dict) or k.type is FacetType.numeric:
            continue
        known = {v: int(n) for v, n in dist.items() if v != "unknown" and int(n) > 0}
        if len(known) < 2:
            continue
        top = sorted(known.items(), key=lambda kv: -kv[1])[:3]
        total = sum(known.values()) or 1
        spread[key] = [{"value": v, "share": round(n / total, 2)} for v, n in top]
    return {
        "query": (query or "").strip(),
        "believed": {"must": dict(getattr(contract, "must", {}) or {}),
                     "prefer": dict(getattr(contract, "prefer", {}) or {}),
                     "text": str(getattr(contract, "text", "") or "")},
        "returned": {"titles": titles, "companies": sorted(set(companies))[:8],
                     "pool": int(((counts or {}).get("__pool__") or 0)) or None},
        "spread": spread,
        "vocabulary": {k.key: list(k.values) for k in schema.for_kind(kind)
                       if k.type in (FacetType.categorical, FacetType.ordinal)},
        "so_far": history_pack(history),
    }


def parse(raw: dict | None, schema: FacetSchema, kind: str, *, max_readings: int = 3) -> IntentCheck | None:
    """The model's answer → a checked `IntentCheck`, or None when there is nothing usable.

    Every facet value is validated against the vocabulary before it can reach a contract, and a reading
    whose edit is entirely illegal is dropped rather than shown — an option that would change nothing,
    or change something into a value the index does not hold, is worse than no option at all."""
    if not isinstance(raw, dict):
        return None
    out = IntentCheck(believed=str(raw.get("believed") or "").strip(),
                      noticed=str(raw.get("noticed") or "").strip()[:280],
                      question=str(raw.get("question") or "").strip(),
                      ask=str(raw.get("ask") or "").strip(),
                      understanding=[str(u).strip()[:120] for u in (raw.get("understanding") or [])
                                     if str(u).strip()][:6])
    for item in (raw.get("readings") or [])[: max_readings * 2]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        r = Reading(label=label[:60], says=str(item.get("says") or "").strip()[:240],
                    text=str(item.get("text") or "").strip()[:400])
        for section in ("must", "prefer"):
            for key, vals in (item.get(section) or {}).items():
                k = schema.key(key)
                if k is None or kind not in k.kinds or not isinstance(vals, list):
                    continue
                clean = [str(schema.validate_value(key, v)) for v in vals
                         if schema.validate_value(key, v) is not None]
                if clean:
                    getattr(r, section)[key] = sorted(set(clean))
        if r.must or r.prefer or r.text:
            out.readings.append(r)
        if len(out.readings) >= max_readings:
            break
    if not out.readings or not out.question:
        # No question is a legitimate end state — the conversation has converged — but only worth
        # returning if there is something to show for it. An empty object is noise.
        if out.understanding or out.noticed:
            out.readings = []
            return out
        return None
    return out


def worth_asking(query: str, coverage: dict | None, *, ambiguous: bool = False,
                 min_pool: int = 12, max_words: int = 6, history: list | None = None) -> tuple[bool, str]:
    """Is the search unsure enough to be worth a question — and a model call?

    A debugger that interrupts a session it understands is noise, and every ask here costs a call, so
    the cold-start test is narrow: a short query, one the lexicon read two ways, or a result set nothing
    matched well.

    ONCE A CONVERSATION IS UNDER WAY, THAT TEST IS THE WRONG ONE. The reader answering in their own
    words makes the query LONGER, so the "specific enough" rule fired exactly when they had just
    engaged — the notes and the question vanished the moment they used them, and the new results
    arrived with nothing to read them by. A conversation in progress keeps its thread: the model is
    given the turn and decides for itself whether anything is left to ask, returning no readings when
    it has converged. That is the right place for the decision, because only it can tell the difference
    between "answered" and "still vague"."""
    cov = coverage or {}
    pool = int(cov.get("pool") or 0)
    if pool < min_pool:
        return False, "too few results to reinterpret"
    if history:
        return True, "we are mid-conversation"
    if ambiguous:
        return True, "the words carry more than one reading"
    words = len([w for w in (query or "").split() if w])
    if not words:
        return False, "nothing was typed"
    if words <= max_words:
        return True, "a short query leaves a lot unsaid"
    if cov.get("weak") or cov.get("diagnosis"):
        return True, "nothing matched strongly"
    return False, "the question looks specific enough"
