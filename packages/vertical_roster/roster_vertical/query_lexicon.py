"""THE VERTICAL'S HALF OF THE LEXICON (docs/specs/query-intent-decoding.md §4).

The kernel's `roster_kernel.facets.lexicon` knows how to find and rank spans. It knows nothing about
places, seniority or work modes. This file is that knowledge, and nothing else lives here.

Everything is built FROM the tables the vertical already had — `METRO_ALIAS`, `US_METROS`, the schema's
own closed vocabularies — rather than restated beside them. A second copy of "which words mean the Bay
Area" is a second thing to get out of step.
"""
from __future__ import annotations

from .facet_schema import FACET_SCHEMA
from .people_facets import METRO_ALIAS, US_METROS

# Words that carry no facet meaning in a search box. A query made only of spans and these is a query
# with nothing left to search semantically — which is what lets the evaluator use the must-slice as its
# pool instead of a one-word embedding's neighbourhood.
FILLER = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "at", "on", "for", "to", "with", "near", "around",
    "job", "jobs", "role", "roles", "position", "positions", "opening", "openings", "opportunity",
    "opportunities", "hiring", "career", "careers", "work", "vacancy", "vacancies",
    "person", "people", "candidate", "candidates", "profile", "profiles", "someone", "who",
    "me", "my", "i", "we", "us", "any", "some", "please", "show", "find", "search", "looking",
})

# THE VALUES THAT ARE ALSO ORDINARY ENGLISH. A bare match on one of these is as likely to be grammar as
# intent — "remote possibility of travel", "staff the front desk", "lead the team", "a principal at the
# school". They still RANK; they are never allowed to filter on their own. This list is the whole
# defence against the failure the panel raised, so it is deliberately generous.
RISKY_VALUES = frozenset({
    "remote", "staff", "lead", "principal", "director", "head", "executive", "founder", "other",
    "research", "design", "product", "operations", "sales", "marketing", "legal", "finance",
    "manager", "intern", "senior", "junior", "mid", "contract", "part time", "full time",
})

# Which keys the lexicon may HARDEN into a must when it accounts for the whole query. Deliberately
# short: a closed vocabulary the reader typed verbatim, or a place. Open-ended keys (skill, specialty,
# company, role_family) rank only — a wrong `must: company` is the wrong-company failure class, and a
# wrong `must: skill` filters by an extraction gap rather than by fact.
MUST_KEYS = frozenset({"work_mode", "employment_type", "work_type", "level", "field", "function", "metro", "state"})

_LEVEL_ALIAS = {
    "staff": "staff_plus", "staff+": "staff_plus", "staff plus": "staff_plus",
    "principal": "staff_plus", "distinguished": "staff_plus", "l6": "staff_plus", "l7": "staff_plus",
    "head of": "leadership", "vp": "leadership", "vice president": "leadership", "chief": "leadership",
    "cto": "leadership", "ceo": "leadership", "cpo": "leadership", "director": "leadership",
    "exec": "leadership", "executive": "leadership", "leadership": "leadership",
    "entry level": "junior", "entry": "junior", "grad": "junior", "graduate": "junior",
    "new grad": "junior", "sde1": "junior", "internship": "intern",
    "mid level": "mid", "midlevel": "mid", "ic": "mid",
    "sr": "senior", "sr.": "senior", "senior level": "senior",
}

_WORK_MODE_ALIAS = {
    "wfh": "remote", "work from home": "remote", "remotely": "remote", "fully remote": "remote",
    "distributed": "remote", "anywhere": "remote",
    "in office": "onsite", "in person": "onsite", "on site": "onsite", "onsite": "onsite", "office": "onsite",
    "hybrid": "hybrid", "flexible": "hybrid",
}

_EMPLOYMENT_ALIAS = {
    "full time": "full_time", "fulltime": "full_time", "ft": "full_time",
    "part time": "part_time", "parttime": "part_time", "pt": "part_time",
    "contract": "contract", "contractor": "contract", "freelance": "contract", "c2c": "contract",
    "intern": "internship", "internship": "internship",
}

_WORK_TYPE_ALIAS = {
    "ic": "ic", "individual contributor": "ic", "hands on": "ic",
    "manager": "manager", "people manager": "manager", "management": "manager",
    "founder": "founder", "founding": "founder", "cofounder": "founder", "co founder": "founder",
}


def _metro_aliases() -> dict[str, str]:
    """Every surface form of a metro the vertical already knows, canonicalised the way the INDEX stores
    it. Built from `US_METROS` (its city lists) and `METRO_ALIAS` — the two tables that already exist —
    so a place cannot mean one thing to the geo code and another to a query."""
    out: dict[str, str] = {}
    for canon, meta in US_METROS.items():
        out[canon.replace("_", " ")] = canon
        for city in (meta.get("cities") or []):
            out[str(city).lower()] = canon
    for surface, canon in METRO_ALIAS.items():
        out[surface.replace("_", " ")] = canon
    # METRO_ALIAS is applied last so it wins: `new_york → nyc` is what the index holds, and the schema's
    # own guidance string still says "new_york", which is the mismatch the spec flagged (§1.3).
    return out


ALIASES: dict[str, dict[str, str]] = {
    "level": _LEVEL_ALIAS,
    "work_mode": _WORK_MODE_ALIAS,
    "employment_type": _EMPLOYMENT_ALIAS,
    "work_type": _WORK_TYPE_ALIAS,
    "metro": _metro_aliases(),
}


def lexicon_config(kind: str) -> dict:
    """What `roster_kernel.facets.lexicon` needs from this vertical, for one entity kind."""
    keys = {k.key for k in FACET_SCHEMA.for_kind(kind)}
    return {
        "aliases": {k: v for k, v in ALIASES.items() if k in keys},
        "filler": FILLER,
        "must_keys": frozenset(k for k in MUST_KEYS if k in keys),
        "risky_values": RISKY_VALUES,
    }
