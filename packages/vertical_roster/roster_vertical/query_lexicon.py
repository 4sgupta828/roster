"""THE VERTICAL'S HALF OF THE LEXICON (docs/specs/query-intent-decoding.md §4).

The kernel's `roster_kernel.facets.lexicon` knows how to find and rank spans. It knows nothing about
places, seniority or work modes. This file is that knowledge, and nothing else lives here.

Everything is built FROM the tables the vertical already had — `METRO_ALIAS`, `US_METROS`, the schema's
own closed vocabularies — rather than restated beside them. A second copy of "which words mean the Bay
Area" is a second thing to get out of step.
"""
from __future__ import annotations

from .facet_schema import FACET_SCHEMA
from .people_facets import US_METROS

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
    """Surface forms of a place → the token THE FACET INDEX HOLDS.

    Measured against prod, because the first version of this got it backwards. `METRO_ALIAS` exists for
    GEO SCOPE resolution and rewrites `new_york → nyc` and `san_francisco → bay_area`. The facet index
    follows the extractor's own guidance instead ("normalized metro token: bay_area, new_york, seattle,
    london"), and the counts are decisive:

        new_york 21,942 · san_francisco 21,328 · london 15,157 · seattle 10,928 · bay_area 9,641 · nyc 3,126

    So applying METRO_ALIAS here canonicalised a query AWAY from the rows: "new york" would have
    targeted 3,126 postings instead of 21,942, and "san francisco" 9,641 instead of 21,328 — worse than
    not canonicalising at all. Two vocabularies for one key is the underlying defect; until they are
    reconciled, the query side must speak the INDEX's dialect, not the scope resolver's.

    The rule: a typed place becomes its own underscored form, and short or alternative names point at
    the long form the index uses."""
    out: dict[str, str] = {}
    for canon, meta in US_METROS.items():
        out[canon.replace("_", " ")] = canon                 # "bay area" → bay_area
        for city in (meta.get("cities") or []):
            city = str(city).lower().strip()
            if city:
                out.setdefault(city, city.replace(" ", "_"))  # "san francisco" → san_francisco, as stored
    # nicknames and abbreviations, pointed at the long form rather than at the scope resolver's token
    out.update({"nyc": "new_york", "new york city": "new_york", "manhattan": "new_york",
                "brooklyn": "new_york", "sf": "san_francisco", "san fran": "san_francisco",
                "la": "los_angeles", "bengaluru": "bangalore", "washington dc": "washington",
                "dc": "washington", "silicon valley": "bay_area", "south bay": "bay_area"})
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
