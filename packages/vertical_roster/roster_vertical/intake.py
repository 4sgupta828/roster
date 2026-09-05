"""GUIDED INTAKE vocabulary (vertical — docs/specs/guided-intake.md §1, §2.1): the two directions, the artifact
each needs, the completeness CHECKLIST per artifact (which items are required, which contract key an answer
feeds and how), the REQUIRED / OPTIONAL keys per direction, the question words, and the two prompts (the
completeness read; the per-turn answer read). Mechanics (whether to ask, budgets, the state) live in
`roster_kernel.facets.intake`; this module only knows the roster domain."""
from __future__ import annotations

from dataclasses import dataclass

from .facet_schema import FACET_SCHEMA, VALUE_LABELS

DIRECTIONS = ("job", "candidate")                       # I am looking for a role | I am hiring
ARTIFACT_FOR = {"job": "profile", "candidate": "jd"}
SEARCH_KIND = {"job": "job", "candidate": "person"}     # the entity kind the direction searches


@dataclass(frozen=True)
class Item:
    name: str
    required: bool
    key: str | None = None        # the contract key an answer feeds (None = artifact-only)
    mode: str = "must"            # must | prefer
    words: str = ""               # the question, plain
    hint: str = ""                # one line under the question


CHECKLIST: dict[str, tuple[Item, ...]] = {
    "profile": (
        Item("current_role", True, None, "must", "What is your current title, and where?", "e.g. 'senior backend engineer at Acme'"),
        Item("level", True, "level", "prefer", "How senior are you today?", "the level your title carries"),
        Item("years", True, None, "must", "Roughly how many years of professional experience?", ""),
        Item("field", True, "field", "must", "Which field is your work in?", "the domain, not the job title"),
        Item("skills", True, "skill", "prefer", "Which three to five skills or systems do you own?", "as they would appear on a posting"),
        Item("location", True, "metro", "must", "Where should roles be?", "a metro, a state, or remote"),
        Item("comp", True, "comp", "must", "What base salary range are you aiming for?", "a band; you can decline"),
        Item("target_level", False, "level", "prefer", "Should I lean toward roles a step up?", ""),
        Item("company_types", False, "company_type", "prefer", "Any preference on company type?", "startup, public, Fortune 500, big tech"),
        Item("avoid", False, None, "must", "Anything to avoid — industries, on-site only, contract work?", ""),
        Item("notable_work", False, None, "must", "Public work I should know about — repos, papers, talks?", ""),
        Item("authorization", False, None, "must", "Anything on work authorization I should factor in?", ""),
    ),
    "jd": (
        Item("title", True, None, "must", "What is the title of the role?", ""),
        Item("level", True, "level", "must", "What level is this hire?", ""),
        Item("location", True, "metro", "must", "Where is the role — which metro, or remote?", ""),
        Item("must_skills", True, "skill", "must", "Which three or more skills or systems are must-haves?", "the ones you would reject without"),
        Item("responsibilities", True, None, "must", "What will this person actually do — two or three responsibilities?", ""),
        Item("comp", True, None, "must", "What is the base salary range, or is it undisclosed?", "peers' stated ranges are shown as a market signal"),
        Item("team", False, None, "must", "Which team, and who does the role report to?", ""),
        Item("nice_to_have", False, "skill", "prefer", "Any nice-to-haves?", ""),
        Item("evidence", False, "evidence", "must", "Do you want public proof of work — repos, papers, talks?", ""),
        Item("disqualifiers", False, None, "must", "Anything that rules a candidate out?", ""),
        Item("company_context", False, None, "must", "What should candidates know about the company — stage, product, why now?", ""),
    ),
}

# Contract keys the intake must know before a search runs (asked regardless of the corpus) and the keys it may
# ask about when the pool is split on them (spec §0.2).
REQUIRED_KEYS = {"job": ["field", "level", "metro"], "candidate": ["field", "level", "metro", "evidence"]}
OPTIONAL_KEYS = {"job": ["company_type", "work_mode", "function", "employment_type"],
                 "candidate": ["company_type", "work_type", "function", "years"]}

QUESTION_WORDS: dict[str, dict] = {
    "field": {"job": "Which field should the roles be in?", "candidate": "Which field is the role in?"},
    "level": {"job": "What level of role?", "candidate": "What level is the hire?"},
    "metro": {"job": "Which metro — or remote?", "candidate": "Which metro is the role in?"},
    "evidence": {"candidate": "Must candidates have public work — repos, papers, talks?", "job": "Should roles favour teams with public work?"},
    "company_type": {"job": "Any preference on company type?", "candidate": "Should candidates come from a particular company type?"},
    "work_mode": {"job": "Remote, hybrid, or on-site?", "candidate": "Remote, hybrid, or on-site?"},
    "function": {"job": "Which kind of work day to day?", "candidate": "Which kind of work day to day?"},
    "employment_type": {"job": "Full-time, contract, or internship?", "candidate": "Full-time, contract, or internship?"},
    "work_type": {"candidate": "An individual contributor, a manager, or a founder?", "job": "IC or manager roles?"},
    "years": {"candidate": "How many years of experience?", "job": "How many years of experience?"},
    "comp": {"decline": "Prefer not to say"},
}

_DIRECTION_WORDS = {"job": "roles", "candidate": "people"}


def required_items(artifact: str) -> list[str]:
    return [it.name for it in CHECKLIST.get(artifact, ()) if it.required]


def item(artifact: str, name: str) -> Item | None:
    return next((it for it in CHECKLIST.get(artifact, ()) if it.name == name), None)


def question_for(kind: str, name: str, ctx: str) -> str:
    """The words for a question: an ITEM (ctx = artifact) or a KEY (ctx = direction)."""
    if kind == "item":
        it = item(ctx, name)
        return it.words if it else f"{name.replace('_', ' ')}?"
    w = QUESTION_WORDS.get(name) or {}
    return w.get(ctx) or w.get("job") or w.get("candidate") or f"{name.replace('_', ' ')}?"


def option_label(key: str, value: str) -> str:
    return VALUE_LABELS.get(value) or str(value).replace("_", " ")


def completeness_prompt(artifact: str) -> str:
    items = CHECKLIST.get(artifact, ())
    names = ", ".join(it.name for it in items)
    what = "a candidate's résumé or self-description" if artifact == "profile" else "a job description or a hiring manager's role notes"
    return (f"You read {what} and report which of these items it states: {names}.\n"
            "Return ONLY JSON: {\"present\": {item: the value as stated, short}, \"missing\": [items it does not state at all], "
            "\"weak\": [items it touches but too thinly to search on]}. Every item appears in exactly one of the three. "
            "Never infer an item the text does not state; a level is 'missing' when no title word states it; comp is 'missing' "
            "unless a figure or range is written. Keep values verbatim-ish and under 12 words.")


def turn_prompt(direction: str) -> str:
    keys = ", ".join(REQUIRED_KEYS[direction] + OPTIONAL_KEYS[direction])
    vocab = "; ".join(f"{k.key}: {', '.join(k.values)}" for k in FACET_SCHEMA.for_kind(SEARCH_KIND[direction]) if k.values and k.key in set(REQUIRED_KEYS[direction] + OPTIONAL_KEYS[direction]))
    return (f"You are the intake for a {_DIRECTION_WORDS[direction]} search. The user was asked ONE question (given) and replied. "
            "Map the reply onto the question and onto any other item or key the reply CLEARLY states. Return ONLY JSON: "
            "{\"answers\": {name: value | null}, \"free_text\": the reply's own words for anything that fits no key, "
            "\"direction\": \"job\" | \"candidate\" | null}. `value` for a schema key must be one of its vocabulary tokens "
            f"(keys: {keys}; vocabulary — {vocab}); for an open key (metro, skill) a lowercase token; null means the user declined "
            "or the reply does not answer. Never guess; never add an answer the reply does not state.")


def understood_words(direction: str, contract: dict, answers: dict | None = None) -> str:
    """The plain 'what I understood' line, code-built from the contract (no model call)."""
    c = contract or {}
    L = lambda k, v: option_label(k, v).replace("us", "US") if k == "country" else option_label(k, v)
    musts = [f"{L(k, v)}" for k, vs in (c.get("must") or {}).items() if isinstance(vs, list) for v in vs]
    prefers = [f"{L(k, v)}" for k, vs in (c.get("prefer") or {}).items() if isinstance(vs, list) for v in vs]
    parts = []
    noun = _DIRECTION_WORDS[direction]
    parts.append(f"{noun.capitalize()} that must be " + ", ".join(musts) if musts else f"{noun.capitalize()} matching \"{(c.get('text') or '').strip()[:80]}\"")
    if prefers:
        parts.append("preferring " + ", ".join(prefers))
    ctr = c.get("center") or {}
    if ctr.get("key"):
        parts.append(f"centred on {option_label(ctr['key'], str(ctr.get('value')))} {ctr['key'].replace('_', ' ')}")
    a = answers or {}
    if a.get("comp"):
        parts.append(f"around {option_label('comp', str(a['comp']))}")
    if a.get("years"):
        parts.append(f"{a['years']} years in")
    return "; ".join(parts) + "."
